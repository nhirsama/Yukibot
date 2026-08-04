"""Application service for idempotent forwarding-route management."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import RouteNotFoundError
from .models import Route
from .ports import RouteRepository


@dataclass(slots=True)
class ForwarderManagementService:
    routes: RouteRepository

    async def list_routes(self) -> tuple[Route, ...]:
        return tuple(await self.routes.list_all())

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
                return existing
            raise ValueError(f"route {route.id} already exists with different configuration")
        await self.routes.add(route)
        return route

    async def replace_route(self, route: Route) -> Route:
        try:
            await self.routes.replace(route)
        except KeyError as error:
            raise RouteNotFoundError(f"route {route.id} does not exist") from error
        return route

    async def set_enabled(self, route_id: int, *, enabled: bool) -> Route:
        route = replace(await self.get_route(route_id), enabled=enabled)
        await self.routes.replace(route)
        return route

    async def remove_route(self, route_id: int) -> None:
        await self.routes.remove(route_id)
