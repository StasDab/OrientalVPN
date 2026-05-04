import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    retries: int = 3,
    base_delay_seconds: float = 1.0,
) -> T:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == retries:
                break
            await asyncio.sleep(base_delay_seconds * (2 ** (attempt - 1)))
    if last_error:
        raise last_error
    raise RuntimeError("with_retry failed without exception")
