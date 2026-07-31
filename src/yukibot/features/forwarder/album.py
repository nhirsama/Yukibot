"""Generic asynchronous sliding-window album buffer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

type FlushCallback[ItemT] = Callable[[tuple[ItemT, ...]], Awaitable[None]]
type ErrorCallback = Callable[[BaseException], None]


class AlbumBuffer[KeyT: Hashable, ItemT]:
    """Collect items by key and flush after no new item arrives for a delay."""

    def __init__(
        self,
        callback: FlushCallback[ItemT],
        *,
        flush_delay: float = 0.8,
        sort_key: Callable[[ItemT], Any] | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        if flush_delay < 0:
            raise ValueError("flush_delay must not be negative")
        self._callback = callback
        self._flush_delay = flush_delay
        self._sort_key = sort_key
        self._on_error = on_error
        self._groups: dict[KeyT, list[ItemT]] = {}
        self._timers: dict[KeyT, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def add(self, key: KeyT, item: ItemT) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("album buffer is closed")
            self._groups.setdefault(key, []).append(item)
            previous = self._timers.get(key)
            if previous is not None:
                previous.cancel()
            task = asyncio.create_task(self._flush_after(key), name=f"album-buffer:{key}")
            self._timers[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def flush(self, key: KeyT) -> None:
        async with self._lock:
            timer = self._timers.pop(key, None)
            current = asyncio.current_task()
            if timer is not None and timer is not current:
                timer.cancel()
            items = self._groups.pop(key, [])
        if items:
            await self._invoke_callback(items)

    async def close(self, *, flush: bool = True) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            timers = tuple(self._timers.values())
            self._timers.clear()
            groups = tuple(self._groups.values()) if flush else ()
            self._groups.clear()
            active = tuple(self._tasks)
            for timer in timers:
                timer.cancel()

        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for items in groups:
            await self._invoke_callback(items)

    @property
    def pending_groups(self) -> int:
        return len(self._groups)

    async def _flush_after(self, key: KeyT) -> None:
        try:
            await asyncio.sleep(self._flush_delay)
            await self.flush(key)
        except asyncio.CancelledError:
            return
        except BaseException as error:
            if self._on_error is not None:
                self._on_error(error)
            else:
                logging.getLogger(__name__).exception("album callback failed")

    async def _invoke_callback(self, items: list[ItemT]) -> None:
        if self._sort_key is not None:
            items.sort(key=self._sort_key)
        await self._callback(tuple(items))
