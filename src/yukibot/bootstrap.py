"""Explicit composition root for the Yukibot process."""

from __future__ import annotations

from dataclasses import dataclass

from yukibot.adapters.database import DatabaseLifecycle, SqliteDatabase
from yukibot.adapters.telegram import (
    AccountIdentity,
    NativeClient,
    PeerRegistry,
    TelegramCommandRouter,
    TelegramRequestLimiter,
    TelethonClientLifecycle,
    TelethonEventSource,
    create_telethon_client,
)
from yukibot.config import Settings
from yukibot.features.forwarder.commands import ForwarderCommands
from yukibot.features.forwarder.feature import ForwarderFeature
from yukibot.features.forwarder.infrastructure import TelethonGateway
from yukibot.features.forwarder.job_repository import SqliteForwardJobRepository
from yukibot.features.forwarder.management import ForwarderManagementService
from yukibot.features.forwarder.migrations import FORWARDER_MIGRATIONS
from yukibot.features.forwarder.repository import (
    SqliteMessageLinkRepository,
    SqliteRouteRepository,
)
from yukibot.features.forwarder.service import ForwarderService
from yukibot.features.forwarder.worker import ForwardJobProcessor, ForwardJobRunner
from yukibot.features.management.commands import ManagementCommands
from yukibot.features.management.feature import ManagementFeature
from yukibot.features.management.migrations import MANAGEMENT_MIGRATIONS
from yukibot.features.management.repository import SqliteManagementRepository
from yukibot.features.management.service import ManagementService
from yukibot.kernel import (
    Application,
    CommandDispatcher,
    CommandRegistry,
    InProcessEventBus,
    LifecycleManager,
    ModuleController,
    SupervisorLifecycle,
    TaskSupervisor,
)


@dataclass(frozen=True, slots=True)
class Runtime:
    application: Application
    bus: InProcessEventBus
    database: SqliteDatabase
    forwarder: ForwarderFeature
    management: ManagementFeature
    modules: ModuleController
    commands: CommandRegistry
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
    database_lifecycle = DatabaseLifecycle(
        database,
        (*FORWARDER_MIGRATIONS, *MANAGEMENT_MIGRATIONS),
    )

    client = native_client or create_telethon_client(
        settings.telegram_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
    )
    peers = PeerRegistry()
    identity = AccountIdentity()
    telegram_client_lifecycle = TelethonClientLifecycle(client, peers, identity)
    request_limiter = TelegramRequestLimiter()
    command_registry = CommandRegistry()
    management_repository = SqliteManagementRepository(database)
    telegram_gateway = TelethonGateway(client, peers, request_limiter=request_limiter)
    routes = SqliteRouteRepository(database)
    links = SqliteMessageLinkRepository(database)
    jobs = SqliteForwardJobRepository(database)
    service = ForwarderService(routes, links, telegram_gateway)
    forwarder_management = ForwarderManagementService(routes)
    forwarder_commands = ForwarderCommands(forwarder_management)
    processor = ForwardJobProcessor(service)
    runner = ForwardJobRunner(jobs, processor)
    forwarder_feature = ForwarderFeature(
        bus,
        runner,
        supervisor,
        command_registry=command_registry,
        command_handler=forwarder_commands.handle,
        album_delay=settings.forwarder_album_delay,
        stop_timeout=settings.shutdown_timeout,
    )
    modules = ModuleController((forwarder_feature,), management_repository)
    management_service = ManagementService(management_repository, modules, identity)
    management_commands = ManagementCommands(management_service)
    management_feature = ManagementFeature(command_registry, management_commands)
    command_dispatcher = CommandDispatcher(
        command_registry,
        management_service,
        management_repository,
    )
    telegram_commands = TelegramCommandRouter(
        command_dispatcher,
        client,
        peers,
        request_limiter,
    )
    telegram_source = TelethonEventSource(
        client,
        bus,
        peers,
        supervisor=supervisor,
        commands=telegram_commands,
        drain_timeout=settings.shutdown_timeout,
    )

    lifecycle = LifecycleManager(
        (
            database_lifecycle,
            telegram_client_lifecycle,
            SupervisorLifecycle(supervisor, timeout=settings.shutdown_timeout),
            management_feature,
            modules,
            telegram_source,
        )
    )
    application = Application(lifecycle, supervisor)
    return Runtime(
        application,
        bus,
        database,
        forwarder_feature,
        management_feature,
        modules,
        command_registry,
        telegram_source,
    )


def build_application(
    settings: Settings, *, native_client: NativeClient | None = None
) -> Application:
    return build_runtime(settings, native_client=native_client).application
