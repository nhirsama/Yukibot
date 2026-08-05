"""In-memory adapters for tests, development and small embedded use cases."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence

from .models import ManagedTopic, MessageLink, MessageRef, Route
from .routing import assert_acyclic_routes


class InMemoryRouteRepository:
    def __init__(self, routes: Iterable[Route] = ()) -> None:
        self._routes = {route.id: route for route in routes}
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
            (topic.source_chat_id, topic.destination_chat_id): topic for topic in topics
        }
        self._lock = asyncio.Lock()

    async def get(self, source_chat_id: int, destination_chat_id: int) -> ManagedTopic | None:
        async with self._lock:
            return self._topics.get((source_chat_id, destination_chat_id))

    async def save(self, topic: ManagedTopic) -> None:
        async with self._lock:
            self._topics[(topic.source_chat_id, topic.destination_chat_id)] = topic
