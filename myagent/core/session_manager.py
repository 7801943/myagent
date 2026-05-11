"""
SessionManager：多用户会话管理 + 用户隔离。

Phase 1 新增：
  - UserContext 数据类（用户身份+凭证）
  - SessionManager 管理 Session 的 CRUD
  - 每用户维护一个 Agent 实例
  - 会话持久化（StateStore 集成）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from myagent.core.session import Session
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.core.agent import Agent
    from myagent.core.hook import HookManager
    from myagent.context.state import StateStore

logger = get_logger(__name__)


@dataclass
class UserContext:
    """用户会话上下文。"""
    user_id: str
    username: str = ""
    permissions: list[str] = field(default_factory=list)
    credentials: dict = field(default_factory=dict)  # 下载 token 等
    preferences: dict = field(default_factory=dict)   # 用户配置


class SessionManager:
    """
    顶层会话管理器。
    职责：
    1. 管理 Session 的 CRUD（create/get/list/delete）
    2. 每用户维护一个 Agent 实例
    3. 会话持久化（StateStore 集成）
    4. 用户隔离
    """

    def __init__(self, factory, state_store: "StateStore | None" = None):
        """
        Args:
            factory: AgentFactory（用于创建 Agent 实例）
            state_store: 可选的 StateStore（用于会话持久化）
        """
        self._factory = factory
        self._state_store = state_store
        self._sessions: dict[str, Session] = {}
        self._user_agents: dict[str, "Agent"] = {}  # user_id → Agent

    def _get_or_create_agent(
        self,
        user_id: str,
        hooks: "HookManager",
        approval_handler=None,
        no_safety: bool = False,
    ) -> "Agent":
        """获取或为用户创建 Agent 实例。"""
        if user_id not in self._user_agents:
            agent = self._factory.create_agent(
                hooks=hooks,
                approval_handler=approval_handler,
                no_safety=no_safety,
            )
            self._user_agents[user_id] = agent
            logger.info(f"Agent created for user: {user_id}")
        return self._user_agents[user_id]

    def create_session(
        self,
        user: UserContext,
        session_id: str | None = None,
        hooks: "HookManager | None" = None,
        approval_handler=None,
        system_prompt: str | None = None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
        no_safety: bool = False,
    ) -> Session:
        """创建新会话。"""
        if hooks is None:
            from myagent.core.hook import HookManager
            hooks = HookManager()

        agent = self._get_or_create_agent(user.user_id, hooks, approval_handler, no_safety=no_safety)

        # 如果未指定 system_prompt，使用 factory 的默认值
        effective_prompt = system_prompt or self._factory.system_prompt

        session = Session(
            session_id=session_id,
            agent=agent,
            user=user,
            state_store=self._state_store,
            system_prompt=effective_prompt,
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
        )
        self._sessions[session.id] = session
        logger.info(f"Session created: {session.id} for user: {user.user_id}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        """获取会话。"""
        return self._sessions.get(session_id)

    async def restore_session(
        self,
        session_id: str,
        user: UserContext,
        hooks: "HookManager | None" = None,
        approval_handler=None,
        max_tokens_budget: int = 100000,
        context_window_size: int = 128000,
        tool_result_max_chars: int = 100000,
    ) -> Session:
        """从 StateStore 恢复会话。

        流程：
        1. 从 state_store 加载 session 数据
        2. 获取/创建用户的 Agent 实例
        3. 创建 Session 对象
        4. 恢复 ContextManager（消息历史）
        """
        if not self._state_store:
            raise RuntimeError("No StateStore configured")

        if hooks is None:
            from myagent.core.hook import HookManager
            hooks = HookManager()

        agent = self._get_or_create_agent(user.user_id, hooks, approval_handler)

        # 加载会话状态
        agent_run_state, metadata = await self._state_store.load_state(session_id)

        # 创建 Session
        session = Session(
            session_id=session_id,
            agent=agent,
            user=user,
            state_store=self._state_store,
            max_tokens_budget=max_tokens_budget,
            context_window_size=context_window_size,
            tool_result_max_chars=tool_result_max_chars,
        )
        session.agent_run_state = agent_run_state
        session.metadata = metadata

        # 恢复消息历史
        messages = await self._state_store.load_messages(session_id)
        if messages:
            session._context.restore_from(messages)

        self._sessions[session.id] = session
        logger.info(f"Session restored: {session_id}")
        return session

    async def delete_session(self, session_id: str) -> None:
        """删除会话。"""
        session = self._sessions.pop(session_id, None)
        if session and self._state_store:
            await self._state_store.clear_session(session_id)
        logger.info(f"Session deleted: {session_id}")

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        """列出会话。如果提供了 user_id，只返回该用户的会话。"""
        if self._state_store:
            sessions = await self._state_store.list_all_sessions()
            if user_id:
                # TODO: 按 user_id 过滤（需要 metadata 中存储 user_id）
                pass
            return sessions
        # 无 StateStore 时，从内存返回
        result = []
        for sid, session in self._sessions.items():
            result.append({
                "session_id": sid,
                "agent_state": session.agent_run_state.value if session.agent_run_state else "idle",
                "session_state": session.session_state.value if session.session_state else "active",
                "metadata": session.metadata,
            })
        return result

    async def get_session_messages(self, session_id: str) -> list:
        """获取会话消息列表（供 ws_handler 展示会话预览）。"""
        session = self._sessions.get(session_id)
        if session:
            return session._context.messages
        if self._state_store:
            return await self._state_store.load_messages(session_id)
        return []