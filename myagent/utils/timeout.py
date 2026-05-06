"""统一的超时管理，包装 asyncio.wait_for。"""
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