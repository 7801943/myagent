"""
SqliteAuditBackend：SQLite 审计日志后端。
"""
import json
import sqlite3
from pathlib import Path

from myagent.observability.events import AuditEvent
from myagent.observability.backends.base import BaseAuditBackend
from myagent.observability.masker import DataMasker
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    agent_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    iteration INTEGER,
    data TEXT
);
"""

class SqliteAuditBackend(BaseAuditBackend):
    """将审计事件写入 SQLite 数据库。"""

    def __init__(
        self,
        db_path: str | Path,
        masker: DataMasker | None = None,
        buffer_size: int = 100,
    ):
        self._path = Path(db_path)
        self._masker = masker or DataMasker()
        self._buffer_size = buffer_size
        self._buffer: list[AuditEvent] = []
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()

    async def write(self, event: AuditEvent) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= self._buffer_size:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        with sqlite3.connect(self._path) as conn:
            for event in self._buffer:
                data = self._masker.mask_dict(event.data)
                conn.execute(
                    """INSERT INTO audit_events
                       (event_type, timestamp, session_id, turn_id, agent_id,
                        trace_id, span_id, iteration, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_type.value,
                        event.timestamp,
                        event.session_id,
                        event.turn_id,
                        event.agent_id,
                        event.trace_id,
                        event.span_id,
                        event.iteration,
                        json.dumps(data, ensure_ascii=False),
                    ),
                )
            conn.commit()
        self._buffer.clear()
        logger.debug(f"Flushed {len(self._buffer)} audit events to SQLite")