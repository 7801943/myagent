"""
Agent 核心调度器 (AgentHarness) — 无状态纯执行引擎

设计原则：
  - Harness 保持无状态——不持有 ContextManager、不持有 StateStore、不管理 session 生命周期
  - Session 把 context 作为参数传入 run()，Harness 只负责拿到上下文后执行 ReAct 循环
  - 每个 Session 拥有独立的 Harness 实例 (per-session)
  - PromptRenderer 由 Session 持有和调用，Harness 不感知

职责：
1. 持有 LLMClient + ToolInterface + EventBus
2. 驱动 ReAct 循环：系统指令检查 → LLM 推理 → 工具执行 → 循环
3. 内部组装 ExecutionContext（Session 不感知）
4. 内化 finalize_content（StreamResult.text 返回已 finalized 的文本）
5. 协作式取消检查
6. 关键节点派发 Event（通过 EventBus）

审批策略：
  - approval_handler 由 Session 在 run() 调用时通过参数传入（Web 用 ClientBridge，CLI 用自定义函数）
  - Harness 不持有 approval_handler 状态
"""
import asyncio
from typing import Callable, Awaitable, TYPE_CHECKING

from myagent.core.llm import LLMClient, StreamResult
from myagent.core.tools import ToolInterface
from myagent.core.events import (
    ApprovalNeeded,
    EventBus,
    ExecutionContext,
    SafetyBlocked,
    StateChange,
    ToolEnd,
    ToolError,
    ToolStart,
)
from myagent.core.commander import check_system_commands
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall, ToolResult as MsgToolResult, ContentBlock
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.models import SessionData

logger = get_logger(__name__)


