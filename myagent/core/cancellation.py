"""
CancellationToken + CancelReason + AgentCancelledError。
协作式取消机制，贯穿 AgentLoop.run() 全生命周期。
"""
import asyncio
from dataclasses import dataclass, field
from enum import Enum


class CancelReason(str, Enum):
    """取消原因枚举。"""
    USER_CANCEL = "user_cancelled"       # 用户主动取消
    TIMEOUT = "timeout"                  # 超时（用户确认取消后）


@dataclass
class CancellationToken:
    """
    协作式取消令牌，贯穿整个 AgentLoop.run() 生命周期。
    AgentLoop 在每个 await 点检查此令牌，实现优雅退出。
    """
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _reason: CancelReason | None = None
    _detail: str = ""

    def cancel(self, reason: CancelReason = CancelReason.USER_CANCEL, detail: str = "") -> None:
        """触发取消信号。"""
        self._reason = reason
        self._detail = detail
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> CancelReason | None:
        return self._reason

    @property
    def detail(self) -> str:
        return self._detail

    async def check(self) -> None:
        """检查取消状态，已取消则抛出 AgentCancelledError。"""
        if self._event.is_set():
            raise AgentCancelledError(
                self._reason or CancelReason.USER_CANCEL,
                self._detail,
            )


class AgentCancelledError(Exception):
    """Agent 执行被取消时抛出的异常。"""

    def __init__(self, reason: CancelReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")