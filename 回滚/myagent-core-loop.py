"""
AgentLoop：ReAct 循环引擎。
流式 → 解析 → 工具执行 → 再流式，直到 stop_reason != tool_use。

重构要点：
- CompositeHook → HookManager (事件分发器)
- CancellationToken 全链路取消检查
- 超时看门狗 (LLM / 工具 / 迭代)
- 审计内联（框架保障，不依赖 Hook 注册）
"""
import asyncio
from typing import AsyncIterator

from myagent.providers.base import StreamEvent
from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.context.message import ToolResult as MsgToolResult
from myagent.core.hook import HookContext, HookManager
from myagent.core.stream import StreamProcessor, StreamResult
from myagent.core.parser import StreamParser
from myagent.core.cancellation import CancellationToken, AgentCancelledError
from myagent.tools.executor import ToolExecutor
from myagent.observability.audit_logger import AuditLogger
from myagent.observability.events import EventType
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AgentLoop:
    """
    ReAct 循环引擎。
    while True:
        1. 流式调用 Provider → StreamProcessor 聚合
        2. 若有 tool_calls → 并行执行 → 结果写入 ContextManager
        3. 若无 tool_calls → break
    """

    def __init__(
        self,
        provider_router: ProviderRouter,
        context: ContextManager,
        executor: ToolExecutor,
        hook: HookManager,
        max_iterations: int = 50,
        # ── 超时看门狗参数 ──
        llm_timeout: float = 120.0,
        tool_batch_timeout: float = 60.0,
        iteration_timeout: float = 300.0,
        # ── 取消令牌（由 Agent.run() 设置）──
        cancel_token: CancellationToken | None = None,
        # ── 审计日志（内联）──
        audit_logger: AuditLogger | None = None,
    ):
        self._router = provider_router
        self._context = context
        self._executor = executor
        self._hook = hook
        self._max_iterations = max_iterations
        self._llm_timeout = llm_timeout
        self._tool_batch_timeout = tool_batch_timeout
        self._iteration_timeout = iteration_timeout
        self._cancel_token = cancel_token
        self._audit = audit_logger

    async def _watchdog(self, ctx: HookContext, timeout: float, stage: str):
        """
        看门狗协程：timeout 秒后向 Hook 发出 timeout_warning 事件。
        不会自动取消任何操作，仅通知前端。
        被 cancel 或主操作完成时，由外部 task.cancel() 终止。
        """
        await asyncio.sleep(timeout)
        await self._hook.emit("timeout_warning", ctx,
            stage=stage, timeout_seconds=timeout,
            message=f"{stage} 已超过 {timeout}s，您可以选择继续等待或取消操作",
        )
        # 审计内联
        if self._audit:
            await self._audit.emit_timeout(
                stage=stage, timeout_seconds=timeout,
                session_id=ctx.session_id, iteration=ctx.iteration,
            )

    async def _stream_with_watchdog(self, ctx: HookContext, processor: StreamProcessor, parser: StreamParser) -> StreamResult:
        """带看门狗的 Provider 流式调用。"""
        watchdog = asyncio.create_task(
            self._watchdog(ctx, self._llm_timeout, "llm_generation")
        )
        content_started = False
        try:
            messages = self._context.get_messages()
            tools = self._executor._registry.list_tools() if len(self._executor._registry) > 0 else None

            async for event in self._router.stream(messages, tools):
                # 检查点：每个 chunk 后检查取消令牌
                if self._cancel_token and self._cancel_token.is_cancelled:
                    raise AgentCancelledError(
                        self._cancel_token.reason or CancelReason.USER_CANCEL,
                        "LLM generation cancelled"
                    )
                
                if not content_started and event.type == "text_delta" and event.text:
                    content_started = True
                    await self._hook.emit("state_change", ctx, state="running")

                processor.process(event)
                await parser.dispatch(event, ctx)

            return processor.result()
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    async def _execute_tools_with_watchdog(self, ctx: HookContext, tool_calls) -> list:
        """带看门狗的工具批量执行。"""
        watchdog = asyncio.create_task(
            self._watchdog(ctx, self._tool_batch_timeout, "tool_execution")
        )
        try:
            results = await self._executor.execute_batch(tool_calls)
            return results
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    async def run(
        self,
        ctx: HookContext,
        *,
        stream_to_ui: AsyncIterator[StreamEvent] | None = None,
    ) -> StreamResult:
        """
        执行完整的 ReAct 循环，返回最终的 StreamResult。
        """
        final_result: StreamResult | None = None

        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1

                # 检查点 1：每轮迭代开始前检查取消
                if self._cancel_token:
                    await self._cancel_token.check()

                await self._hook.emit("iteration_start", ctx)

                # 审计内联：iteration_start
                if self._audit:
                    await self._audit.log_event("iteration_start", ctx.snapshot(), session_id=ctx.session_id)

                # 1. 流式调用 Provider（带看门狗）
                processor = StreamProcessor()
                parser = StreamParser(self._hook)

                await self._hook.emit("provider_call_start", ctx)
                await self._hook.emit("state_change", ctx, state="thinking")

                # 审计内联：provider_call_start
                if self._audit:
                    await self._audit.log_event("provider_call_start", ctx.snapshot(), session_id=ctx.session_id)

                if self._hook.wants_streaming():
                    await self._hook.emit("stream_start", ctx)

                try:
                    result = await self._stream_with_watchdog(ctx, processor, parser)
                except AgentCancelledError:
                    raise  # 向上传递取消
                except Exception as e:
                    logger.error(f"Provider error in iteration {ctx.iteration}: {e}")
                    await self._hook.emit("state_change", ctx, state="error")
                    await self._hook.emit("error", ctx, error=e)
                    if self._audit:
                        await self._audit.log_event("error", {"error": str(e), "error_type": type(e).__name__}, session_id=ctx.session_id)
                    raise

                await self._hook.emit("provider_call_end",
                    ctx, stop_reason=result.stop_reason or "", usage=result.usage
                )

                # 审计内联：provider_call_end
                if self._audit:
                    await self._audit.log_event("provider_call_end", {
                        "stop_reason": result.stop_reason or "",
                        "usage": result.usage,
                    }, session_id=ctx.session_id)

                # 2. 写入 assistant 消息到上下文
                self._context.add_assistant_message(
                    content=result.text,
                    tool_calls=result.tool_calls if result.tool_calls else None,
                )

                # 3. 检查是否需要执行工具
                has_tools = bool(result.tool_calls)
                if self._hook.wants_streaming():
                    await self._hook.emit("stream_end", ctx, resuming=has_tools)

                if not has_tools:
                    final_result = result
                    await self._hook.emit("state_change", ctx, state="finished")
                    await self._hook.emit("iteration_end", ctx)
                    if self._audit:
                        await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)
                    break

                # 检查点 3：工具执行前检查取消
                if self._cancel_token:
                    await self._cancel_token.check()

                # 4. 并行执行工具（带看门狗）
                await self._hook.emit("state_change", ctx, state="waiting_tool")
                await self._hook.emit("before_execute_tools", ctx)
                for tc in result.tool_calls:
                    await self._hook.emit("tool_start",
                        ctx, tool_name=tc.name, args=tc.arguments, call_id=tc.id
                    )
                    # 审计内联：tool_start
                    if self._audit:
                        await self._audit.log_event("tool_start", {
                            "tool_name": tc.name, "call_id": tc.id,
                        }, session_id=ctx.session_id)

                tool_results = await self._execute_tools_with_watchdog(ctx, result.tool_calls)
                await self._hook.emit("after_execute_tools", ctx)

                # 5. 写入工具结果到上下文
                for tc, tr in zip(result.tool_calls, tool_results):
                    msg_result = MsgToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        content=tr.content,
                    )
                    self._context.add_tool_result(tc.id, msg_result)

                    latency = tr.metadata.get("latency_ms", 0)
                    if tr.is_error:
                        if "denied_by" in tr.metadata:
                            await self._hook.emit("safety_blocked",
                                ctx,
                                rule=tr.metadata["denied_by"],
                                reason=str(tr.content),
                                action="deny",
                                call_id=tc.id,
                                tool_name=tc.name,
                            )
                            if self._audit:
                                await self._audit.emit_safety(
                                    decision="deny",
                                    rule_name=tr.metadata["denied_by"],
                                    reason=str(tr.content),
                                    session_id=ctx.session_id,
                                )
                        await self._hook.emit("tool_error",
                            ctx, tool_name=tc.name,
                            error=Exception(tr.content), call_id=tc.id,
                        )
                        if self._audit:
                            await self._audit.log_event("tool_error", {
                                "tool_name": tc.name, "call_id": tc.id,
                                "error": str(tr.content),
                            }, session_id=ctx.session_id)
                    else:
                        await self._hook.emit("tool_end",
                            ctx, tool_name=tc.name, result=tr,
                            call_id=tc.id, latency_ms=latency,
                        )
                        if self._audit:
                            await self._audit.log_event("tool_end", {
                                "tool_name": tc.name, "call_id": tc.id,
                                "latency_ms": latency,
                                "is_error": False,
                            }, session_id=ctx.session_id)

                await self._hook.emit("iteration_end", ctx)
                if self._audit:
                    await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)

            else:
                logger.warning(f"AgentLoop reached max iterations ({self._max_iterations})")
                final_result = StreamResult(
                    text="达到最大迭代次数限制，终止执行。",
                    stop_reason="max_iterations",
                )

        except AgentCancelledError as e:
            # 取消处理
            cancel_msg = f"[系统] 操作已取消 — {e.reason.value}: {e.detail}"
            logger.info(f"AgentLoop cancelled: {e}")

            # 注入取消信息到上下文
            self._context.add_assistant_message(
                content=cancel_msg, tool_calls=None
            )

            # 触发 Hook 事件
            await self._hook.emit("cancelled", ctx,
                reason=e.reason.value, detail=e.detail
            )

            # 审计内联
            if self._audit:
                await self._audit.emit_cancelled(
                    reason=e.reason.value,
                    detail=e.detail,
                    session_id=ctx.session_id,
                    iteration=ctx.iteration,
                )

            # 返回带取消原因的 StreamResult
            return StreamResult(
                text=cancel_msg,
                stop_reason=f"cancelled:{e.reason.value}",
            )

        return final_result or StreamResult(stop_reason="unknown")