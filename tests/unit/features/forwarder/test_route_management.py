from __future__ import annotations

import pytest

from yukibot.features.forwarder import DestinationEndpoint, Route, SourceEndpoint
from yukibot.features.forwarder.commands import ROUTE_HELP
from yukibot.features.forwarder.management import ForwarderManagementService


class MemoryRoutes:
    def __init__(self) -> None:
        self.routes: dict[int, Route] = {}
        self.adds = 0
        self.replacements = 0

    async def list_for_source_chat(self, chat_id: int) -> tuple[Route, ...]:
        return tuple(route for route in self.routes.values() if route.source.chat_id == chat_id)

    async def list_all(self) -> tuple[Route, ...]:
        return tuple(self.routes[route_id] for route_id in sorted(self.routes))

    async def add(self, route: Route) -> None:
        self.adds += 1
        self.routes[route.id] = route

    async def replace(self, route: Route) -> None:
        if route.id not in self.routes:
            raise KeyError(route.id)
        self.replacements += 1
        self.routes[route.id] = route

    async def remove(self, route_id: int) -> bool:
        return self.routes.pop(route_id, None) is not None


def test_help_response_cannot_be_recognized_as_an_outgoing_command() -> None:
    assert not ROUTE_HELP.startswith("/")


async def test_route_management_uses_explicit_idempotent_desired_state() -> None:
    routes = MemoryRoutes()
    service = ForwarderManagementService(routes)
    route = Route(7, SourceEndpoint(-1001), DestinationEndpoint(-2001))

    assert await service.add_route(route) == route
    assert await service.add_route(route) == route
    assert routes.adds == 1

    disabled = await service.set_enabled(7, enabled=False)
    disabled_again = await service.set_enabled(7, enabled=False)
    assert disabled.enabled is False
    assert disabled_again == disabled

    await service.remove_route(7)
    await service.remove_route(7)
    assert await service.list_routes() == ()


async def test_same_route_id_cannot_silently_change_configuration() -> None:
    routes = MemoryRoutes()
    service = ForwarderManagementService(routes)
    await service.add_route(Route(7, SourceEndpoint(-1001), DestinationEndpoint(-2001)))

    with pytest.raises(ValueError, match="different configuration"):
        await service.add_route(Route(7, SourceEndpoint(-1001), DestinationEndpoint(-3001)))
