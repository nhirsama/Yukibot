"""A reusable asyncio sliding-window rate limiter for Telegram gateways."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable


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
