"""Scheduled ingestion for public channels that should not be joined."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from yukibot.contracts import TelegramMessageReceived
from yukibot.kernel import EventBus

from .errors import RetryAfter
from .models import IncomingMessage, PollCursor, Route, SourceEndpoint
from .ports import PollCursorRepository, RouteRepository, TelegramSourceGateway


class SourcePoller:
    """Poll configured public sources and publish the normal receive contract."""

    def __init__(
        self,
        routes: RouteRepository,
        cursors: PollCursorRepository,
        telegram: TelegramSourceGateway,
        bus: EventBus,
        *,
        batch_size: int = 100,
        max_batches_per_poll: int = 10,
        schedule_tick: float = 1.0,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        if batch_size <= 0 or max_batches_per_poll <= 0:
            raise ValueError("poll batch limits must be positive")
        if schedule_tick <= 0:
            raise ValueError("schedule_tick must be positive")
        self._routes = routes
        self._cursors = cursors
        self._telegram = telegram
        self._bus = bus
        self._batch_size = batch_size
        self._max_batches = max_batches_per_poll
        self._schedule_tick = schedule_tick
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._next_due: dict[int, float] = {}
        self._stop = asyncio.Event()

    def prepare(self) -> None:
        self._stop.clear()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._schedule_tick)
            except TimeoutError:
                continue

    async def poll_once(self, now: float | None = None) -> int:
        current = self._clock() if now is None else now
        sources = _polling_sources(await self._routes.list_all())
        self._next_due = {
            chat_id: due for chat_id, due in self._next_due.items() if chat_id in sources
        }
        published = 0
        for chat_id, source in sources.items():
            if self._next_due.get(chat_id, current) > current:
                continue
            interval = source.poll_interval_seconds
            if interval is None:
                continue
            try:
                count, backlog = await self._poll_source(source)
            except RetryAfter as error:
                self._next_due[chat_id] = current + error.seconds
                self._logger.warning(
                    "source poll rate limited",
                    extra={
                        "feature": "forwarder",
                        "chat_id": chat_id,
                        "retry_after": error.seconds,
                    },
                )
                continue
            except Exception as error:
                self._next_due[chat_id] = current + interval
                self._logger.error(
                    "source poll failed",
                    extra={
                        "feature": "forwarder",
                        "chat_id": chat_id,
                        "error_type": type(error).__name__,
                    },
                    exc_info=error,
                )
                continue
            published += count
            self._next_due[chat_id] = current if backlog else current + interval
            self._logger.info(
                "source poll completed",
                extra={
                    "feature": "forwarder",
                    "chat_id": chat_id,
                    "published_messages": count,
                    "backlog": backlog,
                },
            )
        return published

    async def _poll_source(self, source: SourceEndpoint) -> tuple[int, bool]:
        await self._telegram.ensure_source(source, join=False)
        cursor = await self._cursors.get(source.chat_id)
        if cursor is None:
            latest = await self._telegram.latest_message_id(source)
            await self._cursors.save(PollCursor(source.chat_id, latest))
            return 0, False

        published = 0
        after_message_id = cursor.last_message_id
        for _ in range(self._max_batches):
            messages = tuple(
                await self._telegram.fetch_messages_after(
                    source,
                    after_message_id,
                    limit=self._batch_size,
                )
            )
            if not messages:
                return published, False
            _validate_batch(source.chat_id, after_message_id, messages)
            for message in messages:
                await self._bus.publish(TelegramMessageReceived(message))
            after_message_id = messages[-1].ref.message_id
            await self._cursors.save(PollCursor(source.chat_id, after_message_id))
            published += len(messages)
            if len(messages) < self._batch_size:
                return published, False
        return published, True


def _polling_sources(routes: Sequence[Route]) -> dict[int, SourceEndpoint]:
    selected: dict[int, SourceEndpoint] = {}
    for route in routes:
        if not route.enabled or not route.source.is_polled:
            continue
        existing = selected.get(route.source.chat_id)
        if existing is None or (
            route.source.poll_interval_seconds is not None
            and existing.poll_interval_seconds is not None
            and route.source.poll_interval_seconds < existing.poll_interval_seconds
        ):
            selected[route.source.chat_id] = route.source
    return selected


def _validate_batch(
    chat_id: int,
    after_message_id: int,
    messages: tuple[IncomingMessage, ...],
) -> None:
    previous = after_message_id
    for message in messages:
        if message.ref.chat_id != chat_id or message.ref.message_id <= previous:
            raise ValueError("Telegram polling returned an invalid or unordered message batch")
        previous = message.ref.message_id
