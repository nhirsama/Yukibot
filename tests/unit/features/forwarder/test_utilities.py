import asyncio

from yukibot.adapters.telegram import SlidingWindowRateLimiter
from yukibot.features.forwarder import AlbumBuffer


async def test_album_buffer_uses_sliding_window_and_sorts_items() -> None:
    flushed: list[tuple[int, ...]] = []

    async def callback(items: tuple[int, ...]) -> None:
        flushed.append(items)

    buffer = AlbumBuffer[str, int](callback, flush_delay=0.01, sort_key=lambda item: item)
    await buffer.add("album", 3)
    await buffer.add("album", 1)
    await buffer.add("album", 2)
    await asyncio.sleep(0.03)

    assert flushed == [(1, 2, 3)]
    assert buffer.pending_groups == 0
    await buffer.close()


async def test_album_buffer_flushes_pending_items_on_close() -> None:
    flushed: list[tuple[int, ...]] = []

    async def callback(items: tuple[int, ...]) -> None:
        flushed.append(items)

    buffer = AlbumBuffer[str, int](callback, flush_delay=60)
    await buffer.add("album", 1)
    await buffer.close()

    assert flushed == [(1,)]


async def test_sliding_window_limiter_waits_until_capacity_is_available() -> None:
    now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    limiter = SlidingWindowRateLimiter(2, 10, clock=clock, sleep=sleep)
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()

    assert sleeps == [10]
