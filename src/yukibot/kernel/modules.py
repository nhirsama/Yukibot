"""Runtime enable/disable control for explicitly managed feature modules."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .errors import DuplicateFeatureError
from .feature import Feature


class ModuleNotFoundError(KeyError):
    """The requested managed module does not exist."""


class ModuleStateStore(Protocol):
    async def get_enabled(self, name: str) -> bool | None: ...

    async def set_enabled(self, name: str, *, enabled: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class ModuleStatus:
    name: str
    enabled: bool
    running: bool


class ModuleController:
    """Own module lifecycles without exposing infrastructure to management commands."""

    name = "modules"

    def __init__(self, modules: Iterable[Feature], states: ModuleStateStore) -> None:
        items = tuple(modules)
        names = [module.name for module in items]
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise DuplicateFeatureError(f"duplicate managed module name: {duplicate}")
        self._modules = {module.name: module for module in items}
        self._order = names
        self._states = states
        self._running: set[str] = set()
        self._started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            started: list[Feature] = []
            try:
                for name in self._order:
                    enabled = await self._states.get_enabled(name)
                    if enabled is None:
                        enabled = True
                        await self._states.set_enabled(name, enabled=True)
                    if not enabled:
                        continue
                    module = self._modules[name]
                    await module.start()
                    started.append(module)
                    self._running.add(name)
            except BaseException:
                for module in reversed(started):
                    await module.stop()
                    self._running.discard(module.name)
                raise
            self._started = True

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            failures: list[Exception] = []
            for name in reversed(self._order):
                if name not in self._running:
                    continue
                try:
                    await self._modules[name].stop()
                except Exception as error:
                    failures.append(error)
                else:
                    self._running.discard(name)
            self._started = False
            if failures:
                raise RuntimeError(
                    f"{len(failures)} managed module(s) failed to stop"
                ) from failures[0]

    async def list_modules(self) -> tuple[ModuleStatus, ...]:
        async with self._lock:
            statuses: list[ModuleStatus] = []
            for name in self._order:
                enabled = await self._states.get_enabled(name)
                statuses.append(ModuleStatus(name, enabled is not False, name in self._running))
            return tuple(statuses)

    async def enable(self, name: str) -> ModuleStatus:
        async with self._lock:
            module = self._require_module(name)
            if name in self._running:
                await self._states.set_enabled(name, enabled=True)
                return ModuleStatus(name, True, True)
            await self._states.set_enabled(name, enabled=True)
            try:
                await module.start()
            except BaseException:
                await self._states.set_enabled(name, enabled=False)
                raise
            self._running.add(name)
            return ModuleStatus(name, True, True)

    async def disable(self, name: str) -> ModuleStatus:
        async with self._lock:
            module = self._require_module(name)
            await self._states.set_enabled(name, enabled=False)
            if name not in self._running:
                return ModuleStatus(name, False, False)
            await module.stop()
            self._running.discard(name)
            return ModuleStatus(name, False, False)

    def _require_module(self, name: str) -> Feature:
        try:
            return self._modules[name]
        except KeyError as error:
            raise ModuleNotFoundError(f"module {name!r} does not exist") from error
