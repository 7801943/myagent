"""
AgentLoop：ReAct 循环的核心引擎。

职责：管理 LLM ↔ 工具的迭代交互，直到满足终止条件。
不参与会话生命周期或审计内联，这些由 Agent 处理。
"""
import asyncio
from typing import Any

from myagent.core.hook import HookContext, HookManager
from myagent.core.stream import StreamResult
from myagent.core.cancellation import (
    CancellationToken, CancelReason, AgentCancelledError,
)
from myagent.context.manager import ContextManager
from myagent.context.message import ToolCall as MsgToolCall
from myagent.context.message import ToolResult as MsgToolResult
from myagent.providers.router import ProviderRouter
from myagent.providers.base import StreamEvent, AllProvidersFailedError
from myagent.tools.executor import ToolExecutor
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.logging import get_logger
from myagent.utils.timeout import Timeout

logger = get_logger(__name__)

class AgentLoop:
    """
    ReAct 循环引擎：LLM → 工具执行 → 结果回填 → 继续/停止。
    不可取消区域 / 状态持久化由 Agent 层负责，此处仅关注循环逻辑。
    """

    def __init__(
        self,
        provider_router: ProviderRouter,
        context: ContextManager,
        executor: ToolExecutor,
        hook: HookManager,
        max_iterations: int = 50,
        llm_timeout: float = 120.0,
        tool_batch_timeout: float = 300.0,
        iteration_timeout: float = 600.0,
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
        self._audit = audit_logger
        self._cancel_token: CancellationToken | None = None

    async def run(self, ctx: HookContext) -> StreamResult:
        """执行 ReAct 循环。"""
        current_iteration = 0
        accumulated_text = ""
        final_stop_reason = None

        while current_iteration < self._max_iterations:
            # 检查取消
            if self._cancel_token and self._cancel_token.is_cancelled:
                raise AgentCancelledError(self._cancel_token.reason, self._cancel_token.detail)

            current_iteration += 1
            logger.debug(f"=== Iteration {current_iteration} ===")

            await self._hook.emit("iteration_start", ctx, iteration=current_iteration)

            # 1. 构建消息 + 调用 LLM
            messages = self._context.messages

            try:
                async with Timeout(self._llm_timeout):
                    result = await self._stream_and_collect(ctx, messages)
            except asyncio.TimeoutError:
                logger.warning(f"Iteration {current_iteration}: LLM timeout")
                text, stop_reason, tool_calls = "", "llm_timeout", []
                await self._hook.emit("timeout_warning", ctx, message=f"LLM 超时 (>{self._llm_timeout}s)")
            else:
                text, stop_reason, tool_calls = result

            # 2. 追加 assistant 消息（包含文本 + tool_calls）
            self._context.add_assistant_message(
                content=text,
                tool_calls=[MsgToolCall(id=tc.id, name=tc.name, arguments=tc.arguments) for tc in tool_calls],
            )

            accumulated_text += text

            # 3. 判断终止条件
            if not tool_calls:
                final_stop_reason = stop_reason or "end_turn"
                logger.debug(f"Iteration {current_iteration}: 无工具调用，终止")
                await self._hook.emit("iteration_end", ctx, iteration=current_iteration)
                break

            # 4. 执行工具（异步并发）
            if stop_reason in ("tool_use", "tool_calls"):
                for tc in tool_calls:
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

                # 5. 写入工具结果到上下文（修复 P0-4: 透传 is_error、metadata，避免 tool_call_id 在 metadata 中重复）
                for tc, tr in zip(result.tool_calls, tool_results):
                    # 剥离 metadata 中与顶层字段重复的 tool_call_id
                    clean_metadata = dict(tr.metadata) if tr.metadata else {}
                    clean_metadata.pop("tool_call_id", None)

                    msg_result = MsgToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        content=tr.content,
                        is_error=tr.is_error,
                        metadata=clean_metadata,
                    )
                    self._context.add_tool_result(tc.id, msg_result)

                    latency = clean_metadata.get("latency_ms", 0)
                    if tr.is_error:
                        if "denied_by" in clean_metadata:
                            await self._hook.emit("safety_blocked",
                                ctx,
                                rule=clean_metadata["denied_by"],
                                reason=str(tr.content),
                                action="deny",
                                call_id=tc.id,
                                tool_name=tc.name,
                            )
                            if self._audit:
                                await self._audit.emit_safety(
                                    decision="deny",
                                    rule_name=clean_metadata["denied_by"],
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

                await self._hook.emit("iteration_end", ctx, iteration=current_iteration)
            else:
                final_stop_reason = stop_reason or "end_turn"
                logger.debug(f"Iteration {current_iteration}: stop_reason={stop_reason}")
                await self._hook.emit("iteration_end", ctx, iteration=current_iteration)
                break

        else:
            final_stop_reason = "max_iterations"
            logger.warning(f"达到最大迭代次数 {self._max_iterations}")

        return StreamResult(
            text=accumulated_text,
            stop_reason=final_stop_reason,
        )

    async def _stream_and_collect(
        self,
        ctx: HookContext,
        messages: list,
    ) -> tuple[str, str | None, list]:
        """
        调用 LLM 流式 API，收集文本增量和工具调用。
        返回 (text, stop_reason, tool_calls)。
        """
        accumulated_text = ""
        tool_calls: list = []
        stop_reason = None

        try:
            async for event in self._router.stream(messages):
                if event.type == "text_delta":
                    accumulated_text += event.text
                    await self._hook.emit("stream", ctx, delta=event.text)

                elif event.type == "thinking_delta":
                    await self._hook.emit("thinking_stream", ctx, delta=event.text)

                elif event.type == "tool_call_start":
                    pass  # 暂存

                elif event.type == "tool_call_delta":
                    pass  # 增量收集已在 AnthropicProvider 中完成

                elif event.type == "tool_call_end":
                    tool_calls.append(event)

                elif event.type == "message_end":
                    stop_reason = event.stop_reason

                elif event.type == "provider_failover":
                    logger.warning(f"Provider failover: {event.meta}")
                    await self._hook.emit("provider_failover",
                        ctx, meta=event.meta,
                    )

        except AllProvidersFailedError as e:
            logger.error(f"所有 Provider 均失败: {e}")
            raise

        except Exception as e:
            logger.error(f"Stream error: {e}")
            raise

        return accumulated_text, stop_reason, tool_calls

    async def _execute_tools_with_watchdog(
        self,
        ctx: HookContext,
        tool_calls: list,
    ) -> list:
        """执行工具调用，带超时和看门狗机制。"""
        if not tool_calls:
            return []

        async def execute_one(tc) -> "ToolResult":
            try:
                async with Timeout(self._tool_batch_timeout):
                    return await self._executor.execute_tool(tc)
            except asyncio.TimeoutError:
                logger.warning(f"Tool {tc.name} timeout (>{self._tool_batch_timeout}s)")
                from myagent.tools.base import ToolResult
                return ToolResult(
                    content=f"执行超时 (>{self._tool_batch_timeout}s)",
                    is_error=True,
                    metadata={"latency_ms": 0, "tool_call_id": tc.id},
                )

        tasks = [execute_one(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks)
        return results
