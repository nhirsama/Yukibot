"""Account membership inspection and in-memory chat rebuilding."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .errors import RetryAfter
from .models import Route


class MembershipState(StrEnum):
    JOINED = "joined"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


class RebuildJoinResult(StrEnum):
    JOINED = "joined"
    ALREADY_JOINED = "already_joined"
    APPROVAL_PENDING = "approval_pending"


@dataclass(frozen=True, slots=True)
class ChatAccess:
    chat_id: int
    title: str | None = None
    username: str | None = None
    invite_link: str | None = None

    def __post_init__(self) -> None:
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        for name in ("title", "username", "invite_link"):
            value = getattr(self, name)
            if value is not None:
                normalized = value.strip()
                object.__setattr__(self, name, normalized or None)
        if self.username is not None:
            object.__setattr__(self, "username", self.username.removeprefix("@"))

    @property
    def public_link(self) -> str | None:
        return f"https://t.me/{self.username}" if self.username is not None else None

    @property
    def join_reference(self) -> str | None:
        return self.public_link or self.invite_link


@dataclass(frozen=True, slots=True)
class ChatInspection:
    access: ChatAccess
    joined: bool
    metadata_error: str | None = None


@dataclass(frozen=True, slots=True)
class MembershipItem:
    access: ChatAccess
    state: MembershipState
    route_ids: tuple[int, ...]
    roles: tuple[str, ...]
    metadata_error: str | None = None


@dataclass(frozen=True, slots=True)
class MembershipReport:
    items: tuple[MembershipItem, ...]
    updated: int

    def count(self, state: MembershipState) -> int:
        return sum(item.state is state for item in self.items)


@dataclass(frozen=True, slots=True)
class RebuildProgress:
    active: bool = False
    total: int = 0
    completed: int = 0
    joined: int = 0
    already_joined: int = 0
    approval_pending: int = 0
    failed: int = 0
    current_chat_id: int | None = None
    next_attempt_at: float | None = None
    failures: tuple[tuple[int, str], ...] = ()


class RouteReader(Protocol):
    async def list_all(self) -> Sequence[Route]: ...


class ChatAccessStore(Protocol):
    async def get_many(self, chat_ids: Sequence[int]) -> Sequence[ChatAccess]: ...

    async def save(self, access: ChatAccess) -> None: ...


class RecoveryGateway(Protocol):
    async def inspect_chats(self, chat_ids: Sequence[int]) -> Sequence[ChatInspection]: ...

    async def join_chat(self, access: ChatAccess) -> RebuildJoinResult: ...


@dataclass(slots=True)
class _AggregatedChat:
    chat_id: int
    route_ids: set[int] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    required: bool = False
    username: str | None = None


class MembershipRecoveryService:
    def __init__(
        self,
        routes: RouteReader,
        accesses: ChatAccessStore,
        gateway: RecoveryGateway,
        rebuilder: MembershipRebuilder,
    ) -> None:
        self._routes = routes
        self._accesses = accesses
        self._gateway = gateway
        self._rebuilder = rebuilder

    async def check(self, *, include_disabled: bool = True) -> MembershipReport:
        routes = tuple(await self._routes.list_all())
        selected = routes if include_disabled else tuple(route for route in routes if route.enabled)
        chats = _aggregate_chats(selected)
        if not chats:
            return MembershipReport((), 0)

        stored = {item.chat_id: item for item in await self._accesses.get_many(tuple(chats))}
        inspections = {
            item.access.chat_id: item
            for item in await self._gateway.inspect_chats(tuple(sorted(chats)))
        }
        updated = 0
        items: list[MembershipItem] = []
        for chat_id, aggregate in sorted(chats.items()):
            previous = stored.get(chat_id)
            fallback = ChatAccess(
                chat_id,
                title=previous.title if previous is not None else None,
                username=(previous.username if previous is not None else None)
                or aggregate.username,
                invite_link=previous.invite_link if previous is not None else None,
            )
            inspection = inspections.get(chat_id, ChatInspection(fallback, False))
            access = _merge_access(fallback, inspection.access) if inspection.joined else fallback
            if inspection.joined:
                if access != previous:
                    updated += 1
                await self._accesses.save(access)
            state = _membership_state(aggregate.required, inspection.joined, access)
            items.append(
                MembershipItem(
                    access,
                    state,
                    tuple(sorted(aggregate.route_ids)),
                    tuple(sorted(aggregate.roles)),
                    inspection.metadata_error,
                )
            )
        return MembershipReport(tuple(items), updated)

    async def rebuild(self, *, include_disabled: bool = False) -> MembershipReport:
        report = await self.check(include_disabled=include_disabled)
        targets = tuple(
            item.access for item in report.items if item.state is MembershipState.MISSING
        )
        self._rebuilder.start(targets)
        return report

    def progress(self) -> RebuildProgress:
        return self._rebuilder.progress

    def cancel(self) -> bool:
        return self._rebuilder.cancel()


class MembershipRebuilder:
    """Join missing chats serially while keeping operational state in memory."""

    def __init__(
        self,
        gateway: RecoveryGateway,
        *,
        min_interval: float = 300.0,
        max_interval: float = 600.0,
        max_attempts: int = 3,
        clock: Callable[[], float] = time.time,
        random_interval: Callable[[float, float], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if min_interval < 300:
            raise ValueError("rebuild interval must be at least 300 seconds")
        if max_interval < min_interval:
            raise ValueError("rebuild intervals must be ordered")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._gateway = gateway
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._max_attempts = max_attempts
        self._clock = clock
        generator = random.SystemRandom()
        self._random_interval = random_interval or generator.uniform
        self._logger = logger or logging.getLogger(__name__)
        self._queue: list[tuple[ChatAccess, int]] = []
        self._progress = RebuildProgress()
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def progress(self) -> RebuildProgress:
        return self._progress

    def prepare(self) -> None:
        self._stopping = False
        self._queue.clear()
        self._progress = RebuildProgress()
        self._wake.clear()

    def start(self, targets: Sequence[ChatAccess]) -> None:
        if self._progress.active:
            raise ValueError("已有频道重建任务正在运行")
        unique = {target.chat_id: target for target in targets}
        self._queue = [(unique[chat_id], 0) for chat_id in sorted(unique)]
        self._progress = RebuildProgress(active=bool(self._queue), total=len(self._queue))
        self._wake.set()

    def cancel(self) -> bool:
        if not self._progress.active:
            return False
        self._queue.clear()
        self._progress = _replace_progress(self._progress, active=False, current_chat_id=None)
        self._wake.set()
        return True

    def request_stop(self) -> None:
        self._stopping = True
        self.cancel()
        self._wake.set()

    async def run(self) -> None:
        while not self._stopping:
            if await self.process_once():
                continue
            self._wake.clear()
            if self._stopping:
                return
            timeout = _due_in(self._progress.next_attempt_at, self._clock())
            try:
                if timeout is None:
                    await self._wake.wait()
                else:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue

    async def process_once(self, now: float | None = None) -> bool:
        current = self._clock() if now is None else now
        if not self._progress.active or not self._queue:
            return False
        if self._progress.next_attempt_at is not None and self._progress.next_attempt_at > current:
            return False

        access, attempts = self._queue.pop(0)
        self._progress = _replace_progress(self._progress, current_chat_id=access.chat_id)
        retry_after: float | None = None
        error_text: str | None = None
        result: RebuildJoinResult | None = None
        try:
            result = await self._gateway.join_chat(access)
        except asyncio.CancelledError:
            raise
        except RetryAfter as error:
            retry_after = error.seconds
            error_text = str(error)
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"[:500]

        attempts += 1
        completed = self._progress.completed
        joined = self._progress.joined
        already_joined = self._progress.already_joined
        approval_pending = self._progress.approval_pending
        failed = self._progress.failed
        failures = self._progress.failures
        if result is RebuildJoinResult.JOINED:
            completed += 1
            joined += 1
        elif result is RebuildJoinResult.ALREADY_JOINED:
            completed += 1
            already_joined += 1
        elif result is RebuildJoinResult.APPROVAL_PENDING:
            completed += 1
            approval_pending += 1
        elif retry_after is not None and attempts < self._max_attempts:
            self._queue.insert(0, (access, attempts))
        else:
            completed += 1
            failed += 1
            failures = (*failures, (access.chat_id, error_text or "unknown error"))

        active = bool(self._queue)
        delay = self._random_interval(self._min_interval, self._max_interval)
        if not self._min_interval <= delay <= self._max_interval:
            raise ValueError("random rebuild interval is outside configured bounds")
        if retry_after is not None:
            delay = max(delay, retry_after)
        finished = self._clock() if now is None else current
        self._progress = RebuildProgress(
            active=active,
            total=self._progress.total,
            completed=completed,
            joined=joined,
            already_joined=already_joined,
            approval_pending=approval_pending,
            failed=failed,
            current_chat_id=None,
            next_attempt_at=finished + delay if active else None,
            failures=failures,
        )
        self._logger.info(
            "chat rebuild attempt completed",
            extra={
                "feature": "forwarder",
                "chat_id": access.chat_id,
                "attempt": attempts,
                "result": result.value if result is not None else "retry_or_failed",
                "next_attempt_in": delay if active else None,
            },
        )
        return True


def _aggregate_chats(routes: Sequence[Route]) -> dict[int, _AggregatedChat]:
    chats: dict[int, _AggregatedChat] = {}
    for route in routes:
        source = chats.setdefault(route.source.chat_id, _AggregatedChat(route.source.chat_id))
        source.route_ids.add(route.id)
        source.roles.add("poll_source" if route.source.is_polled else "source")
        source.required = source.required or not route.source.is_polled
        source.username = source.username or route.source.username

        destination = chats.setdefault(
            route.destination.chat_id, _AggregatedChat(route.destination.chat_id)
        )
        destination.route_ids.add(route.id)
        destination.roles.add("destination")
        destination.required = True
        destination.username = destination.username or route.destination.username
    return chats


def _merge_access(
    stored: ChatAccess,
    observed: ChatAccess,
) -> ChatAccess:
    return ChatAccess(
        stored.chat_id,
        observed.title or stored.title,
        observed.username,
        observed.invite_link
        or (stored.invite_link if _is_private_invite(stored.invite_link) else None),
    )


def _membership_state(
    required: bool,
    joined: bool,
    access: ChatAccess,
) -> MembershipState:
    if not required or access.chat_id > 0:
        return MembershipState.NOT_REQUIRED
    if joined:
        return MembershipState.JOINED
    if access.join_reference is not None:
        return MembershipState.MISSING
    return MembershipState.UNAVAILABLE


def _is_private_invite(link: str | None) -> bool:
    if link is None:
        return False
    normalized = link.casefold()
    return (
        "t.me/+" in normalized
        or "telegram.me/+" in normalized
        or "/joinchat/" in normalized
        or normalized.startswith("tg://join?")
    )


def _replace_progress(
    progress: RebuildProgress,
    *,
    active: bool | None = None,
    current_chat_id: int | None = None,
) -> RebuildProgress:
    return RebuildProgress(
        active=progress.active if active is None else active,
        total=progress.total,
        completed=progress.completed,
        joined=progress.joined,
        already_joined=progress.already_joined,
        approval_pending=progress.approval_pending,
        failed=progress.failed,
        current_chat_id=current_chat_id,
        next_attempt_at=progress.next_attempt_at,
        failures=progress.failures,
    )


def _due_in(due_at: float | None, now: float) -> float | None:
    return None if due_at is None else max(0.0, due_at - now)
