"""
JsonlAuditBackend：JSONL 文件审计日志后端。
"""
import json
from pathlib import Path

from myagent.observability.events import AuditEvent
from myagent.observability.backends.base import BaseAuditBackend
from myagent.observability.masker import DataMasker
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class JsonlAuditBackend(BaseAuditBackend):
    """将审计事件以 JSONL 格式写入文件。"""

    def __init__(
        self,
        file_path: str | Path,
        masker: DataMasker | None = None,
        buffer_size: int = 100,
    ):
        self._path = Path(file_path)
        self._masker = masker or DataMasker()
        self._buffer_size = buffer_size
        self._buffer: list[str] = []

    async def write(self, event: AuditEvent) -> None:
        """写入一条审计事件（先脱敏，后缓冲）。"""
        data = event.to_dict()
        masked_data = self._masker.mask_dict(data)
        self._buffer.append(json.dumps(masked_data, ensure_ascii=False))

        if len(self._buffer) >= self._buffer_size:
            await self.flush()

    async def flush(self) -> None:
        """将缓冲区写入文件。"""
        if not self._buffer:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            for line in self._buffer:
                f.write(line + "\n")
        self._buffer.clear()
        logger.debug(f"Flushed audit events to {self._path}")