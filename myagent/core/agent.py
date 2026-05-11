"""
Agent：顶层配置持有者 + 会话工厂。
重构后 Agent 变薄，不再持有 ContextManager/AgentLoop。
这些职责转移到 Session。

Agent 保留的职责：
1. 持有共享组件（ProviderRouter、ToolManager、HookManager、AuditLogger）
2. 创建和管理 Session
3. 提供便捷入口 run(user_input)
4. 暴露取消入口 request_cancel()
5. 工具执行钩子（Safety + Secret + Idempotency）——从旧 executor 流水线外置至此
"""
import asyncio
import time
from collections import OrderedDict
from typing import Callable, Awaitable

from myagent.providers.router import ProviderRouter
from myagent.tools.manager import ToolManager
from myagent.tools.api import ToolResult
from myagent.core.hook import HookManager
from myagent.core.session import Session
from myagent.observability.audit_logger import AuditLogger
from myagent.utils.config import TimeoutConfig
from myagent.utils.logging import get_logger

logger = get_logger(__name__)


class IdempotencyCache:
    """幂等缓存：防止同一 tool_call_id 被重复执行。"""

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[ToolResult, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, tool_call_id: str) -> ToolResult | None:
        async with self._lock:
            if tool_call_id not in self._cache:
                return None
            result, ts = self._cache[tool_call_id]
            if time.monotonic() - ts > self._ttl_seconds:
                del self._cache[tool_call_id]
                return None
            self._cache.move_to_end(tool_call_id)
            return result

    async def store(self, tool_call_id: str, result: ToolResult) -> None:
        async with self._lock:
            self._cache[tool_call_id] = (result, time.monotonic())
            self._cache.move_to_end(tool_call_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()


class Agent:
    """
    顶层 Agent 类（V3 精简版）。
    配置持有者 + 会话工厂 + 便捷入口。
    """

    def __init__(
        self,
        *,
        provider_router: ProviderRouter,
        tool_manager: ToolManager | None = None,
        hooks: HookManager | None = None,
        state_store=None,
        max_iterations: int = 50,
        system_prompt: str | None = None,
        safety_guard=None,
        secret_manager=None,
        audit_logger: AuditLogger | None = None,
        timeout_config: TimeoutConfig | None = None,
        approval_handler: Callable[[list], Awaitable[list[bool]]] | None = None,
        max_tokens_budget: int = 200000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
    ):
        self._router = provider_router
        self._tool_manager = tool_manager or ToolManager()
        self._hooks = hooks or HookManager()
        self._state_store = state_store
        self._audit = audit_logger
        self._timeout_config = timeout_config or TimeoutConfig()
        self._approval_handler = approval_handler

        self._max_iterations = max_iterations
        self._system_prompt = system_prompt
        self._max_tokens_budget = max_tokens_budget
        self._context_window_size = context_window_size
        self._tool_result_max_chars = tool_result_max_chars

        self._safety_guard = safety_guard
        self._secret_manager = secret_manager

        self._idempotency = IdempotencyCache()

        self._sessions: dict[str, Session] = {}
        self._active: Session | None = None

    @property
    def session_id(self) -> str | None:
        return self._active.id if self._active else None

    @property
    def context(self):
        return self._active.context if self._active else None

    @property
    def tools(self) -> ToolManager:
        return self._tool_manager

    @property
    def hooks(self) -> HookManager:
        return self._hooks

    @property
    def active_session(self) -> Session | None:
        return self._active

    @property
    def sessions(self) -> dict[str, Session]:
        return dict(self._sessions)

    @property
    def tool_manager(self) -> ToolManager:
        return self._tool_manager

    def add_tool(self, tool) -> None:
        self._tool_manager.register(tool)

    def add_hook(self, hook) -> None:
        self._hooks.register(hook)

    def create_session(self, session_id: str | None = None) -> Session:
        """创建新会话。"""
        session = Session(
            session_id=session_id,
            router=self._router,
            tool_manager=self._tool_manager,
            tool_executor=self._execute_tool_batch,
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
        return self._sessions.get(session_id)

    def set_active_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"Session not found: {session_id}")
        self._active = session
        return session

    async def run(self, user_input: str | list) -> str:
        """执行一轮用户交互。

        Args:
            user_input: 纯文本字符串或 list[ContentBlock] 多模态内容。
        """
        if not self._active:
            self.create_session()

        if self._tool_manager and not self._tool_manager.is_running:
            await self._tool_manager.start()

        return await self._active.run(user_input)

    async def start_hot_reload(self) -> None:
        if self._tool_manager:
            await self._tool_manager.start()

    async def stop_hot_reload(self) -> None:
        if self._tool_manager:
            await self._tool_manager.stop()

    def request_cancel(self, reason: str = "user_cancelled", detail: str = "") -> None:
        if self._active:
            self._active.request_cancel(reason, detail)
            logger.info(f"Cancel requested: {reason} — {detail}")

    async def restore_session(self, session_id: str) -> Session:
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        session = await Session.restore(
            session_id=session_id,
            state_store=self._state_store,
            router=self._router,
            tool_manager=self._tool_manager,
            tool_executor=self._execute_tool_batch,
            hooks=self._hooks,
            audit=self._audit,
            timeout_config=self._timeout_config,
            max_iterations=self._max_iterations,
            system_prompt=self._system_prompt,
            approval_handler=self._approval_handler,
        )

        messages = await session.load_messages()
        if messages:
            session._context.restore_from(messages)

        self._sessions[session.id] = session
        self._active = session
        logger.info(f"Session restored: {session_id}")
        return session

    async def execute_tool(self, name: str, args: dict, tool_call_id: str, skip_safety: bool = False) -> ToolResult:
        """
        工具执行钩子包装（替代旧 executor 流水线）。

        钩子链：Safety → Idempotency → Secret → Execute
        """
        if not skip_safety and self._safety_guard:
            guard_result = await self._safety_guard.check_tool_call(name, args)
            if guard_result.is_denied:
                return ToolResult(
                    content=f"安全策略拒绝执行工具 '{name}': {guard_result.reason}",
                    is_error=True,
                    metadata={"denied_by": guard_result.rule_name, "tool_call_id": tool_call_id},
                )
            if guard_result.requires_hitl:
                return ToolResult(
                    content=f"工具 '{name}' 需要人工审批: {guard_result.reason}",
                    is_error=False,
                    metadata={
                        "needs_approval": True,
                        "reason": guard_result.reason,
                        "tool_call_id": tool_call_id,
                    },
                )
            if guard_result.decision and hasattr(guard_result.decision, 'value') and guard_result.decision.value == "rewrite" and guard_result.rewritten_args:
                args = guard_result.rewritten_args

        cached = await self._idempotency.get(tool_call_id)
        if cached is not None:
            logger.info(f"IdempotencyCache hit for {name} (call_id={tool_call_id})")
            return cached

        if self._secret_manager:
            args = self._secret_manager.inject_secrets(name, args)

        start = time.monotonic()
        result = await self._tool_manager.execute(name, **args)
        latency_ms = int((time.monotonic() - start) * 1000)
        result.metadata["latency_ms"] = latency_ms
        result.metadata["tool_call_id"] = tool_call_id

        await self._idempotency.store(tool_call_id, result)
        return result

    async def _execute_tool_batch(self, tool_calls: list, skip_safety: bool = False) -> list:
        """批量执行工具（AgentLoop 回调）。tool_calls = list[ToolCall]。"""
        tasks = [self.execute_tool(tc.name, tc.arguments, tc.id, skip_safety) for tc in tool_calls]
        return await asyncio.gather(*tasks)
