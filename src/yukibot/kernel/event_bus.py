"""Concurrent, failure-isolating in-process event dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol, TypeVar, cast

EventT = TypeVar("EventT")
EventHandler = Callable[[EventT], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    handler_name: str
    error: BaseException


@dataclass(frozen=True, slots=True)
class DispatchReport:
    event_type: type[object]
    handler_count: int
    failures: tuple[DispatchFailure, ...] = ()

    @property
    def succeeded(self) -> int:
        return self.handler_count - len(self.failures)


class Subscription:
    """Idempotent handle for removing one event subscription."""

    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def unsubscribe(self) -> None:
        if self._active:
            self._active = False
            self._unsubscribe()


class EventBus(Protocol):
    def subscribe(
        self, event_type: type[EventT], handler: EventHandler[EventT]
    ) -> Subscription: ...

    async def publish(self, event: object) -> DispatchReport: ...


class InProcessEventBus:
    """Dispatch exact event types concurrently without propagating handler errors."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._handlers: dict[type[object], list[EventHandler[object]]] = defaultdict(list)
        self._logger = logger or logging.getLogger(__name__)

    def subscribe(self, event_type: type[EventT], handler: EventHandler[EventT]) -> Subscription:
        erased_handler = cast(EventHandler[object], handler)
        handlers = self._handlers[event_type]
        if erased_handler in handlers:
            raise ValueError(
                f"handler {_handler_name(handler)!r} is already subscribed to {event_type.__name__}"
            )
        handlers.append(erased_handler)

        def unsubscribe() -> None:
            current = self._handlers.get(event_type)
            if current is None:
                return
            try:
                current.remove(erased_handler)
            except ValueError:
                return
            if not current:
                self._handlers.pop(event_type, None)

        return Subscription(unsubscribe)

    async def publish(self, event: object) -> DispatchReport:
        event_type = type(event)
        handlers = tuple(self._handlers.get(event_type, ()))
        if not handlers:
            return DispatchReport(event_type, 0)

        results = await asyncio.gather(
            *(self._invoke(handler, event) for handler in handlers),
            return_exceptions=False,
        )
        failures = tuple(result for result in results if result is not None)
        return DispatchReport(event_type, len(handlers), failures)

    async def _invoke(self, handler: EventHandler[object], event: object) -> DispatchFailure | None:
        name = _handler_name(handler)
        started = monotonic()
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._logger.exception(
                "event handler failed",
                extra={
                    "event_type": type(event).__name__,
                    "handler": name,
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                    "error_type": type(error).__name__,
                },
            )
            return DispatchFailure(name, error)
        else:
            self._logger.debug(
                "event handled",
                extra={
                    "event_type": type(event).__name__,
                    "handler": name,
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                },
            )
            return None


def _handler_name(handler: Callable[..., object]) -> str:
    if inspect.ismethod(handler):
        owner = handler.__self__.__class__.__qualname__
        return f"{owner}.{handler.__name__}"
    return getattr(handler, "__qualname__", repr(handler))
