"""
AgentLoop：ReAct 循环引擎（通用 Turn dispatcher 版）。
Loop 根据上一个 Turn 返回的 next_turn 动态路由到下一个 Turn。

状态机：
  MODEL → TOOL → MODEL（全部安全，无审批）
  MODEL → TOOL → HUMAN → TOOL → MODEL（部分需审批，审批后执行）
  MODEL → TOOL → HUMAN → MODEL（全部被拒绝）
  MODEL → None（无工具调用，结束）

保留在 dispatcher 层的职责：
- cancelled 异常处理（跨 Turn 的全局事件）
- max_iterations 控制
"""
from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.core.hook import HookContext, HookManager
from myagent.core.stream import StreamResult
from myagent.core.cancellation import CancellationToken, AgentCancelledError, CancelReason
from myagent.core.turns import TurnKind, TurnResult, ModelTurn, ToolTurn, HumanTurn
from myagent.tools.executor import ToolExecutor
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.logging import get_logger
from typing import Callable, Awaitable

logger = get_logger(__name__)


class AgentLoop:
    """
    ReAct 循环引擎（通用 Turn dispatcher 版）。
    
    根据 TurnResult.next_turn 动态路由到下一个 Turn：
    1. MODEL → LLM 生成，决定是否有 tool_calls
    2. TOOL → 执行工具，分拣结果（完成 vs 需审批）
    3. HUMAN → 等待人工审批（仅在有 needs_approval 时触发）
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
        human_approval_timeout: float = 300.0,
        iteration_timeout: float = 300.0,
        # ── 取消令牌（由 Session.run() 设置）──
        cancel_token: CancellationToken | None = None,
        # ── 审计日志（内联）──
        audit_logger: AuditLogger | None = None,
        # ── 人工审批 handler ──
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
    ):
        self._router = provider_router
        self._context = context
        self._executor = executor
        self._hook = hook
        self._max_iterations = max_iterations
        self._llm_timeout = llm_timeout
        self._tool_batch_timeout = tool_batch_timeout
        self._human_approval_timeout = human_approval_timeout
        self._iteration_timeout = iteration_timeout
        self._cancel_token = cancel_token
        self._audit = audit_logger
        self._approval_handler = approval_handler

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
        elif kind == TurnKind.HUMAN:
            return HumanTurn(
                context=self._context,
                hooks=self._hook,
                cancel_token=self._cancel_token,
                audit=self._audit,
                watchdog_timeout=self._human_approval_timeout,
                approval_handler=self._approval_handler,
            )
        else:
            raise ValueError(f"Unknown TurnKind: {kind}")

    async def run(self, ctx: HookContext) -> StreamResult:
        """
        执行完整的 ReAct 循环，返回最终的 StreamResult。
        通用 dispatcher：根据 TurnResult.next_turn 动态路由。
        """
        final_result: StreamResult | None = None
        current_kind = TurnKind.MODEL
        current_data = None
        previous_kind: TurnKind | None = None

        try:
            for iteration in range(self._max_iterations):
                ctx.iteration = iteration + 1

                # 迭代级取消检查
                if self._cancel_token:
                    await self._cancel_token.check()

                # → iteration_start 留在 dispatcher 层
                if self._audit:
                    await self._audit.log_event("iteration_start", ctx.snapshot(), session_id=ctx.session_id)

                # 创建并执行当前 Turn
                turn = self._create_turn(current_kind)
                result: TurnResult = await turn.execute(ctx, current_data, source=previous_kind)

                if result.next_turn is None:
                    # 循环结束
                    final_result = result.stream_result
                    await self._hook.emit("state_change", ctx, state="idle")
                    if self._audit:
                        await self._audit.log_event("iteration_end", ctx.snapshot(), session_id=ctx.session_id)
                    break

                # 路由到下一个 Turn
                previous_kind = result.kind
                current_kind = result.next_turn
                current_data = result.data

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