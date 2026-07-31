"""Ordered application startup and reverse-order shutdown."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from enum import StrEnum

from .errors import DuplicateFeatureError, LifecycleStartError, LifecycleStopError
from .feature import Feature


class LifecycleState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class LifecycleManager:
    def __init__(
        self, features: Iterable[Feature], *, logger: logging.Logger | None = None
    ) -> None:
        self._features = tuple(features)
        names = [feature.name for feature in self._features]
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise DuplicateFeatureError(f"duplicate feature name: {duplicate}")
        self._started: list[Feature] = []
        self._state = LifecycleState.NEW
        self._logger = logger or logging.getLogger(__name__)

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def started_features(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self._started)

    async def start(self) -> None:
        if self._state is LifecycleState.RUNNING:
            return
        if self._state is not LifecycleState.NEW:
            raise RuntimeError(f"cannot start lifecycle in state {self._state}")
        self._state = LifecycleState.STARTING
        for feature in self._features:
            try:
                await feature.start()
            except BaseException as error:
                self._state = LifecycleState.FAILED
                rollback_failures = await self._stop_started()
                for name, rollback_error in rollback_failures:
                    self._logger.error(
                        "feature rollback failed",
                        extra={"feature": name, "error_type": type(rollback_error).__name__},
                        exc_info=rollback_error,
                    )
                if not isinstance(error, Exception):
                    raise
                raise LifecycleStartError(feature.name, error) from error
            self._started.append(feature)
            self._logger.info("feature started", extra={"feature": feature.name})
        self._state = LifecycleState.RUNNING

    async def stop(self) -> None:
        if self._state is LifecycleState.STOPPED:
            return
        if self._state is LifecycleState.NEW:
            self._state = LifecycleState.STOPPED
            return
        if self._state is LifecycleState.STOPPING:
            return
        self._state = LifecycleState.STOPPING
        failures = await self._stop_started()
        self._state = LifecycleState.STOPPED if not failures else LifecycleState.FAILED
        if failures:
            raise LifecycleStopError(failures)

    async def _stop_started(self) -> list[tuple[str, BaseException]]:
        failures: list[tuple[str, BaseException]] = []
        while self._started:
            feature = self._started.pop()
            try:
                await feature.stop()
            except Exception as error:
                failures.append((feature.name, error))
            else:
                self._logger.info("feature stopped", extra={"feature": feature.name})
        return failures
