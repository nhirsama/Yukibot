"""Top-level asynchronous application run loop."""

from __future__ import annotations

import asyncio
import logging

from .lifecycle import LifecycleManager
from .shutdown import ShutdownCoordinator
from .supervisor import TaskSupervisor


class Application:
    def __init__(
        self,
        lifecycle: LifecycleManager,
        supervisor: TaskSupervisor,
        *,
        shutdown: ShutdownCoordinator | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.supervisor = supervisor
        self.shutdown = shutdown or ShutdownCoordinator()
        self._logger = logger or logging.getLogger(__name__)

    async def run(self, *, install_signal_handlers: bool = True) -> None:
        if install_signal_handlers:
            self.shutdown.install_signal_handlers()
        try:
            await self.lifecycle.start()
            await self._wait_for_shutdown()
        finally:
            if install_signal_handlers:
                self.shutdown.remove_signal_handlers()
            await self.lifecycle.stop()

    def request_shutdown(self, reason: str = "requested") -> None:
        self.shutdown.request(reason)

    async def _wait_for_shutdown(self) -> None:
        shutdown_waiter = asyncio.create_task(self.shutdown.wait(), name="application-shutdown")
        failure_waiter = asyncio.create_task(
            self.supervisor.failure_event.wait(), name="critical-task-failure"
        )
        try:
            done, _ = await asyncio.wait(
                (shutdown_waiter, failure_waiter), return_when=asyncio.FIRST_COMPLETED
            )
            if failure_waiter in done and self.supervisor.failure_event.is_set():
                self.shutdown.request("critical_task_failed")
            reason = await self.shutdown.wait()
            self._logger.info("application shutdown requested", extra={"reason": reason})
        finally:
            for waiter in (shutdown_waiter, failure_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(shutdown_waiter, failure_waiter, return_exceptions=True)
