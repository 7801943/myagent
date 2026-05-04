"""
IdempotencyCache：V3 核心防击穿机制。
绑定全局唯一 Tool Call ID，拦截并抵御网络超时引发的重复执行攻击。
Phase 1 使用内存 LRU 缓存；可从 StateStore 预热。
"""
import asyncio
import time
from collections import OrderedDict

from myagent.tools.base import ToolResult
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class IdempotencyCache:
    """
    幂等缓存：防止同一 tool_call_id 被重复执行。

    使用场景：
    - 网络超时导致 LLM 重发相同 tool_call
    - 断线恢复后重新执行 pending 工具调用
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[ToolResult, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def has(self, tool_call_id: str) -> bool:
        """检查是否已有该 tool_call_id 的结果。"""
        async with self._lock:
            if tool_call_id not in self._cache:
                return False
            _, ts = self._cache[tool_call_id]
            if time.monotonic() - ts > self._ttl_seconds:
                del self._cache[tool_call_id]
                return False
            return True

    async def get(self, tool_call_id: str) -> ToolResult | None:
        """获取缓存的工具结果。"""
        async with self._lock:
            if tool_call_id not in self._cache:
                return None
            result, ts = self._cache[tool_call_id]
            if time.monotonic() - ts > self._ttl_seconds:
                del self._cache[tool_call_id]
                return None
            # 移到末尾（LRU）
            self._cache.move_to_end(tool_call_id)
            return result

    async def store(self, tool_call_id: str, result: ToolResult) -> None:
        """存储工具执行结果。"""
        async with self._lock:
            self._cache[tool_call_id] = (result, time.monotonic())
            self._cache.move_to_end(tool_call_id)
            # 淘汰超出的条目
            while len(self._cache) > self._max_size:
                evicted_key, _ = self._cache.popitem(last=False)
                logger.debug(f"IdempotencyCache evicted: {evicted_key}")

    async def warmup(self, results: dict[str, ToolResult]) -> None:
        """从 StateStore 预热缓存。"""
        for call_id, result in results.items():
            await self.store(call_id, result)
        logger.info(f"IdempotencyCache warmed up with {len(results)} entries")

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()