from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from tests.contract.adapters.telegram.conftest import (
    FakeDialog,
    FakeMessage,
    FakeNativeClient,
    FakePeer,
    FakeRaw,
)
from yukibot.adapters.telegram import (
    PeerRegistry,
    TelethonClientLifecycle,
    TelethonEventSource,
    normalize_message,
    peer_dialog_id,
)
from yukibot.contracts import (
    TelegramContentType,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
    TelegramServiceKind,
)
from yukibot.kernel import InProcessEventBus


class NewEvent:
    pass


class EditEvent:
    pass


class DeleteEvent:
    pass


class MessageMediaPhoto:
    pass


class MessageActionTopicCreate:
    title = "news"


class MessageService:
    def __init__(self) -> None:
        self.media = None
        self.reply_to = None
        self.action = MessageActionTopicCreate()


def test_peer_ids_from_pure_python_telethon_v2_are_normalized() -> None:
    Channel = type("Channel", (), {"id": 123})
    Group = type("Group", (), {"id": 456})
    User = type("User", (), {"id": 789})

    assert peer_dialog_id(Channel()) == -1_000_000_000_123
    assert peer_dialog_id(Group()) == -456
    assert peer_dialog_id(User()) == 789


def test_normalize_photo_caption_and_forum_topic() -> None:
    reply = SimpleNamespace(forum_topic=True, reply_to_top_id=77, reply_to_msg_id=70)
    raw = FakeRaw(media=MessageMediaPhoto(), reply_to=reply)
    message = FakeMessage(
        10,
        FakePeer(-1001),
        text="caption",
        sender=FakePeer(42),
        photo=object(),
        file=object(),
        replied_message_id=70,
        _raw=raw,
    )

    normalized = normalize_message(message, datetime.now(UTC))

    assert normalized.content_type is TelegramContentType.PHOTO
    assert normalized.text is None
    assert normalized.caption == "caption"
    assert normalized.topic_id == 77
    assert normalized.sender_id == 42


def test_normalize_edit_timestamp_from_raw_message() -> None:
    edited_at = datetime(2026, 1, 2, tzinfo=UTC)
    message = FakeMessage(
        10,
        FakePeer(-1001),
        _raw=FakeRaw(edit_date=int(edited_at.timestamp())),
    )

    normalized = normalize_message(message, datetime.now(UTC))

    assert normalized.edited_at == edited_at


def test_normalize_service_topic_creation() -> None:
    message = FakeMessage(55, FakePeer(-1001), text=None, text_html=None, _raw=MessageService())

    normalized = normalize_message(message, datetime.now(UTC))

    assert normalized.content_type is TelegramContentType.SERVICE
    assert normalized.topic_id == 55
    assert normalized.service is not None
    assert normalized.service.kind is TelegramServiceKind.TOPIC_CREATED
    assert normalized.service.new_title == "news"


async def test_event_source_lifecycle_and_event_publication(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    client = FakeNativeClient()
    client.authorized = False
    client.dialogs.append(FakeDialog(FakePeer(-2001)))
    peers = PeerRegistry()
    bus = InProcessEventBus()
    received = []
    deleted = []

    async def on_message(event: TelegramMessageReceived) -> None:
        received.append(event)

    async def on_delete(event: TelegramMessagesDeleted) -> None:
        deleted.append(event)

    bus.subscribe(TelegramMessageReceived, on_message)
    bus.subscribe(TelegramMessagesDeleted, on_delete)
    connection = TelethonClientLifecycle(client, peers)  # type: ignore[arg-type]
    source = TelethonEventSource(client, bus, peers)  # type: ignore[arg-type]

    await connection.start()
    await source.start()
    assert client.connected
    assert client.logged_in
    assert peers.get(-2001) is not None

    message = FakeMessage(1, FakePeer(-100123), sender=FakePeer(42))
    await client.handlers[NewEvent](message)  # type: ignore[operator]
    deletion = SimpleNamespace(message_ids=(1, 2), channel_id=123)
    await client.handlers[DeleteEvent](deletion)  # type: ignore[operator]

    assert received[0].message.ref.chat_id == -100123
    assert peers.get(-100123) is not None
    assert deleted[0].chat_id == -1_000_000_000_123

    await source.stop()
    assert client.handlers == {}
    assert not client.disconnected
    assert peers.get(-2001) is not None

    await connection.stop()
    assert client.disconnected
    assert peers.get(-2001) is None


async def test_event_source_drains_inflight_handlers_before_stopping(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    client = FakeNativeClient()
    bus = InProcessEventBus()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(event: TelegramMessageReceived) -> None:
        entered.set()
        await release.wait()

    bus.subscribe(TelegramMessageReceived, slow_handler)
    peers = PeerRegistry()
    source = TelethonEventSource(
        client,
        bus,
        peers,
        drain_timeout=0.2,
    )  # type: ignore[arg-type]
    connection = TelethonClientLifecycle(client, peers)  # type: ignore[arg-type]
    await connection.start()
    await source.start()

    handling = asyncio.create_task(
        client.handlers[NewEvent](FakeMessage(1, FakePeer(-1001)))  # type: ignore[operator]
    )
    await entered.wait()
    stopping = asyncio.create_task(source.stop())
    await asyncio.sleep(0)

    assert not stopping.done()
    assert client.handlers == {}
    release.set()
    await asyncio.gather(handling, stopping)
    await connection.stop()
