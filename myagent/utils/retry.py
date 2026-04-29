"""指数退避 + 抖动的异步重试装饰器。"""
import asyncio
import functools
import random
from typing import Type

from myagent.utils.logging import get_logger

logger = get_logger(__name__)

class ExponentialBackoff:
    def __init__(self, base: float = 1.0, max_delay: float = 30.0, jitter: bool = True):
        self.base = base
        self.max_delay = max_delay
        self.jitter = jitter

    def delay(self, attempt: int) -> float:
        d = min(self.base * (2 ** attempt), self.max_delay)
        if self.jitter:
            d = d * (0.5 + random.random() * 0.5)
        return d

def async_retry(
    max_attempts: int = 3,
    backoff: ExponentialBackoff | None = None,
    retry_on: tuple[Type[Exception], ...] = (Exception,),
):
    """
    异步重试装饰器。
    仅对 retry_on 中指定的异常类型重试。
    """
    if backoff is None:
        backoff = ExponentialBackoff()

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        delay = backoff.delay(attempt)
                        logger.warning(
                            f"Retry {attempt+1}/{max_attempts} for {func.__name__}: {e}, "
                            f"waiting {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator