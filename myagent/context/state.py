"""
StateStore：会话状态持久化。
V3 核心变更 —— 显式状态机的持久化保障，使得断线恢复成为可能。

Phase 1 重构：
  - AgentState 重命名为 AgentRunState（Agent 运行时状态）
  - 新增 SessionState（会话生命周期状态：active/suspended/closed）
  - sessions 表新增 session_state 列

表结构：
  - sessions: session_id, agent_state(枚举), session_state(枚举), metadata(JSON), updated_at
  - messages: session_id, seq, message_json, created_at
  - pending_tool_calls: session_id, tool_call_id, tool_call_json, status, result_json
"""
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# [FIX] ToolResult → ToolResultMessage，消除与 api.py:ToolResult 的二义性
from myagent.context.message import Message, ToolCall, ToolResultMessage
from myagent.core.models import SessionState, AgentRunState, _LEGACY_STATE_MAP
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 向后兼容别名
AgentState = AgentRunState

class StateStore(ABC):
    """状态持久化抽象接口。"""

    @abstractmethod
    async def save_state(self, session_id: str, state: AgentRunState, metadata: dict | None = None, session_state: SessionState | None = None) -> None: ...

    @abstractmethod
    async def load_state(self, session_id: str) -> tuple[AgentRunState, dict]:
        """返回 (当前状态, metadata)。不存在时返回 (IDLE, {})。"""
        ...

    @abstractmethod
    async def save_messages(self, session_id: str, messages: list[Message]) -> None: ...

    @abstractmethod
    async def load_messages(self, session_id: str) -> list[Message]: ...

    @abstractmethod
    async def save_pending_tool_calls(self, session_id: str, tool_calls: list[ToolCall]) -> None: ...

    @abstractmethod
    async def load_pending_tool_calls(self, session_id: str) -> list[ToolCall]: ...

    @abstractmethod
    async def save_tool_result(self, session_id: str, tool_call_id: str, result: ToolResultMessage) -> None: ...

    @abstractmethod
    async def load_tool_results(self, session_id: str) -> dict[str, ToolResultMessage]:
        """返回 {tool_call_id: ToolResult}，用于幂等缓存检查。"""
        ...

    @abstractmethod
    async def clear_session(self, session_id: str) -> None: ...

