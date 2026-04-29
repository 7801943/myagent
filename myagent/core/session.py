"""
Session + SessionManager：会话管理。
Agent 持有一个 SessionManager 来管理多会话列表、活跃会话切换。
与 StateStore 配合实现持久化。
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from myagent.context.state import AgentState
from myagent.utils.logging import get_logger

if TYPE_CHECKING:
    from myagent.context.state import StateStore

logger = get_logger(__name__)


class Session:
    """单个会话的数据模型。"""

    def __init__(
        self,
        session_id: str | None = None,
        state_store: "StateStore | None" = None,
    ):
        self.id: str = session_id or uuid4().hex[:16]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.agent_state: AgentState = AgentState.IDLE
        self.metadata: dict = {}
        self._state_store = state_store

    async def save(self, messages=None) -> None:
        """持久化会话状态和消息。"""
        if self._state_store:
            await self._state_store.save_state(
                self.id, self.agent_state, self.metadata
            )
            if messages is not None:
                await self._state_store.save_messages(self.id, messages)

    @classmethod
    async def restore(cls, session_id: str, state_store: "StateStore") -> "Session":
        """从 StateStore 恢复会话。"""
        state, metadata = await state_store.load_state(session_id)
        session = cls(session_id=session_id, state_store=state_store)
        session.agent_state = state
        session.metadata = metadata
        return session


class SessionManager:
    """
    管理会话列表。Agent 持有一个 SessionManager。
    与 StateStore 配合实现持久化。
    """

    def __init__(self, state_store: "StateStore | None" = None):
        self._sessions: dict[str, Session] = {}
        self._active_session_id: str | None = None
        self._state_store = state_store

    def create_session(self, session_id: str | None = None) -> Session:
        """创建新会话并设为活跃。"""
        session = Session(session_id=session_id, state_store=self._state_store)
        self._sessions[session.id] = session
        self._active_session_id = session.id
        logger.info(f"Session created: {session.id}")
        return session

    def switch_session(self, session_id: str) -> Session:
        """切换到已有会话。"""
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id} not found")
        self._active_session_id = session_id
        return self._sessions[session_id]

    async def restore_session(self, session_id: str) -> Session:
        """从持久化存储恢复会话。"""
        if not self._state_store:
            raise RuntimeError("No StateStore configured for SessionManager")
        session = await Session.restore(session_id, self._state_store)
        self._sessions[session.id] = session
        self._active_session_id = session.id
        logger.info(f"Session restored: {session_id}")
        return session

    def list_sessions(self) -> list[Session]:
        """返回所有会话列表。"""
        return list(self._sessions.values())

    @property
    def active(self) -> Session | None:
        """返回当前活跃会话。"""
        if self._active_session_id and self._active_session_id in self._sessions:
            return self._sessions[self._active_session_id]
        return None

    @property
    def active_id(self) -> str | None:
        """返回当前活跃会话 ID。"""
        return self._active_session_id