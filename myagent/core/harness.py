"""
Agent 核心调度器 (AgentHarness) — ReAct 中枢

职责：
1. 持有 LLMClient + ToolInterface + HookManager
2. 驱动 ReAct 循环：系统指令检查 → LLM 推理 → 工具执行 → 循环
3. 协作式取消检查
4. 关键节点派发 Event（通过 HookManager）
5. 管理转发 session、llm、tools 之间的交互

工具执行与审批已移至 ToolInterface.execute_with_approval()，
harness 仅负责调用并将结果写入 context + 发射 hook 事件。

删除 Turn 抽象后，循环逻辑直接内联在此处。
"""
import asyncio
import re
from typing import Callable, Awaitable

from myagent.core.llm import LLMClient, StreamResult
from myagent.core.tools import ToolInterface, ExecutedTool
from myagent.core.hook import HookContext, HookManager
from myagent.core.commander import check_system_commands
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall, ToolResult as MsgToolResult, ContentBlock
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class AgentHarness:
    """
    Agent 核心调度器。依赖注入所有组件。
    
    职责定位为调度中心：
      - 管理 session ↔ LLM ↔ tools 之间的交互
      - 日志、取消管理
      - Hook 事件分发
    
    工具执行和审批逻辑由 ToolInterface.execute_with_approval() 负责。
    
    用法：
        harness = AgentHarness(llm_client=llm, tool_interface=tools, hooks=hooks)
        result = await harness.run(context, ctx)
    """

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        tool_interface: ToolInterface,
        hooks: HookManager,
        max_iterations: int = 100,
    ):
        self._llm = llm_client
        self._tools = tool_interface
        self._hooks = hooks
        self._max_iterations = max_iterations
        self._approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    @property
    def tool_interface(self) -> ToolInterface:
        return self._tools

    @property
    def router(self):
        """向后兼容：暴露 ProviderRouter（Session._init_meta_from_agent 需要）。"""
        return self._llm.router

    @property
    def tool_manager(self):
        """向后兼容：暴露 ToolManager（Session._init_meta_from_harness 需要）。"""
        return self._tools._tool_manager if hasattr(self._tools, '_tool_manager') else None

    @property
    def safety_guard(self):
        """暴露 ToolInterface 的安全状态（prompt/variables.py 需要）。"""
        return self._tools if self._tools.has_safety else None

    # ── 主入口 ──

    async def run(self, context: ContextManager, ctx: HookContext) -> StreamResult:
        """
        驱动 ReAct 循环：系统指令检查 → LLM 推理 → 工具执行 → 循环。
        
        Returns:
            StreamResult: 最终的 LLM 聚合结果（text + stop_reason）
        """
        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1
                logger.debug(f"Iteration {ctx.iteration}")

                # 每轮开始前检查取消信号
                await self._check_cancelled()

                # 步骤 A：系统指令检查（/model, /new, /clear 等）
                await check_system_commands(context, ctx, self._hooks)

                # 步骤 B：LLM 推理（LLMClient 内部完成流式聚合 + Hook 转发）
                tools = ctx.session_meta.tool.tools if ctx.session_meta else self._tools.list_schemas()
                messages = context.get_messages()
                llm_result = await self._llm.generate(messages, tools, ctx)

                # 写入 assistant 消息到上下文
                await context.add_assistant_message(
                    content=llm_result.text,
                    tool_calls=llm_result.tool_calls if llm_result.tool_calls else None,
                )
                if llm_result.usage:
                    context.update_usage(llm_result.usage)

                # 无工具调用 → 循环结束
                if not llm_result.tool_calls:
                    await self._hooks.emit("state_change", ctx, state="idle")
                    return llm_result

                # 步骤 C：工具执行（含安全分拣 + 人工审批）
                await self._execute_tools(context, ctx, llm_result.tool_calls)

            # 达到最大迭代次数
            logger.warning(f"Harness reached max iterations ({self._max_iterations})")
            msg = "达到最大迭代次数限制，终止执行。"
            await context.add_assistant_message(content=msg, tool_calls=None)
            return StreamResult(text=msg, stop_reason="max_iterations")

        except asyncio.CancelledError:
            cancel_msg = "[系统] 操作已取消"
            logger.info(f"Harness cancelled at iteration {getattr(ctx, 'iteration', 0)}")
            await context.add_assistant_message(content=cancel_msg, tool_calls=None)
            return StreamResult(text=cancel_msg, stop_reason="cancelled")

    # ── 取消检查 ──

    async def _check_cancelled(self) -> None:
        """协作式取消检查点。当前任务被取消时立即抛出 CancelledError。"""
        task = asyncio.current_task()
        if task is not None and task.cancelled():
            raise asyncio.CancelledError()

    # ── 工具执行（调度转发） ──

    async def _execute_tools(
        self,
        context: ContextManager,
        ctx: HookContext,
        tool_calls: list[ToolCall],
    ) -> None:
        """
        工具执行入口：发射 tool_start → 委托 ToolInterface 完成全部执行+审批 → 写入结果+发射 hook。
        
        工具的实际执行、安全分拣、人工审批由 ToolInterface.execute_with_approval() 统一处理，
        harness 仅负责 hook 事件管理和结果写入 context。
        """
        await self._hooks.emit("state_change", ctx, state="waiting_tool")

        # 为每个工具发射 tool_start 事件
        for tc in tool_calls:
            await self._hooks.emit("tool_start", ctx, tool_name=tc.name, args=tc.arguments, call_id=tc.id)
            logger.debug(f"Tool start: {tc.name} (call_id={tc.id})")

        # 如果有待审批工具，发射 approval_needed hook 事件
        # （注意：实际的审批判断在 ToolInterface 内部完成，这里仅做事件通知）
        if self._approval_handler:
            await self._hooks.emit("approval_needed", ctx, tool_calls=tool_calls)

        # 委托 ToolInterface 完成全部执行管线（含安全检查 + 人工审批 + 批准后重执行）
        executed_tools = await self._tools.execute_with_approval(
            tool_calls,
            approval_handler=self._approval_handler,
        )

        # 将执行结果写入 context 并发射对应的 hook 事件
        for et in executed_tools:
            tc, tr = et.tool_call, et.result
            lat = tr.metadata.get("latency_ms", 0)

            # 1. 写入上下文 (处理多模态)
            blocks = getattr(tr, "content_blocks", None)
            if blocks:
                content = [ContentBlock(type="text", text=tr.content)] + [
                    ContentBlock(type="image_base64", base64_data=cb["data"], media_type=cb.get("media_type", "image/png"))
                    for cb in blocks if cb.get("type") == "image_base64"
                ]
            else:
                content = tr.content

            await context.add_tool_result(tc.id, MsgToolResult(
                tool_call_id=tc.id, tool_name=tc.name, content=content, metadata={"latency_ms": lat}
            ))

            # 2. 发射 hook 事件与日志记录
            if et.status == "rejected" or tr.is_error:
                if tr.is_error and "denied_by" in tr.metadata:
                    await self._hooks.emit("safety_blocked", ctx, rule=tr.metadata["denied_by"], reason=str(tr.content), action="deny", call_id=tc.id, tool_name=tc.name)
                    logger.info(f"Safety blocked: {tc.name} by {tr.metadata['denied_by']}")
                await self._hooks.emit("tool_error", ctx, tool_name=tc.name, error=Exception(tr.content), call_id=tc.id)
                
                if et.status == "rejected":
                    logger.info(f"Tool rejected: {tc.name} (call_id={tc.id})")
                else:
                    logger.debug(f"Tool error: {tc.name} (call_id={tc.id}): {tr.content}")
            else:
                await self._hooks.emit("tool_end", ctx, tool_name=tc.name, result=tr, call_id=tc.id, latency_ms=lat)
                logger.debug(f"Tool end: {tc.name} (call_id={tc.id}), latency={lat}ms")