class SQLiteStateStore(StateStore):
    """
    基于 aiosqlite 的 StateStore 实现。
    Phase 1 的唯一持久化后端，零外部依赖。
    """

    def __init__(self, db_path: str | Path = "myagent_state.db"):
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """创建表结构。Agent 启动时调用。"""
        self._db = await aiosqlite.connect(self._db_path)
        # 启用 SQLite 健壮性 PRAGMA
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                agent_state TEXT NOT NULL DEFAULT 'idle',
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                message_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS pending_tool_calls (
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_call_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT,
                PRIMARY KEY(session_id, tool_call_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        """)
        await self._db.commit()

        # 安全添加 session_state 列（Phase 1 新增，已存在则忽略）
        try:
            await self._db.execute("ALTER TABLE sessions ADD COLUMN session_state TEXT DEFAULT 'active'")
            await self._db.commit()
        except Exception:
            pass  # 列已存在

        # 安全添加 workspace 列（Phase 2 新增，已存在则忽略）
        try:
            await self._db.execute("ALTER TABLE sessions ADD COLUMN workspace TEXT")
            await self._db.commit()
        except Exception:
            pass  # 列已存在

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def save_state(self, session_id: str, state: AgentRunState, metadata: dict | None = None, session_state: SessionState | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        ss_value = session_state.value if session_state else SessionState.ACTIVE.value
        await self._db.execute(
            """INSERT INTO sessions (session_id, agent_state, session_state, metadata, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 agent_state = excluded.agent_state,
                 session_state = excluded.session_state,
                 metadata = excluded.metadata,
                 updated_at = excluded.updated_at""",
            (session_id, state.value, ss_value, meta_json, now),
        )
        await self._db.commit()
        logger.debug(f"State saved: session={session_id}, state={state.value}, session_state={ss_value}")

    async def load_state(self, session_id: str) -> tuple[AgentRunState, dict]:
        async with self._db.execute(
            "SELECT agent_state, metadata FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return AgentRunState.IDLE, {}
            # 兼容旧数据库中的已废弃状态值（running / finished）
            state_str = _LEGACY_STATE_MAP.get(row[0], row[0])
            return AgentRunState(state_str), json.loads(row[1])

    async def save_session_state(self, session_id: str, session_state: SessionState) -> None:
        """保存会话生命周期状态。"""
        now = datetime.now(timezone.utc).isoformat()
        # 确保会话行存在
        await self._db.execute(
            """INSERT INTO sessions (session_id, agent_state, session_state, metadata, updated_at)
               VALUES (?, 'idle', ?, '{}', ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 session_state = excluded.session_state,
                 updated_at = excluded.updated_at""",
            (session_id, session_state.value, now),
        )
        await self._db.commit()

    async def save_workspace(self, session_id: str, workspace_json: str) -> None:
        """保存工作空间状态快照（JSON 字符串）。"""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO sessions (session_id, agent_state, session_state, metadata, updated_at, workspace)
               VALUES (?, 'idle', 'active', '{}', ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 workspace = excluded.workspace,
                 updated_at = excluded.updated_at""",
            (session_id, now, workspace_json),
        )
        await self._db.commit()

    async def load_workspace(self, session_id: str) -> str | None:
        """加载工作空间状态快照 JSON。不存在返回 None。"""
        async with self._db.execute(
            "SELECT workspace FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # 全量替换策略：先删后插。短会话场景足够高效。
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        for seq, msg in enumerate(messages):
            msg_json = msg.model_dump_json()
            await self._db.execute(
                "INSERT INTO messages (session_id, seq, message_json, created_at) VALUES (?, ?, ?, ?)",
                (session_id, seq, msg_json, now),
            )
        await self._db.commit()

    async def load_messages(self, session_id: str) -> list[Message]:
        async with self._db.execute(
            "SELECT message_json FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [Message.model_validate_json(row[0]) for row in rows]

    async def save_pending_tool_calls(self, session_id: str, tool_calls: list[ToolCall]) -> None:
        for tc in tool_calls:
            await self._db.execute(
                """INSERT OR REPLACE INTO pending_tool_calls
                   (session_id, tool_call_id, tool_call_json, status)
                   VALUES (?, ?, ?, 'pending')""",
                (session_id, tc.id, tc.model_dump_json()),
            )
        await self._db.commit()

    async def load_pending_tool_calls(self, session_id: str) -> list[ToolCall]:
        async with self._db.execute(
            "SELECT tool_call_json FROM pending_tool_calls WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [ToolCall.model_validate_json(row[0]) for row in rows]

    async def save_tool_result(self, session_id: str, tool_call_id: str, result: ToolResultMessage) -> None:
        await self._db.execute(
            """UPDATE pending_tool_calls SET status = 'completed', result_json = ?
               WHERE session_id = ? AND tool_call_id = ?""",
            (result.model_dump_json(), session_id, tool_call_id),
        )
        await self._db.commit()

    async def load_tool_results(self, session_id: str) -> dict[str, ToolResultMessage]:
        async with self._db.execute(
            "SELECT tool_call_id, result_json FROM pending_tool_calls WHERE session_id = ? AND status = 'completed'",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: ToolResultMessage.model_validate_json(row[1]) for row in rows}

    async def list_all_sessions(self) -> list[dict]:
        """返回所有会话的摘要信息列表，按更新时间倒序。"""
        async with self._db.execute(
            "SELECT session_id, agent_state, metadata, updated_at, session_state FROM sessions ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                meta = json.loads(row[2]) if row[2] else {}
                # 兼容旧数据库中的已废弃状态值
                state_str = _LEGACY_STATE_MAP.get(row[1], row[1])
                result.append({
                    "session_id": row[0],
                    "agent_state": state_str,
                    "metadata": meta,
                    "updated_at": row[3],
                    "session_state": row[4] or "active",
                })
            return result

    async def clear_session(self, session_id: str) -> None:
        cursor = await self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM pending_tool_calls WHERE session_id = ?", (session_id,))
        await self._db.commit()
        if cursor.rowcount > 0:
            logger.info(f"DB session cleared: {session_id}")
