"""Application service for idempotent forwarding-route management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from .errors import RouteNotFoundError
from .models import ChatIdentity, PollCursor, Route, RouteDraft
from .ports import PollCursorRepository, RouteRepository, TelegramSourceGateway
from .recovery import ChatAccess, ChatAccessStore
from .topics import ManagedTopicService


@dataclass(slots=True)
class ForwarderManagementService:
    routes: RouteRepository
    topics: ManagedTopicService | None = None
    sources: TelegramSourceGateway | None = None
    poll_cursors: PollCursorRepository | None = None
    chat_accesses: ChatAccessStore | None = None
    _add_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def resolve_chat(self, reference: str) -> ChatIdentity:
        if self.sources is not None:
            return await self.sources.resolve_chat(reference)
        try:
            return ChatIdentity(int(reference))
        except ValueError:
            raise ValueError("chat reference must be a numeric ID") from None

    async def list_routes(self) -> tuple[Route, ...]:
        return tuple(await self.routes.list_all())

    async def remember_chat_accesses(self, identities: tuple[ChatIdentity, ...]) -> None:
        if self.chat_accesses is None:
            return
        for identity in identities:
            public_link = (
                f"https://t.me/{identity.username}" if identity.username is not None else None
            )
            await self.chat_accesses.save(
                ChatAccess(
                    identity.chat_id,
                    title=(
                        self.sources.chat_title(identity.chat_id)
                        if self.sources is not None
                        else None
                    ),
                    username=identity.username,
                    invite_link=identity.invite_link or public_link,
                )
            )

    def route_titles(self, route: Route) -> tuple[str | None, str | None]:
        if self.sources is None:
            return None, None
        return (
            self.sources.chat_title(route.source.chat_id),
            self.sources.chat_title(route.destination.chat_id),
        )

    async def get_route(self, route_id: int) -> Route:
        for route in await self.routes.list_all():
            if route.id == route_id:
                return route
        raise RouteNotFoundError(f"route {route_id} does not exist")

    async def add_route(self, route: Route) -> Route:
        existing = next(
            (item for item in await self.routes.list_all() if item.id == route.id),
            None,
        )
        if existing is not None:
            if existing == route:
                await self._prepare_source(existing)
                await self._prepare_topic(existing)
                return existing
            raise ValueError(f"route {route.id} already exists with different configuration")
        await self._prepare_source(route)
        await self.routes.add(route)
        await self._prepare_topic(route)
        return route

    async def add_generated_route(self, draft: RouteDraft) -> Route:
        async with self._add_lock:
            existing = next(
                (route for route in await self.routes.list_all() if draft.matches(route)),
                None,
            )
            if existing is not None:
                await self._prepare_source(existing)
                await self._prepare_topic(existing)
                return existing
            provisional = draft.bind(1)
            await self._prepare_source(provisional)
            route = await self.routes.add_auto(draft)
            await self._prepare_topic(route)
            return route

    async def replace_route(self, route: Route) -> Route:
        await self.get_route(route.id)
        await self._prepare_source(route)
        try:
            await self.routes.replace(route)
        except KeyError as error:
            raise RouteNotFoundError(f"route {route.id} does not exist") from error
        await self._prepare_topic(route)
        return route

    async def set_enabled(self, route_id: int, *, enabled: bool) -> Route:
        route = replace(await self.get_route(route_id), enabled=enabled)
        if enabled:
            await self._prepare_source(route)
        await self.routes.replace(route)
        await self._prepare_topic(route)
        return route

    async def remove_route(self, route_id: int) -> None:
        await self.routes.remove(route_id)

    async def _prepare_topic(self, route: Route) -> None:
        if route.enabled and self.topics is not None:
            source_title = (
                self.sources.chat_title(route.source.chat_id) if self.sources is not None else None
            )
            await self.topics.resolve(route, source_title=source_title)

    async def _prepare_source(self, route: Route) -> None:
        if not route.enabled or self.sources is None:
            return
        await self.sources.ensure_source(route.source, join=not route.source.is_polled)
        if not route.source.is_polled or self.poll_cursors is None:
            return
        if await self.poll_cursors.get(route.source.chat_id) is not None:
            return
        latest = await self.sources.latest_message_id(route.source)
        await self.poll_cursors.save(PollCursor(route.source.chat_id, latest))
