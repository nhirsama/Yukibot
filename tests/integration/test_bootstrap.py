import asyncio
from datetime import UTC, datetime

from tests.contract.adapters.telegram.conftest import (
    FakeDialog,
    FakeMessage,
    FakeNativeClient,
    FakePeer,
)
from yukibot.adapters.database import MigrationRunner, SqliteDatabase
from yukibot.bootstrap import build_runtime
from yukibot.config import Settings
from yukibot.contracts import (
    MessageRef,
    TelegramContentType,
    TelegramMessage,
    TelegramMessageReceived,
)
from yukibot.features.forwarder import DestinationEndpoint, Route, SourceEndpoint
from yukibot.features.forwarder.job_repository import SqliteForwardJobRepository
from yukibot.features.forwarder.jobs import pending_jobs_for_event
from yukibot.features.forwarder.migrations import FORWARDER_MIGRATIONS
from yukibot.features.forwarder.repository import SqliteRouteRepository
from yukibot.kernel import LifecycleState


class NewEvent:
    pass


class EditEvent:
    pass


class DeleteEvent:
    pass


async def test_composed_runtime_starts_and_stops_all_resources(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session_path=tmp_path / "user.session",
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        forwarder_album_delay=0,
    )
    client = FakeNativeClient()
    runtime = build_runtime(settings, native_client=client)  # type: ignore[arg-type]

    task = asyncio.create_task(runtime.application.run(install_signal_handlers=False))
    for _ in range(100):
        if len(runtime.application.lifecycle.started_features) == 6:
            break
        await asyncio.sleep(0.001)
    assert client.connected
    assert client.update_pump_calls == 1
    assert runtime.application.lifecycle.started_features == (
        "database",
        "telegram-client",
        "task-supervisor",
        "management",
        "modules",
        "telegram",
    )

    runtime.application.request_shutdown("test")
    await asyncio.wait_for(task, timeout=1)

    assert client.disconnected
    assert client.update_pump_stopped.is_set()
    assert client.handlers == {}
    assert runtime.application.lifecycle.state is LifecycleState.STOPPED
    assert not await runtime.database.ping()


async def test_composed_runtime_persists_and_deduplicates_forwarding(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        database_url=f"sqlite:///{tmp_path / 'forwarding.db'}",
        forwarder_album_delay=0,
    )
    source = FakePeer(-1001)
    destination = FakePeer(-2001)
    client = FakeNativeClient()
    client.dialogs.extend((FakeDialog(source), FakeDialog(destination)))
    client.messages[(-1001, 10)] = FakeMessage(10, source)
    runtime = build_runtime(settings, native_client=client)  # type: ignore[arg-type]
    running = asyncio.create_task(runtime.application.run(install_signal_handlers=False))
    for _ in range(100):
        if len(runtime.application.lifecycle.started_features) == 6:
            break
        await asyncio.sleep(0.001)

    routes = SqliteRouteRepository(runtime.database)
    await routes.add(Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001)))
    event = TelegramMessageReceived(
        TelegramMessage(
            MessageRef(-1001, 10),
            TelegramContentType.TEXT,
            datetime.now(UTC),
            text="hello",
        )
    )
    await runtime.bus.publish(event)
    for _ in range(100):
        if client.calls:
            break
        await asyncio.sleep(0.001)
    assert len(client.calls) == 1

    await runtime.bus.publish(event)
    await asyncio.sleep(0.01)
    assert len(client.calls) == 1

    runtime.application.request_shutdown("test")
    await asyncio.wait_for(running, timeout=1)


