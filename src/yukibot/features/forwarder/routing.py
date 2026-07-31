"""Pure route graph validation."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import RouteCycleError
from .models import Route


def assert_acyclic_routes(routes: Iterable[Route]) -> None:
    """Reject enabled routes whose chat-level graph contains a cycle."""

    graph: dict[int, set[int]] = {}
    for route in routes:
        if route.enabled:
            graph.setdefault(route.source.chat_id, set()).add(route.destination.chat_id)

    visiting: set[int] = set()
    visited: set[int] = set()
    path: list[int] = []

    def visit(chat_id: int) -> None:
        if chat_id in visiting:
            cycle_start = path.index(chat_id)
            cycle = (*path[cycle_start:], chat_id)
            rendered = " -> ".join(str(item) for item in cycle)
            raise RouteCycleError(f"forwarding route cycle detected: {rendered}")
        if chat_id in visited:
            return

        visiting.add(chat_id)
        path.append(chat_id)
        for destination in graph.get(chat_id, ()):
            visit(destination)
        path.pop()
        visiting.remove(chat_id)
        visited.add(chat_id)

    for source in graph:
        visit(source)
