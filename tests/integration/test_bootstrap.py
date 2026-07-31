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
        if len(runtime.application.lifecycle.started_features) == 5:
            break
        await asyncio.sleep(0.001)
    assert client.connected
    assert runtime.application.lifecycle.started_features == (
        "database",
        "telegram-client",
        "task-supervisor",
        "forwarder",
        "telegram",
    )

    runtime.application.request_shutdown("test")
    await asyncio.wait_for(task, timeout=1)

    assert client.disconnected
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
        if len(runtime.application.lifecycle.started_features) == 5:
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