async def test_recovered_jobs_run_only_after_telegram_is_ready(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    database_url = f"sqlite:///{tmp_path / 'recovered.db'}"
    event = TelegramMessageReceived(
        TelegramMessage(
            MessageRef(-1001, 10),
            TelegramContentType.TEXT,
            datetime.now(UTC),
            text="hello",
        )
    )
    async with SqliteDatabase(database_url) as database:
        await MigrationRunner(database, FORWARDER_MIGRATIONS).upgrade()
        await SqliteRouteRepository(database).add(
            Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))
        )
        jobs = SqliteForwardJobRepository(database)
        await jobs.enqueue(pending_jobs_for_event(event, now=100.0, album_delay=0))
        assert len(await jobs.claim_due(100.0)) == 1

    source = FakePeer(-1001)
    destination = FakePeer(-2001)
    client = FakeNativeClient()
    client.dialogs.extend((FakeDialog(source), FakeDialog(destination)))
    client.messages[(-1001, 10)] = FakeMessage(10, source)
    runtime = build_runtime(
        Settings(
            telegram_api_id=1,
            telegram_api_hash="hash",
            database_url=database_url,
            forwarder_album_delay=0,
        ),
        native_client=client,  # type: ignore[arg-type]
    )
    running = asyncio.create_task(runtime.application.run(install_signal_handlers=False))
    for _ in range(100):
        if client.calls:
            break
        await asyncio.sleep(0.001)

    assert len(client.calls) == 1
    runtime.application.request_shutdown("test")
    await asyncio.wait_for(running, timeout=1)


async def test_runtime_control_plane_manages_modules_admins_and_routes(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(
        "yukibot.adapters.telegram.event_source.telethon_event_types",
        lambda: (NewEvent, EditEvent, DeleteEvent),
    )
    settings = Settings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        forwarder_album_delay=0,
    )
    client = FakeNativeClient()
    runtime = build_runtime(settings, native_client=client)  # type: ignore[arg-type]
    ordinary_messages: list[TelegramMessageReceived] = []

    async def record_ordinary(event: TelegramMessageReceived) -> None:
        ordinary_messages.append(event)

    runtime.bus.subscribe(TelegramMessageReceived, record_ordinary)
    running = asyncio.create_task(runtime.application.run(install_signal_handlers=False))
    for _ in range(100):
        if len(runtime.application.lifecycle.started_features) == 6:
            break
        await asyncio.sleep(0.001)

    chat = FakePeer(-4321, "control chat")
    owner = client.me
    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(1, chat, text="/help", sender=owner, outgoing=True)
    )
    help_response = client.calls[-1]
    assert help_response[0:2] == ("message", -4321)
    assert "/admin - 管理管理员和功能模块" in str(help_response[2])
    assert "/route - 管理消息转发路由" in str(help_response[2])
    assert help_response[-1] == 1

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(
            2,
            chat,
            text="/admin module disable forwarder",
            sender=owner,
            outgoing=True,
        )
    )
    assert not runtime.commands.recognizes("/route list")
    assert (await runtime.modules.list_modules())[0].running is False

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(
            3,
            chat,
            text="/admin module enable forwarder",
            sender=owner,
            outgoing=True,
        )
    )
    assert runtime.commands.recognizes("/route list")
    assert (await runtime.modules.list_modules())[0].running is True

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(
            4,
            chat,
            text="/route add 7 -1001 -2001",
            sender=owner,
            outgoing=True,
        )
    )
    routes = await SqliteRouteRepository(runtime.database).list_all()
    assert [route.id for route in routes] == [7]

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(5, chat, text="/admin admin add 123", sender=owner, outgoing=True)
    )
    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(
            6,
            chat,
            text="/admin admin list",
            sender=owner,
            outgoing=True,
        )
    )
    assert client.calls[-1][2] == "owner: 999\nadmin: 123"

    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(
            7,
            chat,
            text="/route list",
            sender=FakePeer(123, "delegated admin"),
            outgoing=False,
        )
    )
    assert "7: -1001 -> -2001" in str(client.calls[-1][2])

    calls_before_unknown = len(client.calls)
    await client.handlers[NewEvent](  # type: ignore[operator]
        FakeMessage(8, chat, text="/unknown value", sender=owner, outgoing=True)
    )
    assert len(client.calls) == calls_before_unknown
    assert ordinary_messages[-1].message.text == "/unknown value"

    runtime.application.request_shutdown("test")
    await asyncio.wait_for(running, timeout=1)
