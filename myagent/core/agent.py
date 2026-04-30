"""
Agent：顶层编排类。
组装 ProviderRouter + ContextManager + ToolExecutor + AgentLoop + HookManager 体系。
提供 run() 方法作为对外的统一入口。

重构要点：
- CompositeHook → HookManager
- 审计内联：Agent 直接持有 AuditLogger
- SessionManager 管理会话列表
- CancellationToken 全链路取消
"""
import asyncio
from uuid import uuid4

from myagent.providers.router import ProviderRouter
from myagent.context.manager import ContextManager
from myagent.context.state import StateStore, AgentState
from myagent.tools.registry import ToolRegistry
from myagent.tools.executor import ToolExecutor
from myagent.tools.idempotency import IdempotencyCache
from myagent.core.hook import HookContext, HookManager
from myagent.core.loop import AgentLoop
from myagent.core.stream import StreamResult
from myagent.core.session import SessionManager
from myagent.core.cancellation import CancellationToken, CancelReason, AgentCancelledError
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.config import TimeoutConfig
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class Agent:
    """
    顶层 Agent 编排类。
    职责：
    1. 组装所有子系统
    2. 管理 session 生命周期
    3. 提供 run(user_input) → 最终文本 的统一入口
    4. 暴露取消入口 request_cancel()
    """

    def __init__(
        self,
        *,
        provider_router: ProviderRouter,
        context: ContextManager | None = None,
        tool_registry: ToolRegistry | None = None,
        hooks: HookManager | None = None,
        state_store: StateStore | None = None,
        max_iterations: int = 50,
        system_prompt: str | None = None,
        # ── Phase 2 可选组件 ──
        safety_guard: "SafetyGuard | None" = None,
        secret_manager: "SecretManager | None" = None,
        hitl_callback=None,
        # ── 审计内联 ──
        audit_logger: AuditLogger | None = None,
        # ── 超时配置 ──
        timeout_config: TimeoutConfig | None = None,
        # ── 会话 ID ──
        session_id: str | None = None,
    ):
        self._router = provider_router
        self._context = context or ContextManager()
        self._tool_registry = tool_registry or ToolRegistry()
        self._hooks = hooks or HookManager()
        self._state_store = state_store
        self._max_iterations = max_iterations
        self._session_id = session_id or uuid4().hex[:16]
        self._safety_guard = safety_guard
        self._secret_manager = secret_manager
        self._audit = audit_logger
        self._timeout_config = timeout_config or TimeoutConfig()

        # CancellationToken（每次 run 创建新的）
        self._cancel_token: CancellationToken | None = None

        # SessionManager（内部创建，也可外部注入 state_store）
        self._session_manager = SessionManager(state_store=state_store)
        self._session_manager.create_session(session_id=session_id)
        self._session_id = self._session_manager.active_id or self._session_id

        # 初始化幂等缓存
        self._idempotency = IdempotencyCache()

        # 初始化工具执行器（Phase 2 增强版）
        self._executor = ToolExecutor(
            registry=self._tool_registry,
            idempotency_cache=self._idempotency,
            safety_guard=safety_guard,
            secret_manager=secret_manager,
            hitl_callback=hitl_callback,
        )

        # 初始化 ReAct 循环引擎
        self._loop = AgentLoop(
            provider_router=self._router,
            context=self._context,
            executor=self._executor,
            hook=self._hooks,
            max_iterations=max_iterations,
            llm_timeout=self._timeout_config.llm_generation,
            tool_batch_timeout=self._timeout_config.tool_batch,
            iteration_timeout=self._timeout_config.iteration,
            audit_logger=self._audit,
        )

        # 设置 system prompt
        if system_prompt:
            self._context.set_system(system_prompt)

        # 注册状态同步：Turn 的 state_change hook 事件 → 更新 Session 状态
        self._hooks.on("state_change", self._on_session_state_change)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def tools(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    @property
    def session(self) -> SessionManager:
        return self._session_manager

    def add_tool(self, tool) -> None:
        """注册工具。"""
        self._tool_registry.register(tool)

    def add_hook(self, hook) -> None:
        """追加 Hook（兼容旧接口，推荐使用 hooks.register()）。"""
        self._hooks.register(hook)

    async def _on_session_state_change(self, ctx, state):
        """Hook 回调：当 Turn 发射 state_change 事件时更新 Session 状态。"""
        session = self._session_manager.active
        if session:
            try:
                session.agent_state = AgentState(state)
            except ValueError:
                pass  # 忽略未知的状态值

    async def _persist(self, state=None, metadata=None):
        """统一持久化入口：通过 Session 写入状态和消息历史。"""
        session = self._session_manager.active
        if session:
            await session.persist(self._context.messages, state, metadata)

    def request_cancel(
        self,
        reason: CancelReason = CancelReason.USER_CANCEL,
        detail: str = "",
    ) -> None:
        """供外部（CLI/WebSocket）调用的取消入口。"""
        if self._cancel_token:
            self._cancel_token.cancel(reason, detail)
            logger.info(f"Cancel requested: {reason.value} — {detail}")

    async def run(self, user_input: str) -> str:
        """
        对外统一入口：接收用户输入，返回最终文本。
        """
        # 每次 run 创建新的 CancellationToken
        self._cancel_token = CancellationToken()
        self._loop._cancel_token = self._cancel_token

        ctx = HookContext(
            session_id=self._session_id,
            agent_id="main",
        )

        # 审计内联：session_start
        if self._audit:
            await self._audit.log_event("session_start", ctx.snapshot(), session_id=ctx.session_id)

        try:
            # 写入用户消息
            self._context.add_user_message(user_input)

            # 审计内联：turn_start
            if self._audit:
                await self._audit.log_event("turn_start", ctx.snapshot(), session_id=ctx.session_id)

            # 执行 ReAct 循环
            result: StreamResult = await self._loop.run(ctx)

            # 内容后处理
            final_content = self._hooks.finalize_content(ctx, result.text)

            # 审计内联：turn_end
            if self._audit:
                await self._audit.log_event("turn_end", ctx.snapshot(), session_id=ctx.session_id)

            # 审计内联：session_end
            if self._audit:
                await self._audit.log_event("session_end", {
                    "exit_reason": result.stop_reason or "completed",
                }, session_id=ctx.session_id)

            # 持久化状态
            await self._persist(
                AgentState.IDLE,
                {"stop_reason": result.stop_reason or "completed"}
            )

            return final_content or ""

        except AgentCancelledError as e:
            # AgentLoop 内部已处理，这里只做持久化
            logger.info(f"Agent run cancelled: {e}")
            await self._persist(AgentState.IDLE, {
                "cancelled": True,
                "cancel_reason": e.reason.value,
            })
            raise

        except asyncio.CancelledError as e:
            logger.info("Agent run task cancelled by asyncio.")
            await self._persist(AgentState.IDLE, {
                "cancelled": True,
                "cancel_reason": "asyncio_cancelled",
            })
            raise

        except Exception as e:
            logger.error(f"Agent run error: {e}")
            await self._hooks.emit("error", ctx, error=e)
            if self._audit:
                await self._audit.log_event("error", {
                    "error": str(e), "error_type": type(e).__name__,
                }, session_id=ctx.session_id)
            await self._persist(AgentState.ERROR)
            raise

    async def restore_session(self, session_id: str) -> None:
        """从 StateStore 恢复会话。"""
        if not self._state_store:
            raise RuntimeError("No StateStore configured")
        
        # 使用 SessionManager 恢复会话
        session = await self._session_manager.restore_session(session_id)
        self._session_id = session.id
        
        # 恢复上下文
        messages = await session.load_messages()
        self._context.restore_from(messages)
        
        logger.info(f"Session restored: {session_id}")