"""
WebSocket 锁机制：确保同一 session 的消息串行处理。
防止 WebSocket 并发写入导致消息乱序。
"""
import asyncio

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class WebSocketLock:
    """
    WebSocket 会话锁。
    确保同一个 session_id 的 ReAct 循环不会被并发执行。
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, session_id: str) -> asyncio.Lock:
        """获取指定 session 的锁（懒创建）。"""
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def acquire(self, session_id: str) -> None:
        """获取锁。"""
        lock = self.get_lock(session_id)
        await lock.acquire()
        logger.debug(f"WebSocket lock acquired: {session_id}")

    def release(self, session_id: str) -> None:
        """释放锁。"""
        lock = self._locks.get(session_id)
        if lock and lock.locked():
            lock.release()
            logger.debug(f"WebSocket lock released: {session_id}")

    def cleanup(self, session_id: str) -> None:
        """清理已结束 session 的锁。"""
        lock = self._locks.pop(session_id, None)
        if lock and lock.locked():
            lock.release()