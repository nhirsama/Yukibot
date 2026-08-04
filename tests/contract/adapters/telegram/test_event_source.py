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
    TelegramCommandRouter,
    TelegramRequestLimiter,
    TelethonClientLifecycle,
    TelethonEventSource,
    normalize_message,
    peer_dialog_id,
)
from yukibot.contracts import (
    TelegramContentType,
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
    TelegramServiceKind,
)
from yukibot.kernel import (
    CommandDispatcher,
    CommandRegistry,
    CommandResult,
    ControlCommand,
    InProcessEventBus,
    TaskSupervisor,
)


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


class OutgoingAuthorizer:
    async def is_authorized(self, command: ControlCommand) -> bool:
        return command.outgoing


class MemoryReceipts:
    def __init__(self) -> None:
        self.processed: set[tuple[int, int]] = set()

    async def is_processed(self, chat_id: int, message_id: int) -> bool:
        return (chat_id, message_id) in self.processed

    async def mark_processed(self, chat_id: int, message_id: int) -> None:
        self.processed.add((chat_id, message_id))


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
    source = TelethonEventSource(
        client,  # type: ignore[arg-type]
        bus,
        peers,
        supervisor=TaskSupervisor(),
    )

    await connection.start()
    await source.start()
    await client.update_pump_started.wait()
    assert client.connected
    assert client.logged_in
    assert client.update_pump_calls == 1
    assert peers.get(-2001) is not None

    message = FakeMessage(1, FakePeer(-100123), sender=FakePeer(42))
    await client.handlers[NewEvent](message)  # type: ignore[operator]
    deletion = SimpleNamespace(message_ids=(1, 2), channel_id=123, chat_id=None)
    await client.handlers[DeleteEvent](deletion)  # type: ignore[operator]

    assert received[0].message.ref.chat_id == -100123
    assert peers.get(-100123) is not None
    assert deleted[0].chat_id == -1_000_000_000_123

    await source.stop()
    assert client.handlers == {}
    assert client.update_pump_stopped.is_set()
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
        supervisor=TaskSupervisor(),
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


async def test_event_source_drains_inflight_control_commands(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingRouter:
        async def route(self, message, *, execute=True):  # type: ignore[no-untyped-def]
            entered.set()
            await release.wait()
            return True

    client = FakeNativeClient()
    source = TelethonEventSource(
        client,  # type: ignore[arg-type]
        InProcessEventBus(),
        PeerRegistry(),
        supervisor=TaskSupervisor(),
        commands=BlockingRouter(),
        drain_timeout=0.2,
    )
    await source.start()
    handling = asyncio.create_task(
        client.handlers[NewEvent](  # type: ignore[operator]
            FakeMessage(1, FakePeer(-1001), text="/admin module list", outgoing=True)
        )
    )
    await entered.wait()

    stopping = asyncio.create_task(source.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    assert client.handlers == {}

    release.set()
    await asyncio.gather(handling, stopping)


async def test_commands_are_out_of_band_in_any_chat_and_edits_do_not_execute(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    client = FakeNativeClient()
    peers = PeerRegistry()
    bus = InProcessEventBus()
    ordinary: list[TelegramMessageReceived] = []
    edits: list[TelegramMessageEdited] = []
    handled: list[ControlCommand] = []

    async def handle(command: ControlCommand) -> CommandResult:
        handled.append(command)
        return CommandResult(f"route arguments: {command.raw_arguments}")

    async def on_ordinary(event: TelegramMessageReceived) -> None:
        ordinary.append(event)

    async def on_edit(event: TelegramMessageEdited) -> None:
        edits.append(event)

    registry = CommandRegistry()
    registry.register("/route", summary="routes", help_text="route help", handler=handle)
    dispatcher = CommandDispatcher(registry, OutgoingAuthorizer(), MemoryReceipts())
    router = TelegramCommandRouter(
        dispatcher,
        client,  # type: ignore[arg-type]
        peers,
        TelegramRequestLimiter(),
    )
    bus.subscribe(TelegramMessageReceived, on_ordinary)
    bus.subscribe(TelegramMessageEdited, on_edit)
    source = TelethonEventSource(
        client,  # type: ignore[arg-type]
        bus,
        peers,
        supervisor=TaskSupervisor(),
        commands=router,
    )
    await source.start()

    chat = FakePeer(-4321, "arbitrary chat")
    owner = FakePeer(999, "owner")
    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(10, chat, text="/route  list", sender=owner, outgoing=True)
    )

    assert len(handled) == 1
    assert handled[0].raw_arguments == " list"
    assert ordinary == []
    assert client.calls == [("message", -4321, "route arguments:  list", None, 10)]

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(
            100,
            chat,
            text="route arguments:  list",
            sender=owner,
            outgoing=True,
            replied_message_id=10,
        )
    )
    assert ordinary == []
    assert len(handled) == 1

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(11, chat, text="/unknown value", sender=owner, outgoing=True)
    )
    assert len(ordinary) == 1
    assert ordinary[0].message.text == "/unknown value"

    await client.handlers[EditEvent](  # type: ignore[operator]
        FakeMessage(12, chat, text="/route remove 1", sender=owner, outgoing=True)
    )
    assert len(handled) == 1
    assert edits == []

    await source.stop()
