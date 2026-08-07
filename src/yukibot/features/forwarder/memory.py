"""In-memory adapters for tests, development and small embedded use cases."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence

from .models import (
    ManagedTopic,
    MessageLink,
    MessageRef,
    PollCursor,
    Route,
    RouteDraft,
    normalize_general_topic,
)
from .recovery import ChatAccess
from .routing import assert_acyclic_routes


class InMemoryRouteRepository:
    def __init__(self, routes: Iterable[Route] = ()) -> None:
        self._routes = {route.id: route for route in routes}
        self._next_id = max(self._routes, default=0) + 1
        assert_acyclic_routes(self._routes.values())
        self._lock = asyncio.Lock()

    async def list_for_source_chat(self, chat_id: int) -> Sequence[Route]:
        async with self._lock:
            return tuple(
                route
                for route in sorted(self._routes.values(), key=lambda item: item.id)
                if route.source.chat_id == chat_id
            )

    async def list_all(self) -> Sequence[Route]:
        async with self._lock:
            return tuple(sorted(self._routes.values(), key=lambda item: item.id))

    async def add(self, route: Route) -> None:
        async with self._lock:
            if route.id in self._routes:
                raise ValueError(f"route {route.id} already exists")
            assert_acyclic_routes((*self._routes.values(), route))
            self._routes[route.id] = route
            self._next_id = max(self._next_id, route.id + 1)

    async def add_auto(self, draft: RouteDraft) -> Route:
        async with self._lock:
            route = draft.bind(self._next_id)
            assert_acyclic_routes((*self._routes.values(), route))
            self._routes[route.id] = route
            self._next_id += 1
            return route

    async def replace(self, route: Route) -> None:
        async with self._lock:
            if route.id not in self._routes:
                raise KeyError(route.id)
            proposed = {**self._routes, route.id: route}
            assert_acyclic_routes(proposed.values())
            self._routes[route.id] = route

    async def remove(self, route_id: int) -> bool:
        async with self._lock:
            return self._routes.pop(route_id, None) is not None


class InMemoryMessageLinkRepository:
    def __init__(self, links: Iterable[MessageLink] = ()) -> None:
        self._links = {(link.route_id, link.source): link for link in links}
        self._lock = asyncio.Lock()

    async def save_many(self, links: Sequence[MessageLink]) -> None:
        async with self._lock:
            for link in links:
                self._links[(link.route_id, link.source)] = link

    async def get(self, route_id: int, source: MessageRef) -> MessageLink | None:
        async with self._lock:
            return self._links.get((route_id, source))

    async def find_all(self, source: MessageRef) -> Sequence[MessageLink]:
        async with self._lock:
            return tuple(link for link in self._links.values() if link.source == source)

    async def find_by_source_message_id(self, message_id: int) -> Sequence[MessageLink]:
        async with self._lock:
            return tuple(
                link for link in self._links.values() if link.source.message_id == message_id
            )

    async def remove(self, link: MessageLink) -> None:
        async with self._lock:
            self._links.pop((link.route_id, link.source), None)

    async def count(self) -> int:
        async with self._lock:
            return len(self._links)


class InMemoryManagedTopicRepository:
    def __init__(self, topics: Iterable[ManagedTopic] = ()) -> None:
        self._topics = {
            (topic.source_chat_id, topic.source_topic_id, topic.destination_chat_id): topic
            for topic in topics
        }
        self._lock = asyncio.Lock()

    async def get(
        self,
        source_chat_id: int,
        source_topic_id: int | None,
        destination_chat_id: int,
    ) -> ManagedTopic | None:
        async with self._lock:
            normalized_topic_id = (
                None if source_topic_id is None else normalize_general_topic(source_topic_id)
            )
            return self._topics.get((source_chat_id, normalized_topic_id, destination_chat_id))

    async def save(self, topic: ManagedTopic) -> None:
        async with self._lock:
            self._topics[
                (topic.source_chat_id, topic.source_topic_id, topic.destination_chat_id)
            ] = topic


class InMemoryPollCursorRepository:
    def __init__(self, cursors: Iterable[PollCursor] = ()) -> None:
        self._cursors = {cursor.source_chat_id: cursor for cursor in cursors}
        self._lock = asyncio.Lock()

    async def get(self, source_chat_id: int) -> PollCursor | None:
        async with self._lock:
            return self._cursors.get(source_chat_id)

    async def save(self, cursor: PollCursor) -> None:
        async with self._lock:
            existing = self._cursors.get(cursor.source_chat_id)
            if existing is None or cursor.last_message_id > existing.last_message_id:
                self._cursors[cursor.source_chat_id] = cursor


class InMemoryChatAccessRepository:
    def __init__(self, items: Iterable[ChatAccess] = ()) -> None:
        self._items = {item.chat_id: item for item in items}
        self._lock = asyncio.Lock()

    async def get_many(self, chat_ids: Sequence[int]) -> Sequence[ChatAccess]:
        async with self._lock:
            return tuple(
                self._items[chat_id] for chat_id in sorted(set(chat_ids)) if chat_id in self._items
            )

    async def save(self, access: ChatAccess) -> None:
        async with self._lock:
            existing = self._items.get(access.chat_id)
            if existing is not None:
                access = ChatAccess(
                    access.chat_id,
                    access.title or existing.title,
                    access.username,
                    access.invite_link,
                )
            self._items[access.chat_id] = access
