"""
Agent：顶层配置持有者 + 会话工厂。
重构后 Agent 变薄，不再持有 ContextManager/AgentLoop。
这些职责转移到 Session。

Agent 保留的职责：
1. 持有共享组件（ProviderRouter、ToolExecutor、HookManager、AuditLogger）
2. 创建和管理 Session
3. 提供便捷入口 run(user_input)
4. 暴露取消入口 request_cancel()
"""
import asyncio
from typing import Callable, Awaitable
from uuid import uuid4

from myagent.providers.router import ProviderRouter
from myagent.tools.registry import ToolRegistry
from myagent.tools.executor import ToolExecutor
from myagent.tools.executor import IdempotencyCache
from myagent.tools.hot_reloader import HotReloader
from myagent.core.hook import HookManager
from myagent.core.session import Session
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.config import TimeoutConfig
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class Agent:
    """
    顶层 Agent 类（变薄版）。
    配置持有者 + 会话工厂 + 便捷入口。
    """

    def __init__(
        self,
        *,
        provider_router: ProviderRouter,
        tool_registry: ToolRegistry | None = None,
        hooks: HookManager | None = None,
        state_store=None,
        max_iterations: int = 50,
        system_prompt: str | None = None,
        # ── Phase 2 可选组件 ──
        safety_guard=None,
        secret_manager=None,
        # ── 审计内联 ──
        audit_logger: AuditLogger | None = None,
        # ── 超时配置 ──
        timeout_config: TimeoutConfig | None = None,
        # ── 人工审批 handler ──
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
        # ── ContextManager 配置 ──
        max_tokens_budget: int = 200000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
        # ── 工具热加载 ──
        hot_reloader: HotReloader | None = None,
    ):
        # 共享组件
        self._router = provider_router
        self._tool_registry = tool_registry or ToolRegistry()
        self._hooks = hooks or HookManager()
        self._state_store = state_store
        self._audit = audit_logger
        self._timeout_config = timeout_config or TimeoutConfig()
        self._approval_handler = approval_handler
        self._hot_reloader = hot_reloader

        # 配置
        self._max_iterations = max_iterations
        self._system_prompt = system_prompt
        self._max_tokens_budget = max_tokens_budget
        self._context_window_size = context_window_size
        self._tool_result_max_chars = tool_result_max_chars

        # 共享 ToolExecutor（跨所有 Session）
        self._idempotency = IdempotencyCache()
        self._executor = ToolExecutor(
            registry=self._tool_registry,
            idempotency_cache=self._idempotency,
            safety_guard=safety_guard,
            secret_manager=secret_manager,
        )

        # 会话管理
        self._sessions: dict[str, Session] = {}
        self._active: Session | None = None

    @property
    def session_id(self) -> str | None:
        return self._active.id if self._active else None

    @property
    def context(self):
        """兼容性属性：返回活跃会话的 ContextManager。"""
        return self._active.context if self._active else None

    @property
    def tools(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    @property
    def active_session(self) -> Session | None:
        return self._active

    @property
    def sessions(self) -> dict[str, Session]:
        return dict(self._sessions)

    def add_tool(self, tool) -> None:
        """注册工具。"""
        self._tool_registry.register(tool)

    def add_hook(self, hook) -> None:
        """追加 Hook（兼容旧接口，推荐使用 hooks.register()）。"""
        self._hooks.register(hook)

    def create_session(self, session_id: str | None = None) -> Session:
        """创建新会话。"""
        session = Session(
            session_id=session_id,
            router=self._router,
            executor=self._executor,
            hooks=self._hooks,
            audit=self._audit,
            timeout_config=self._timeout_config,
            max_iterations=self._max_iterations,
            system_prompt=self._system_prompt,
            state_store=self._state_store,
            approval_handler=self._approval_handler,
            max_tokens_budget=self._max_tokens_budget,
            context_window_size=self._context_window_size,
            tool_result_max_chars=self._tool_result_max_chars,
        )
        self._sessions[session.id] = session
        self._active = session
        logger.info(f"Session created: {session.id}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        """获取指定会话。"""
        return self._sessions.get(session_id)

    def set_active_session(self, session_id: str) -> Session:
        """设置活跃会话。"""
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"Session not found: {session_id}")
        self._active = session
        return session

    async def run(self, user_input: str) -> str:
        """
        便捷入口：在活跃会话上执行。
        如果没有活跃会话，自动创建一个。
        首次调用时自动启动热加载器（如果已配置且未运行）。
        """
        if not self._active:
            self.create_session()

        # 懒启动热加载器（确保在 event loop 中）
        if self._hot_reloader and not self._hot_reloader.is_running:
            await self.start_hot_reload()

        return await self._active.run(user_input)

    # ── 热加载生命周期 ──

    async def start_hot_reload(self) -> None:
        """启动工具热加载器。"""
        if self._hot_reloader:
            await self._hot_reloader.start()

    async def stop_hot_reload(self) -> None:
        """停止工具热加载器。"""
        if self._hot_reloader:
            await self._hot_reloader.stop()

    @property
    def hot_reloader(self) -> HotReloader | None:
        """返回热加载器实例（供外部查询状态）。"""
        return self._hot_reloader

    def request_cancel(
        self,
        reason: str = "user_cancelled",
        detail: str = "",
    ) -> None:
        """供外部（CLI/WebSocket）调用的取消入口。"""
        if self._active:
            self._active.request_cancel(reason, detail)
            logger.info(f"Cancel requested: {reason} — {detail}")

    async def restore_session(self, session_id: str) -> Session:
        """从 StateStore 恢复会话。"""
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        session = await Session.restore(
            session_id=session_id,
            state_store=self._state_store,
            router=self._router,
            executor=self._executor,
            hooks=self._hooks,
            audit=self._audit,
            timeout_config=self._timeout_config,
            max_iterations=self._max_iterations,
            system_prompt=self._system_prompt,
            approval_handler=self._approval_handler,
        )

        # 恢复上下文消息
        messages = await session.load_messages()
        if messages:
            session._context.restore_from(messages)

        self._sessions[session.id] = session
        self._active = session
        logger.info(f"Session restored: {session_id}")
        return session