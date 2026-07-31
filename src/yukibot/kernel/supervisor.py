"""Ownership and shutdown for long-running asyncio tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from .errors import SupervisorClosedError


@dataclass(frozen=True, slots=True)
class TaskFailure:
    task_name: str
    error: BaseException
    critical: bool


class TaskSupervisor:
    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._critical: set[asyncio.Task[Any]] = set()
        self._failures: list[TaskFailure] = []
        self._failure_event = asyncio.Event()
        self._closed = False
        self._logger = logger or logging.getLogger(__name__)

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    @property
    def failures(self) -> tuple[TaskFailure, ...]:
        return tuple(self._failures)

    @property
    def failure_event(self) -> asyncio.Event:
        return self._failure_event

    def create_task[T](
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        name: str,
        critical: bool = False,
    ) -> asyncio.Task[T]:
        if self._closed:
            coroutine.close()
            raise SupervisorClosedError("task supervisor is closed")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        if critical:
            self._critical.add(task)
        task.add_done_callback(self._on_done)
        return task

    async def stop(self, *, timeout: float = 10.0) -> None:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        if self._closed and not self._tasks:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            self._logger.error(
                "tasks did not stop before timeout",
                extra={"task_count": len(pending), "timeout": timeout},
            )
        await asyncio.gather(*done, return_exceptions=True)

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        critical = task in self._critical
        self._critical.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        failure = TaskFailure(task.get_name(), error, critical)
        self._failures.append(failure)
        if critical:
            self._failure_event.set()
        self._logger.error(
            "background task failed",
            extra={
                "task": task.get_name(),
                "critical": critical,
                "error_type": type(error).__name__,
            },
            exc_info=error,
        )


class SupervisorLifecycle:
    name = "task-supervisor"

    def __init__(self, supervisor: TaskSupervisor, *, timeout: float = 10.0) -> None:
        self._supervisor = supervisor
        self._timeout = timeout

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._supervisor.stop(timeout=self._timeout)
