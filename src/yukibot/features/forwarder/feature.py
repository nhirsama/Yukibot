"""Framework integration for durable forwarding jobs."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from yukibot.contracts import (
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
)
from yukibot.kernel import (
    CommandHandler,
    CommandRegistry,
    CommandSubscription,
    EventBus,
    Subscription,
    TaskSupervisor,
)

from .jobs import ForwardJobEvent, pending_jobs_for_event
from .poller import SourcePoller
from .worker import ForwardJobRunner


class ForwarderFeature:
    name = "forwarder"

    def __init__(
        self,
        bus: EventBus,
        runner: ForwardJobRunner,
        supervisor: TaskSupervisor,
        *,
        command_registry: CommandRegistry | None = None,
        command_handler: CommandHandler | None = None,
        album_delay: float = 0.8,
        stop_timeout: float = 15.0,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
        poller: SourcePoller | None = None,
    ) -> None:
        if album_delay < 0:
            raise ValueError("album_delay must not be negative")
        if stop_timeout <= 0:
            raise ValueError("stop_timeout must be positive")
        if (command_registry is None) != (command_handler is None):
            raise ValueError("command registry and handler must be provided together")
        self._bus = bus
        self._runner = runner
        self._supervisor = supervisor
        self._command_registry = command_registry
        self._command_handler = command_handler
        self._command_subscription: CommandSubscription | None = None
        self._album_delay = album_delay
        self._stop_timeout = stop_timeout
        self._clock = clock
        self._subscriptions: list[Subscription] = []
        self._worker: asyncio.Task[None] | None = None
        self._logger = logger or logging.getLogger(__name__)
        self._poller = poller
        self._polling_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is not None:
            return
        recovered = await self._runner.prepare()
        self._worker = self._supervisor.create_task(
            self._runner.run(),
            name="forwarder:worker",
            critical=True,
        )
        try:
            self._subscriptions.append(
                self._bus.subscribe(TelegramMessageReceived, self._on_message)
            )
            self._subscriptions.append(self._bus.subscribe(TelegramMessageEdited, self._on_edit))
            self._subscriptions.append(
                self._bus.subscribe(TelegramMessagesDeleted, self._on_delete)
            )
            if self._command_registry is not None and self._command_handler is not None:
                from .commands import ROUTE_HELP

                self._command_subscription = self._command_registry.register(
                    "/route",
                    summary="管理消息转发路由",
                    help_text=ROUTE_HELP,
                    handler=self._command_handler,
                )
            if self._poller is not None:
                self._poller.prepare()
                self._polling_task = self._supervisor.create_task(
                    self._poller.run(),
                    name="forwarder:source-poller",
                    critical=True,
                )
        except BaseException:
            if self._command_subscription is not None:
                self._command_subscription.unregister()
                self._command_subscription = None
            for subscription in self._subscriptions:
                subscription.unsubscribe()
            self._subscriptions.clear()
            self._runner.request_stop()
            if self._poller is not None:
                self._poller.request_stop()
            if self._polling_task is not None:
                self._polling_task.cancel()
                await asyncio.gather(self._polling_task, return_exceptions=True)
                self._polling_task = None
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            raise
        self._runner.wake()
        if recovered:
            self._logger.warning(
                "recovered interrupted forwarder jobs",
                extra={"feature": self.name, "job_count": recovered},
            )

    async def stop(self) -> None:
        polling_task, self._polling_task = self._polling_task, None
        if polling_task is not None:
            if self._poller is not None:
                self._poller.request_stop()
            try:
                await asyncio.wait_for(
                    asyncio.shield(polling_task),
                    timeout=self._stop_timeout,
                )
            except TimeoutError:
                self._logger.error(
                    "forwarder source poller did not stop before timeout",
                    extra={"feature": self.name, "timeout": self._stop_timeout},
                )
                polling_task.cancel()
                await asyncio.gather(polling_task, return_exceptions=True)
            except Exception:
                await asyncio.gather(polling_task, return_exceptions=True)
        if self._command_subscription is not None:
            self._command_subscription.unregister()
            self._command_subscription = None
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()

        worker, self._worker = self._worker, None
        if worker is None:
            return
        self._runner.request_stop()
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=self._stop_timeout)
        except TimeoutError:
            self._logger.error(
                "forwarder worker did not drain before timeout",
                extra={"feature": self.name, "timeout": self._stop_timeout},
            )
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        except Exception:
            await asyncio.gather(worker, return_exceptions=True)

    async def _on_message(self, event: TelegramMessageReceived) -> None:
        await self._enqueue(event)

    async def _on_edit(self, event: TelegramMessageEdited) -> None:
        await self._enqueue(event)

    async def _on_delete(self, event: TelegramMessagesDeleted) -> None:
        await self._enqueue(event)

    async def _enqueue(self, event: ForwardJobEvent) -> None:
        jobs = pending_jobs_for_event(
            event,
            now=self._clock(),
            album_delay=self._album_delay,
        )
        inserted = await self._runner.enqueue(jobs)
        self._logger.debug(
            "forwarder event persisted",
            extra={
                "feature": self.name,
                "event_type": type(event).__name__,
                "job_count": inserted,
                "deduplicated": inserted == 0,
            },
        )
