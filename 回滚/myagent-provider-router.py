"""
ProviderRouter：多路冗余 + Failover + 熔断器。
按优先级排序 Provider，遇到可重试错误自动切换。
"""
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from myagent.providers.base import (
    BaseProvider, StreamEvent,
    ProviderRateLimitError, ProviderTimeoutError, AllProvidersFailedError,
)
from myagent.utils.logging import get_logger

logger = get_logger(__name__)

# 可触发 failover 的错误类型
_RETRYABLE_ERRORS = (ProviderRateLimitError, ProviderTimeoutError)

@dataclass
class CircuitBreaker:
    """熔断器：连续失败 N 次后暂停使用，recovery_seconds 后自动恢复。"""
    failure_threshold: int = 3
    recovery_seconds: int = 60
    _failure_counts: dict[str, int] = field(default_factory=dict)
    _tripped_at: dict[str, float] = field(default_factory=dict)

    def is_open(self, provider_name: str) -> bool:
        """检查熔断器是否处于断开（暂停）状态。"""
        if provider_name not in self._tripped_at:
            return False
        elapsed = time.monotonic() - self._tripped_at[provider_name]
        if elapsed >= self.recovery_seconds:
            # 恢复
            del self._tripped_at[provider_name]
            self._failure_counts[provider_name] = 0
            logger.info(f"Circuit breaker recovered for {provider_name}")
            return False
        return True

    def record_failure(self, provider_name: str) -> None:
        """记录一次失败。"""
        count = self._failure_counts.get(provider_name, 0) + 1
        self._failure_counts[provider_name] = count
        if count >= self.failure_threshold:
            self._tripped_at[provider_name] = time.monotonic()
            logger.warning(
                f"Circuit breaker tripped for {provider_name} "
                f"(failures={count}, recovery in {self.recovery_seconds}s)"
            )

    def record_success(self, provider_name: str) -> None:
        """记录成功，重置计数。"""
        self._failure_counts.pop(provider_name, None)
        self._tripped_at.pop(provider_name, None)

class ProviderRouter:
    """
    多路冗余路由器。
    1. 按 priority 排序
    2. 跳过熔断中的 Provider
    3. 遇到可重试错误自动切换
    4. 全部失败时抛 AllProvidersFailedError
    """

    def __init__(
        self,
        providers: list[BaseProvider],
        failure_threshold: int = 3,
        recovery_seconds: int = 60,
    ):
        self._providers = sorted(providers, key=lambda p: getattr(p, "_priority", 99))
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_seconds=recovery_seconds,
        )

    @property
    def providers(self) -> list[BaseProvider]:
        return list(self._providers)

    async def stream(
        self,
        messages: list,
        tools: list | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        """
        尝试按优先级从 Provider 获取流式响应。
        失败时自动 failover 到下一个 Provider。
        failover 事件通过 StreamEvent(type="provider_failover") 向上层透传，
        由 StreamParser 负责分发给 Hook，Router 本身不直接依赖 Hook 体系。
        """
        errors: list[tuple[str, Exception]] = []
        tried_providers = set()

        for provider in self._providers:
            if provider.name in tried_providers:
                continue
            if self._breaker.is_open(provider.name):
                logger.debug(f"Skipping {provider.name} (circuit breaker open)")
                continue

            tried_providers.add(provider.name)

            try:
                # 用 async for 包装，确保流式传输中的错误也能被捕获
                formatted_messages = provider.format_messages(messages)
                formatted_tools = provider.format_tools(tools) if tools else None

                async for event in provider.stream(formatted_messages, formatted_tools, **kwargs):
                    yield event
                # 成功完成
                self._breaker.record_success(provider.name)
                return

            except _RETRYABLE_ERRORS as e:
                errors.append((provider.name, e))
                self._breaker.record_failure(provider.name)
                # 找到下一个可用 Provider，通过流通知上层发生了 failover
                next_p = self._find_next_available(tried_providers | {provider.name})
                yield StreamEvent(
                    type="provider_failover",
                    meta={
                        "from_provider": provider.name,
                        "to_provider": next_p.name if next_p else "",
                        "reason": str(e),
                    },
                )
                logger.warning(f"Provider {provider.name} failed: {e}, trying next...")
                continue

            except Exception as e:
                errors.append((provider.name, e))
                logger.error(f"Provider {provider.name} unexpected error: {e}")
                continue

        raise AllProvidersFailedError(errors)

    def _find_next_available(self, tried: set[str]) -> BaseProvider | None:
        for p in self._providers:
            if p.name not in tried and not self._breaker.is_open(p.name):
                return p
        return None