import asyncio

from tests.contract.adapters.telegram.conftest import FakeNativeClient
from yukibot.bootstrap import build_runtime
from yukibot.config import Settings
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
        if client.connected:
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
