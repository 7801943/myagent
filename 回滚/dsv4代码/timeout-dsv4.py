"""统一的超时管理，包装 asyncio.wait_for，提供 Timeout 异步上下文管理器。"""
import asyncio
from dataclasses import dataclass


@dataclass
class TimeoutConfig:
    """可配置的超时参数集合。"""
    provider_first_token_s: float = 15.0
    tool_execution_s: float = 30.0
    cli_command_s: float = 60.0
    subagent_total_s: float = 300.0
    agent_turn_s: float = 600.0


class Timeout:
    """异步超时上下文管理器，超时时抛出 asyncio.TimeoutError。
    
    用法:
        async with Timeout(30.0):
            await some_operation()
    """

    def __init__(self, timeout: float):
        self._timeout = timeout

    async def __aenter__(self):
        self._task = asyncio.current_task()
        loop = asyncio.get_event_loop()
        self._handle = loop.call_later(self._timeout, self._cancel_task)
        return self

    def _cancel_task(self):
        """超时回调：取消当前任务。"""
        if self._task and not self._task.done():
            self._task.cancel()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._handle.cancel()
        # 将 CancelledError 转为 asyncio.TimeoutError，匹配 loop.py 的 except
        if exc_type is asyncio.CancelledError:
            raise asyncio.TimeoutError()
        return False

    def __call__(self, coro):
        """支持 with_timeout 式调用: await Timeout(30)(some_coro())"""
        return with_timeout(coro, self._timeout)


class TimeoutError(Exception):
    """Agent 框架内部超时异常（区分 asyncio.TimeoutError）。"""
    def __init__(self, operation: str, timeout: float):
        self.operation = operation
        self.timeout = timeout
        super().__init__(f"Operation '{operation}' timed out after {timeout}s")


async def with_timeout(coro, timeout: float, operation: str = "unknown"):
    """包装 asyncio.wait_for，抛出自定义 TimeoutError。"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(operation, timeout)
