"""A reusable asyncio sliding-window rate limiter for Telegram gateways."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class SlidingWindowRateLimiter:
    def __init__(
        self,
        max_events: int,
        period: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        if period <= 0:
            raise ValueError("period must be positive")
        self._max_events = max_events
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._discard_expired(now)
                if len(self._events) < self._max_events:
                    self._events.append(now)
                    return
                delay = max(0.0, self._period - (now - self._events[0]))
                await self._sleep(delay)

    def _discard_expired(self, now: float) -> None:
        threshold = now - self._period
        while self._events and self._events[0] <= threshold:
            self._events.popleft()


class TelegramRequestLimiter:
    """Share account concurrency and serialize operations for each destination chat."""

    def __init__(self, *, max_concurrency: int = 4, messages_per_second: int = 20) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if messages_per_second <= 0:
            raise ValueError("messages_per_second must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._messages_per_second = messages_per_second
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._chat_limiters: dict[int, SlidingWindowRateLimiter] = {}

    @asynccontextmanager
    async def slot(self, chat_id: int) -> AsyncIterator[None]:
        chat_lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        limiter = self._chat_limiters.setdefault(
            chat_id,
            SlidingWindowRateLimiter(self._messages_per_second, 1.0),
        )
        async with chat_lock, self._semaphore:
            await limiter.acquire()
            yield
