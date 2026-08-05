from __future__ import annotations

from datetime import UTC, datetime

from yukibot.contracts import TelegramContentType, TelegramMessage, TelegramMessageReceived
from yukibot.features.forwarder import (
    ChatIdentity,
    DestinationEndpoint,
    InMemoryPollCursorRepository,
    InMemoryRouteRepository,
    MessageRef,
    PollCursor,
    Route,
    SourceEndpoint,
)
from yukibot.features.forwarder.poller import SourcePoller
from yukibot.kernel import InProcessEventBus


class FakePollingSource:
    def __init__(self, messages: tuple[TelegramMessage, ...], *, latest: int = 0) -> None:
        self.messages = messages
        self.latest = latest
        self.prepared: list[tuple[SourceEndpoint, bool]] = []
        self.fetches: list[tuple[int, int]] = []

    async def resolve_chat(self, reference: str) -> ChatIdentity:
        return ChatIdentity(-1001, "source")

    async def ensure_source(self, source: SourceEndpoint, *, join: bool) -> None:
        self.prepared.append((source, join))

    async def latest_message_id(self, source: SourceEndpoint) -> int:
        return self.latest

    async def fetch_messages_after(
        self,
        source: SourceEndpoint,
        after_message_id: int,
        *,
        limit: int,
    ) -> tuple[TelegramMessage, ...]:
        self.fetches.append((after_message_id, limit))
        return tuple(
            message for message in self.messages if message.ref.message_id > after_message_id
        )[:limit]


def message(message_id: int) -> TelegramMessage:
    return TelegramMessage(
        MessageRef(-1001, message_id),
        TelegramContentType.TEXT,
        datetime.now(UTC),
        text=f"message {message_id}",
    )


def polling_route() -> Route:
    return Route(
        1,
        SourceEndpoint(-1001, username="source", poll_interval_seconds=300),
        DestinationEndpoint(-2001),
    )


async def test_first_poll_initializes_cursor_without_forwarding_old_history() -> None:
    routes = InMemoryRouteRepository((polling_route(),))
    cursors = InMemoryPollCursorRepository()
    telegram = FakePollingSource((message(40), message(50)), latest=50)
    bus = InProcessEventBus()
    received: list[TelegramMessageReceived] = []
    bus.subscribe(TelegramMessageReceived, received.append)  # type: ignore[arg-type]
    poller = SourcePoller(routes, cursors, telegram, bus)

    assert await poller.poll_once(100.0) == 0
    assert await cursors.get(-1001) == PollCursor(-1001, 50)
    assert received == []
    assert telegram.fetches == []
    assert telegram.prepared == [(polling_route().source, False)]


async def test_polling_pages_in_order_advances_cursor_and_obeys_interval() -> None:
    routes = InMemoryRouteRepository((polling_route(),))
    cursors = InMemoryPollCursorRepository((PollCursor(-1001, 10),))
    telegram = FakePollingSource((message(11), message(12), message(13)), latest=13)
    bus = InProcessEventBus()
    received: list[TelegramMessageReceived] = []

    async def capture(event: TelegramMessageReceived) -> None:
        received.append(event)

    bus.subscribe(TelegramMessageReceived, capture)
    poller = SourcePoller(routes, cursors, telegram, bus, batch_size=2)

    assert await poller.poll_once(100.0) == 3
    assert [event.message.ref.message_id for event in received] == [11, 12, 13]
    assert await cursors.get(-1001) == PollCursor(-1001, 13)
    assert telegram.fetches == [(10, 2), (12, 2)]

    assert await poller.poll_once(399.0) == 0
    assert telegram.fetches == [(10, 2), (12, 2)]
    assert await poller.poll_once(400.0) == 0
    assert telegram.fetches[-1] == (13, 2)
