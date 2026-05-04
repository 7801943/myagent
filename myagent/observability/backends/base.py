"""
BaseAuditBackend：审计日志后端抽象基类。
"""
from abc import ABC, abstractmethod
from myagent.observability.events import AuditEvent

class BaseAuditBackend(ABC):
    """审计日志后端抽象基类。"""

    @abstractmethod
    async def write(self, event: AuditEvent) -> None:
        """写入一条审计事件。"""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """刷新缓冲区。"""
        ...

    async def close(self) -> None:
        """关闭后端连接。"""
        await self.flush()