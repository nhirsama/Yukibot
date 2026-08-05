"""SDK-independent forwarding use cases."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .errors import (
    DeliveryResultMismatch,
    MessageNotFound,
    MessageNotModified,
    NativeForwardUnsupported,
    PartialDeliveryState,
)
from .models import (
    ForwardMode,
    IncomingMessage,
    MessageLink,
    MessageRef,
    MessagesDeleted,
    Route,
    ServiceKind,
)
from .ports import MessageLinkRepository, RouteRepository, TelegramGateway
from .topics import ManagedTopicService


class SyncOperation(StrEnum):
    EDIT = "edit"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    route_id: int
    sources: tuple[MessageRef, ...]
    destinations: tuple[MessageRef, ...]
    mode_used: ForwardMode
    deduplicated: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    route_id: int
    sources: tuple[MessageRef, ...]
    error: Exception


@dataclass(frozen=True, slots=True)
class ForwardingReport:
    outcomes: tuple[DeliveryOutcome, ...] = ()
    failures: tuple[DeliveryFailure, ...] = ()
    matched_routes: int = 0
    ignored_reason: str | None = None
    buffered: bool = False

    @property
    def delivered_messages(self) -> int:
        return sum(
            len(outcome.destinations) for outcome in self.outcomes if not outcome.deduplicated
        )

    @property
    def deduplicated_messages(self) -> int:
        return sum(len(outcome.destinations) for outcome in self.outcomes if outcome.deduplicated)


@dataclass(frozen=True, slots=True)
class SyncFailure:
    operation: SyncOperation
    link: MessageLink
    error: Exception


@dataclass(frozen=True, slots=True)
class SyncReport:
    operation: SyncOperation
    synchronized: int = 0
    failures: tuple[SyncFailure, ...] = ()
    ignored_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ForwarderOptions:
    sync_edits: bool = True
    sync_deletes: bool = True
    allow_ambiguous_deletes: bool = False


@dataclass(slots=True)
class ForwarderService:
    """Coordinates routes, message mappings and Telegram delivery operations."""

    routes: RouteRepository
    links: MessageLinkRepository
    telegram: TelegramGateway
    options: ForwarderOptions = field(default_factory=ForwarderOptions)
    topics: ManagedTopicService | None = None
    _route_locks: dict[int, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def forward_message(self, message: IncomingMessage) -> ForwardingReport:
        new_title = _changed_title(message)
        if message.outgoing and new_title is None:
            return ForwardingReport(ignored_reason="outgoing_message")

        candidates = await self.routes.list_for_source_chat(message.ref.chat_id)
        matched = (
            ()
            if message.outgoing
            else tuple(route for route in candidates if route.matches(message))
        )
        matched_ids = {route.id for route in matched}
        outcomes: list[DeliveryOutcome] = []
        failures: list[DeliveryFailure] = []

        for route in candidates:
            if route.id not in matched_ids and (new_title is None or not route.enabled):
                continue
            async with self._route_lock(route.id):
                try:
                    effective_route = await self._resolve_destination(route, source_title=new_title)
                    if route.id not in matched_ids:
                        continue
                    existing = await self.links.get(route.id, message.ref)
                    if existing is not None:
                        outcomes.append(
                            DeliveryOutcome(
                                route.id,
                                (message.ref,),
                                (existing.destination,),
                                existing.delivery_mode,
                                deduplicated=True,
                            )
                        )
                        continue
                    reply_to = await self._resolve_reply(message, route)
                    destination, mode_used = await self._deliver_one(
                        message,
                        effective_route,
                        reply_to,
                    )
                    link = MessageLink(route.id, message.ref, destination, mode_used)
                    await self.links.save_many((link,))
                    outcomes.append(
                        DeliveryOutcome(route.id, (message.ref,), (destination,), mode_used)
                    )
                except Exception as error:
                    failures.append(DeliveryFailure(route.id, (message.ref,), error))

        return ForwardingReport(tuple(outcomes), tuple(failures), len(matched))

    async def forward_album(self, messages: tuple[IncomingMessage, ...]) -> ForwardingReport:
        ordered = self._validate_and_order_album(messages)
        if any(message.outgoing for message in ordered):
            return ForwardingReport(ignored_reason="outgoing_message")

        first = ordered[0]
        candidates = await self.routes.list_for_source_chat(first.ref.chat_id)
        matched = tuple(route for route in candidates if route.matches_album(ordered))
        sources = tuple(message.ref for message in ordered)
        outcomes: list[DeliveryOutcome] = []
        failures: list[DeliveryFailure] = []

        for route in matched:
            async with self._route_lock(route.id):
                try:
                    existing = tuple([await self.links.get(route.id, source) for source in sources])
                    if all(link is not None for link in existing):
                        persisted = tuple(link for link in existing if link is not None)
                        destinations = tuple(link.destination for link in persisted)
                        outcomes.append(
                            DeliveryOutcome(
                                route.id,
                                sources,
                                destinations,
                                persisted[0].delivery_mode,
                                deduplicated=True,
                            )
                        )
                        continue
                    if any(link is not None for link in existing):
                        raise PartialDeliveryState(
                            f"route {route.id} has an incomplete persisted album mapping"
                        )
                    reply_to = await self._resolve_reply(first, route)
                    effective_route = await self._resolve_destination(route)
                    destinations, mode_used = await self._deliver_album(
                        ordered,
                        effective_route,
                        reply_to,
                    )
                    if len(destinations) != len(ordered):
                        raise DeliveryResultMismatch(
                            f"sent {len(ordered)} album items but received "
                            f"{len(destinations)} destination references"
                        )
                    links = tuple(
                        MessageLink(route.id, source, destination, mode_used)
                        for source, destination in zip(sources, destinations, strict=True)
                    )
                    await self.links.save_many(links)
                    outcomes.append(DeliveryOutcome(route.id, sources, destinations, mode_used))
                except Exception as error:
                    failures.append(DeliveryFailure(route.id, sources, error))

        return ForwardingReport(tuple(outcomes), tuple(failures), len(matched))

    async def synchronize_edit(self, message: IncomingMessage) -> SyncReport:
        if not self.options.sync_edits:
            return SyncReport(SyncOperation.EDIT, ignored_reason="edit_sync_disabled")
        if message.outgoing:
            return SyncReport(SyncOperation.EDIT, ignored_reason="outgoing_message")

        links = await self.links.find_all(message.ref)
        synchronized = 0
        failures: list[SyncFailure] = []
        for link in links:
            if link.delivery_mode is ForwardMode.FORWARD:
                # Telegram owns the rendering of native forwards; their source
                # edit updates cannot be applied with edit_message.
                synchronized += 1
                continue
            try:
                await self.telegram.edit_from_source(message, link.destination)
            except MessageNotModified:
                synchronized += 1
            except MessageNotFound as error:
                await self.links.remove(link)
                failures.append(SyncFailure(SyncOperation.EDIT, link, error))
            except Exception as error:
                failures.append(SyncFailure(SyncOperation.EDIT, link, error))
            else:
                synchronized += 1
        return SyncReport(SyncOperation.EDIT, synchronized, tuple(failures))

    async def synchronize_delete(self, event: MessagesDeleted) -> SyncReport:
        if not self.options.sync_deletes:
            return SyncReport(SyncOperation.DELETE, ignored_reason="delete_sync_disabled")
        if event.chat_id is None and not self.options.allow_ambiguous_deletes:
            return SyncReport(SyncOperation.DELETE, ignored_reason="source_chat_unknown")

        links = await self._links_for_delete(event)
        synchronized = 0
        failures: list[SyncFailure] = []
        for link in links:
            try:
                await self.telegram.delete_message(link.destination)
            except MessageNotFound:
                await self.links.remove(link)
                synchronized += 1
            except Exception as error:
                failures.append(SyncFailure(SyncOperation.DELETE, link, error))
            else:
                await self.links.remove(link)
                synchronized += 1
        return SyncReport(SyncOperation.DELETE, synchronized, tuple(failures))

    async def _deliver_one(
        self,
        message: IncomingMessage,
        route: Route,
        reply_to_message_id: int | None,
    ) -> tuple[MessageRef, ForwardMode]:
        if message.service is not None:
            text = format_service_message(message)
            destination = await self.telegram.send_text(
                text,
                route.destination,
                reply_to_message_id=reply_to_message_id,
            )
            return destination, ForwardMode.COPY

        try:
            destination = await self.telegram.deliver_message(
                message,
                route.destination,
                mode=route.mode,
                reply_to_message_id=reply_to_message_id,
            )
            return destination, route.mode
        except NativeForwardUnsupported:
            if route.mode is not ForwardMode.FORWARD or not route.fallback_to_copy:
                raise
            destination = await self.telegram.deliver_message(
                message,
                route.destination,
                mode=ForwardMode.COPY,
                reply_to_message_id=reply_to_message_id,
            )
            return destination, ForwardMode.COPY

    async def _deliver_album(
        self,
        messages: tuple[IncomingMessage, ...],
        route: Route,
        reply_to_message_id: int | None,
    ) -> tuple[tuple[MessageRef, ...], ForwardMode]:
        try:
            result = await self.telegram.deliver_album(
                messages,
                route.destination,
                mode=route.mode,
                reply_to_message_id=reply_to_message_id,
            )
            return tuple(result), route.mode
        except NativeForwardUnsupported:
            if route.mode is not ForwardMode.FORWARD or not route.fallback_to_copy:
                raise
            result = await self.telegram.deliver_album(
                messages,
                route.destination,
                mode=ForwardMode.COPY,
                reply_to_message_id=reply_to_message_id,
            )
            return tuple(result), ForwardMode.COPY

    async def _resolve_reply(self, message: IncomingMessage, route: Route) -> int | None:
        if message.reply_to_message_id is None:
            return None
        parent = MessageRef(message.ref.chat_id, message.reply_to_message_id)
        mapping = await self.links.get(route.id, parent)
        return mapping.destination.message_id if mapping is not None else None

    async def _resolve_destination(
        self,
        route: Route,
        *,
        source_title: str | None = None,
    ) -> Route:
        if self.topics is None:
            return route
        destination = await self.topics.resolve(route, source_title=source_title)
        return (
            route if destination == route.destination else replace(route, destination=destination)
        )

    def _route_lock(self, route_id: int) -> asyncio.Lock:
        return self._route_locks.setdefault(route_id, asyncio.Lock())

    async def _links_for_delete(self, event: MessagesDeleted) -> tuple[MessageLink, ...]:
        found: dict[tuple[int, MessageRef], MessageLink] = {}
        for message_id in event.message_ids:
            if event.chat_id is None:
                matches = await self.links.find_by_source_message_id(message_id)
            else:
                matches = await self.links.find_all(MessageRef(event.chat_id, message_id))
            for link in matches:
                found[(link.route_id, link.source)] = link
        return tuple(found.values())

    @staticmethod
    def _validate_and_order_album(
        messages: tuple[IncomingMessage, ...],
    ) -> tuple[IncomingMessage, ...]:
        if not messages:
            raise ValueError("an album must contain at least one message")
        first = messages[0]
        if first.grouped_id is None:
            raise ValueError("album messages must have a grouped_id")
        expected = (first.ref.chat_id, first.topic_id, first.grouped_id)
        if any(
            (message.ref.chat_id, message.topic_id, message.grouped_id) != expected
            for message in messages
        ):
            raise ValueError("album messages must belong to the same chat, topic and media group")
        return tuple(sorted(messages, key=lambda message: message.ref.message_id))


def format_service_message(message: IncomingMessage) -> str:
    """Render normalized Telegram service data as plain text."""

    service = message.service
    if service is None:
        raise ValueError("message is not a service message")

    topic_suffix = f" in topic {message.topic_id}" if message.topic_id else ""
    if service.kind is ServiceKind.MEMBERS_JOINED:
        names = ", ".join(service.member_names) or "A member"
        return f"{names} joined the group{topic_suffix}."
    if service.kind is ServiceKind.MEMBER_LEFT:
        return f"{service.actor_name or 'A member'} left the group{topic_suffix}."
    if service.kind is ServiceKind.MESSAGE_PINNED:
        return f"A message was pinned by {service.actor_name or 'an administrator'}{topic_suffix}."
    if service.kind is ServiceKind.TITLE_CHANGED:
        title = service.new_title or "an unnamed title"
        return f"The group title was changed to {title}."
    if service.kind is ServiceKind.TOPIC_CREATED:
        return f"A topic was created{topic_suffix}."
    if service.kind is ServiceKind.TOPIC_CLOSED:
        return f"The topic was closed{topic_suffix}."
    if service.kind is ServiceKind.TOPIC_REOPENED:
        return f"The topic was reopened{topic_suffix}."
    return f"A system event occurred{topic_suffix}."


def _changed_title(message: IncomingMessage) -> str | None:
    service = message.service
    if service is None or service.kind is not ServiceKind.TITLE_CHANGED:
        return None
    return service.new_title
