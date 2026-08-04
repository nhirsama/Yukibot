"""The /route command owned by the Forwarder module."""

from __future__ import annotations

import shlex

from yukibot.kernel import CommandResult, ControlCommand

from .errors import RouteNotFoundError
from .management import ForwarderManagementService
from .models import DestinationEndpoint, ForwardMode, Route, SourceEndpoint

ROUTE_HELP = """转发路由命令:
/route list - 列出全部路由
/route show <id> - 查看路由详情
/route add <id> <source_chat> <destination_chat> [选项] - 添加路由
/route set <id> <source_chat> <destination_chat> [选项] - 更新路由
/route enable <id> - 启用路由
/route disable <id> - 停用路由
/route remove <id> - 删除路由
选项: [copy|forward] [source_topic|-] [destination_topic|-]"""


class ForwarderCommands:
    def __init__(self, service: ForwarderManagementService) -> None:
        self._service = service

    async def handle(self, command: ControlCommand) -> CommandResult:
        try:
            arguments = shlex.split(command.raw_arguments)
        except ValueError as error:
            return CommandResult(f"Invalid arguments: {error}")
        if not arguments or arguments == ["help"]:
            return CommandResult(ROUTE_HELP)
        try:
            if arguments == ["list"]:
                routes = await self._service.list_routes()
                return CommandResult(
                    "\n".join(_route_summary(route) for route in routes) or "No forwarding routes."
                )
            if len(arguments) == 2 and arguments[0] == "show":
                route = await self._service.get_route(int(arguments[1]))
                return CommandResult(_route_details(route))
            if arguments[0] in {"add", "set"} and 4 <= len(arguments) <= 7:
                route = _parse_route(arguments[1:])
                if arguments[0] == "add":
                    await self._service.add_route(route)
                    return CommandResult(f"Route {route.id} is configured.")
                await self._service.replace_route(route)
                return CommandResult(f"Route {route.id} is updated.")
            if len(arguments) == 2 and arguments[0] in {"enable", "disable"}:
                enabled = arguments[0] == "enable"
                route = await self._service.set_enabled(int(arguments[1]), enabled=enabled)
                state = "enabled" if route.enabled else "disabled"
                return CommandResult(f"Route {route.id} is {state}.")
            if len(arguments) == 2 and arguments[0] == "remove":
                route_id = int(arguments[1])
                await self._service.remove_route(route_id)
                return CommandResult(f"Route {route_id} is removed.")
        except (ValueError, RouteNotFoundError) as error:
            return CommandResult(error.args[0] if error.args else str(error))
        return CommandResult(ROUTE_HELP)


def _parse_route(arguments: list[str]) -> Route:
    route_id = int(arguments[0])
    source_chat = int(arguments[1])
    destination_chat = int(arguments[2])
    mode = ForwardMode(arguments[3]) if len(arguments) >= 4 else ForwardMode.COPY
    source_topic = _topic_id(arguments[4]) if len(arguments) >= 5 else None
    destination_topic = _topic_id(arguments[5]) if len(arguments) >= 6 else None
    return Route(
        route_id,
        SourceEndpoint(source_chat, source_topic),
        DestinationEndpoint(destination_chat, destination_topic),
        mode=mode,
    )


def _topic_id(value: str) -> int | None:
    return None if value in {"-", "none"} else int(value)


def _route_summary(route: Route) -> str:
    state = "enabled" if route.enabled else "disabled"
    return (
        f"{route.id}: {_endpoint(route.source.chat_id, route.source.topic_id)} -> "
        f"{_endpoint(route.destination.chat_id, route.destination.topic_id)} "
        f"({route.mode.value}, {state})"
    )


def _route_details(route: Route) -> str:
    keywords = ", ".join(route.message_filter.keywords) or "none"
    allowed = ", ".join(sorted(route.message_filter.allowed_content_types)) or "all"
    blocked = ", ".join(sorted(route.message_filter.blocked_content_types)) or "none"
    return "\n".join(
        (
            _route_summary(route),
            f"keywords: {keywords}",
            f"allowed content: {allowed}",
            f"blocked content: {blocked}",
            f"service messages: {str(route.message_filter.include_service_messages).lower()}",
            f"fallback to copy: {str(route.fallback_to_copy).lower()}",
        )
    )


def _endpoint(chat_id: int, topic_id: int | None) -> str:
    return str(chat_id) if topic_id is None else f"{chat_id}/topic/{topic_id}"
