"""Explicit composition root for the Yukibot process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from yukibot.adapters.database import DatabaseLifecycle, SqliteDatabase
from yukibot.adapters.telegram import (
    NativeClient,
    PeerRegistry,
    TelethonClientLifecycle,
    TelethonEventSource,
    TelethonGateway,
    create_telethon_client,
)
from yukibot.config import Settings
from yukibot.features.forwarder import (
    FORWARDER_MIGRATIONS,
    Forwarder,
    ForwarderFeature,
    ForwarderService,
    ForwardingReport,
    SqliteMessageLinkRepository,
    SqliteRouteRepository,
)
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
    telegram_gateway = TelethonGateway(client, peers)
    routes = SqliteRouteRepository(database)
    links = SqliteMessageLinkRepository(database)
    service = ForwarderService(routes, links, telegram_gateway)

    report_logger = logging.getLogger("yukibot.features.forwarder.background")

    async def report_background(report: ForwardingReport) -> None:
        report_logger.log(
            logging.ERROR if report.failures else logging.INFO,
            "album forwarding completed",
            extra={
                "feature": "forwarder",
                "matched_routes": report.matched_routes,
                "delivered_messages": report.delivered_messages,
                "failure_count": len(report.failures),
            },
        )

    def report_background_error(error: BaseException) -> None:
        report_logger.error(
            "album forwarding failed",
            extra={"feature": "forwarder", "error_type": type(error).__name__},
            exc_info=error,
        )

    def create_album_task(
        coroutine: Coroutine[Any, Any, None],
    ) -> asyncio.Task[None]:
        return supervisor.create_task(coroutine, name="forwarder:album")

    forwarder = Forwarder(
        service,
        album_flush_delay=settings.forwarder_album_delay,
        on_background_report=report_background,
        on_background_error=report_background_error,
        task_factory=create_album_task,
    )
    forwarder_feature = ForwarderFeature(bus, forwarder)
    telegram_source = TelethonEventSource(client, bus, peers)

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