class AgentHarness:
    """
    无状态纯执行引擎。依赖注入所有组件。

    职责定位为 ReAct 循环调度：
      - 管理 LLM ↔ tools 之间的交互
      - 日志、取消管理
      - EventBus 事件分发

    工具执行和审批逻辑由 ToolInterface.execute_with_approval() 负责。

    用法：
        harness = AgentHarness(llm_client=llm, tool_interface=tools, events=events)
        result = await harness.run(context, session_id, session_data, command_handler)
    """

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        tool_interface: ToolInterface,
        events: EventBus | None = None,
        hooks: EventBus | None = None,
        max_iterations: int = 100,
    ):
        self._llm = llm_client
        self._tools = tool_interface
        self._events = events or hooks
        if self._events is None:
            raise ValueError("AgentHarness requires an EventBus")
        self._max_iterations = max_iterations

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def hooks(self) -> EventBus:
        """Backward-compatible alias for EventBus."""
        return self._events

    @property
    def tool_interface(self) -> ToolInterface:
        return self._tools

    @property
    def router(self):
        """向后兼容：暴露 ProviderRouter（Session._init_meta_from_harness 需要）。"""
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

    async def run(
        self,
        context: ContextManager,
        session_id: str,
        session_data: "SessionData",
        command_handler: Callable | None = None,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ) -> StreamResult:
        """
        驱动 ReAct 循环：系统指令检查 → LLM 推理 → 工具执行 → 循环。

        Harness 内部组装 ExecutionContext，Session 不需要感知 ExecutionContext 的存在。

        注意：
          - Prompt 渲染 + 用户消息写入已在 Session.chat() 中完成并写入 context
          - StreamResult.text 返回的是已 finalized 的文本，Session 无需二次处理
          - run() 内不做 SessionData 状态持久化（由 Session 在 run() 返回后 / 异常时触发）
          - 除 CancelledError 外的 Exception 将被直接透传抛出，由上层 Session catch

        Returns:
            StreamResult: 最终的 LLM 聚合结果（text 已 finalized + stop_reason）
        """
        # Harness 内部组装执行上下文（Session 不感知 ExecutionContext）
        ctx = ExecutionContext(
            session_id=session_id,
            session_meta=session_data,
            system_command_handler=command_handler,
        )

        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1
                logger.debug(f"Iteration {ctx.iteration}")

                # 每轮开始前检查取消信号
                await self._check_cancelled()

                # 步骤 A：系统指令检查（/model, /new, /clear 等）
                await check_system_commands(context, ctx, self._events)

                # 步骤 B：LLM 推理（LLMClient 内部完成流式聚合 + EventBus 转发）
                # 注意：直接从 ToolInterface 获取实时工具 schema 列表（含热加载发现的新工具）
                tools = self._tools.list_schemas()
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
                    await self._events.publish(ctx.event(StateChange, state="idle"))
                    # 迁移期保留 finalize_content 兼容；新逻辑应走直接调用。
                    final_text = self._events.finalize_content(ctx, llm_result.text)
                    return StreamResult(text=final_text or "", stop_reason=llm_result.stop_reason)

                # 步骤 C：工具执行（含安全分拣 + 人工审批）
                await self._execute_tools(context, ctx, llm_result.tool_calls, approval_handler)

            # 达到最大迭代次数
            logger.warning(f"Harness reached max iterations ({self._max_iterations})")
            msg = "达到最大迭代次数限制，终止执行。"
            await context.add_assistant_message(content=msg, tool_calls=None)
            final_text = self._events.finalize_content(ctx, msg)
            return StreamResult(text=final_text or "", stop_reason="max_iterations")

        except asyncio.CancelledError:
            cancel_msg = "[系统] 操作已取消"
            logger.info(f"Harness cancelled at iteration {getattr(ctx, 'iteration', 0)}")
            await context.add_assistant_message(content=cancel_msg, tool_calls=None)
            final_text = self._events.finalize_content(ctx, cancel_msg)
            return StreamResult(text=final_text or "", stop_reason="cancelled")

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
        ctx: ExecutionContext,
        tool_calls: list[ToolCall],
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ) -> None:
        """
        工具执行入口：发射 ToolStart → 委托 ToolInterface 完成全部执行+审批 → 写入结果+发射事件。

        工具的实际执行、安全分拣、人工审批由 ToolInterface.execute_with_approval() 统一处理，
        harness 仅负责事件分发和结果写入 context。
        """
        await self._events.publish(ctx.event(StateChange, state="waiting_tool"))

        # 为每个工具发射 tool_start 事件
        for tc in tool_calls:
            await self._events.publish(ctx.event(
                ToolStart,
                tool_name=tc.name,
                args=tc.arguments,
                call_id=tc.id,
            ))
            logger.debug(f"Tool start: {tc.name} (call_id={tc.id})")

        # 如果有待审批工具，发射 ApprovalNeeded 事件
        # （注意：实际的审批判断在 ToolInterface 内部完成，这里仅做事件通知）
        if approval_handler:
            await self._events.publish(ctx.event(ApprovalNeeded, tool_calls=tool_calls))

        # 委托 ToolInterface 完成全部执行管线（含安全检查 + 人工审批 + 批准后重执行）
        executed_tools = await self._tools.execute_with_approval(
            tool_calls,
            approval_handler=approval_handler,
        )

        # 将执行结果写入 context 并发射对应事件
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

            # 2. 发射事件与日志记录
            if et.status == "rejected" or tr.is_error:
                if tr.is_error and "denied_by" in tr.metadata:
                    await self._events.publish(ctx.event(
                        SafetyBlocked,
                        rule=tr.metadata["denied_by"],
                        reason=str(tr.content),
                        action="deny",
                        call_id=tc.id,
                        tool_name=tc.name,
                    ))
                    logger.info(f"Safety blocked: {tc.name} by {tr.metadata['denied_by']}")
                await self._events.publish(ctx.event(
                    ToolError,
                    tool_name=tc.name,
                    error=Exception(tr.content),
                    call_id=tc.id,
                ))
                
                if et.status == "rejected":
                    logger.info(f"Tool rejected: {tc.name} (call_id={tc.id})")
                else:
                    logger.debug(f"Tool error: {tc.name} (call_id={tc.id}): {tr.content}")
            else:
                await self._events.publish(ctx.event(
                    ToolEnd,
                    tool_name=tc.name,
                    result=tr,
                    call_id=tc.id,
                    latency_ms=lat,
                ))
                logger.debug(f"Tool end: {tc.name} (call_id={tc.id}), latency={lat}ms")
