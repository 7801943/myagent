"""
AgentLoop：ReAct 循环引擎（Turn 抽象版）。
Loop 退化为极简 dispatcher：创建 Turn → 执行 → 根据结果决定下一个 Turn。

保留在 dispatcher 层的职责：
- cancelled 异常处理（跨 Turn 的全局事件）
- max_iterations 控制
"""
from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.core.hook import HookContext, HookManager
from myagent.core.stream import StreamResult
from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason
from myagent.core.turns import TurnKind, TurnResult, ModelTurn, ToolTurn
from myagent.tools.executor import ToolExecutor
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class AgentLoop:
    """
    ReAct 循环引擎（Turn dispatcher 版）。
    
    while True:
        1. 创建并执行 ModelTurn（LLM 流式生成）
        2. 如果有 tool_calls → 创建并执行 ToolTurn（工具批量执行）
        3. 回到步骤 1，直到 ModelTurn 无 tool_calls 或达到 max_iterations
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

    def _create_turn(self, kind: TurnKind):
        """工厂方法：根据 TurnKind 创建对应的 Turn 实例。"""
        if kind == TurnKind.MODEL:
            return ModelTurn(
                provider_router=self._router,
                context=self._context,
                executor=self._executor,
                hooks=self._hook,
                cancel_token=self._cancel_token,
                audit=self._audit,
                watchdog_timeout=self._llm_timeout,
            )
        elif kind == TurnKind.TOOL:
            return ToolTurn(
                context=self._context,
                executor=self._executor,
                hooks=self._hook,
                cancel_token=self._cancel_token,
                audit=self._audit,
                watchdog_timeout=self._tool_batch_timeout,
            )
        else:
            raise ValueError(f"Unknown TurnKind: {kind}")

    async def run(self, ctx: HookContext) -> StreamResult:
        """
        执行完整的 ReAct 循环，返回最终的 StreamResult。
        """
        final_result: StreamResult | None = None

        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1

                # 迭代级取消检查
                if self._cancel_token:
                    await self._cancel_token.check()

                # → iteration_start 留在 dispatcher 层
                if self._audit:
                    await self._audit.log_event("iteration_start", ctx.snapshot(), session_id=ctx.session_id)

                # ── ModelTurn ──
                model_turn = self._create_turn(TurnKind.MODEL)
                model_result: TurnResult = await model_turn.execute(ctx)

                if model_result.next_turn is None:
                    # 无工具调用，循环结束
                    final_result = model_result.stream_result
                    await self._hook.emit("state_change", ctx, state="idle")
                    if self._audit:
                        await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)
                    break

                # ── ToolTurn ──
                tool_turn = self._create_turn(TurnKind.TOOL)
                await tool_turn.execute(ctx, model_result.data)

                # → iteration_end 留在 dispatcher 层
                if self._audit:
                    await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)

            else:
                # for-else: 达到 max_iterations
                logger.warning(f"AgentLoop reached max iterations ({self._max_iterations})")
                final_result = StreamResult(
                    text="达到最大迭代次数限制，终止执行。",
                    stop_reason="max_iterations",
                )

        except AgentCancelledError as e:
            # → cancelled 处理留在 dispatcher 层
            cancel_msg = f"[系统] 操作已取消 — {e.reason.value}: {e.detail}"
            logger.info(f"AgentLoop cancelled: {e}")

            self._context.add_assistant_message(
                content=cancel_msg, tool_calls=None
            )

            if self._audit:
                await self._audit.emit_cancelled(
                    reason=e.reason.value,
                    detail=e.detail,
                    session_id=ctx.session_id,
                    iteration=ctx.iteration,
                )

            return StreamResult(
                text=cancel_msg,
                stop_reason=f"cancelled:{e.reason.value}",
            )

        return final_result or StreamResult(stop_reason="unknown")