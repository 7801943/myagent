"""
StateStore：会话状态持久化。
V3 核心变更 —— 显式状态机的持久化保障，使得断线恢复成为可能。

表结构：
  - sessions: session_id, agent_state(枚举), metadata(JSON), updated_at
  - messages: session_id, seq, message_json, created_at
  - pending_tool_calls: session_id, tool_call_id, tool_call_json, status, result_json
"""
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import aiosqlite

from myagent.context.message import Message, ToolCall, ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class AgentState(str, Enum):
    """
    V3 显式状态机的核心枚举。
    AgentLoop 的每一次状态变迁都必须先持久化到 StateStore，
    再执行实际操作——这是断电恢复的基石。
    """
    IDLE = "idle"                    # 等待输入
    THINKING = "thinking"            # LLM 正在进行思维链推理
    RUNNING = "running"              # 正在调用 LLM / 内容生成
    WAITING_TOOL = "waiting_tool"    # LLM 已返回 tool_calls，等待执行
    WAITING_HITL = "waiting_hitl"    # 等待人工审批（Phase 2 实现，Phase 1 预留）
    ERROR = "error"                  # 发生错误
    FINISHED = "finished"            # 当前 Turn 完成

class StateStore(ABC):
    """状态持久化抽象接口。"""

    @abstractmethod
    async def save_state(self, session_id: str, state: AgentState, metadata: dict | None = None) -> None: ...

    @abstractmethod
    async def load_state(self, session_id: str) -> tuple[AgentState, dict]:
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
    async def save_tool_result(self, session_id: str, tool_call_id: str, result: ToolResult) -> None: ...

    @abstractmethod
    async def load_tool_results(self, session_id: str) -> dict[str, ToolResult]:
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

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def save_state(self, session_id: str, state: AgentState, metadata: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        await self._db.execute(
            """INSERT INTO sessions (session_id, agent_state, metadata, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 agent_state = excluded.agent_state,
                 metadata = excluded.metadata,
                 updated_at = excluded.updated_at""",
            (session_id, state.value, meta_json, now),
        )
        await self._db.commit()
        logger.debug(f"State saved: session={session_id}, state={state.value}")

    async def load_state(self, session_id: str) -> tuple[AgentState, dict]:
        async with self._db.execute(
            "SELECT agent_state, metadata FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return AgentState.IDLE, {}
            return AgentState(row[0]), json.loads(row[1])

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

    async def save_tool_result(self, session_id: str, tool_call_id: str, result: ToolResult) -> None:
        await self._db.execute(
            """UPDATE pending_tool_calls SET status = 'completed', result_json = ?
               WHERE session_id = ? AND tool_call_id = ?""",
            (result.model_dump_json(), session_id, tool_call_id),
        )
        await self._db.commit()

    async def load_tool_results(self, session_id: str) -> dict[str, ToolResult]:
        async with self._db.execute(
            "SELECT tool_call_id, result_json FROM pending_tool_calls WHERE session_id = ? AND status = 'completed'",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: ToolResult.model_validate_json(row[1]) for row in rows}

    async def list_all_sessions(self) -> list[dict]:
        """返回所有会话的摘要信息列表，按更新时间倒序。"""
        async with self._db.execute(
            "SELECT session_id, agent_state, metadata, updated_at FROM sessions ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                meta = json.loads(row[2]) if row[2] else {}
                result.append({
                    "session_id": row[0],
                    "agent_state": row[1],
                    "metadata": meta,
                    "updated_at": row[3],
                })
            return result

    async def clear_session(self, session_id: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM pending_tool_calls WHERE session_id = ?", (session_id,))
        await self._db.commit()
