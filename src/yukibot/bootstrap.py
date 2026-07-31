"""Explicit composition root for the Yukibot process."""

from __future__ import annotations

from dataclasses import dataclass

from yukibot.adapters.database import DatabaseLifecycle, SqliteDatabase
from yukibot.adapters.telegram import (
    NativeClient,
    PeerRegistry,
    TelegramRequestLimiter,
    TelethonClientLifecycle,
    TelethonEventSource,
    create_telethon_client,
)
from yukibot.config import Settings
from yukibot.features.forwarder.feature import ForwarderFeature
from yukibot.features.forwarder.infrastructure import TelethonGateway
from yukibot.features.forwarder.job_repository import SqliteForwardJobRepository
from yukibot.features.forwarder.migrations import FORWARDER_MIGRATIONS
from yukibot.features.forwarder.repository import (
    SqliteMessageLinkRepository,
    SqliteRouteRepository,
)
from yukibot.features.forwarder.service import ForwarderService
from yukibot.features.forwarder.worker import ForwardJobProcessor, ForwardJobRunner
from yukibot.kernel import (
    Application,
    InProcessEventBus,
    LifecycleManager,
    SupervisorLifecycle,
    TaskSupervisor,
)


@dataclass(frozen=True, slots=True)
class Runtime:
    application: Application
    bus: InProcessEventBus
    database: SqliteDatabase
    forwarder: ForwarderFeature
    telegram: TelethonEventSource


def build_runtime(
    settings: Settings,
    *,
    native_client: NativeClient | None = None,
) -> Runtime:
    """Construct all concrete dependencies without connecting to external systems."""

    bus = InProcessEventBus()
    supervisor = TaskSupervisor()
    database = SqliteDatabase(settings.database_url)
    database_lifecycle = DatabaseLifecycle(database, FORWARDER_MIGRATIONS)

    client = native_client or create_telethon_client(
        settings.telegram_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
    )
    peers = PeerRegistry()
    telegram_client_lifecycle = TelethonClientLifecycle(client, peers)
    request_limiter = TelegramRequestLimiter()
    telegram_gateway = TelethonGateway(client, peers, request_limiter=request_limiter)
    routes = SqliteRouteRepository(database)
    links = SqliteMessageLinkRepository(database)
    jobs = SqliteForwardJobRepository(database)
    service = ForwarderService(routes, links, telegram_gateway)
    processor = ForwardJobProcessor(service)
    runner = ForwardJobRunner(jobs, processor)
    forwarder_feature = ForwarderFeature(
        bus,
        runner,
        supervisor,
        album_delay=settings.forwarder_album_delay,
        stop_timeout=settings.shutdown_timeout,
    )
    telegram_source = TelethonEventSource(
        client,
        bus,
        peers,
        drain_timeout=settings.shutdown_timeout,
    )

    lifecycle = LifecycleManager(
        (
            database_lifecycle,
            telegram_client_lifecycle,
            SupervisorLifecycle(supervisor, timeout=settings.shutdown_timeout),
            forwarder_feature,
            telegram_source,
        )
    )
    application = Application(lifecycle, supervisor)
    return Runtime(application, bus, database, forwarder_feature, telegram_source)


def build_application(
    settings: Settings, *, native_client: NativeClient | None = None
) -> Application:
    return build_runtime(settings, native_client=native_client).application
