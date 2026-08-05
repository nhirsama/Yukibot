"""The /route command owned by the Forwarder module."""

from __future__ import annotations

import re
import shlex

from yukibot.kernel import CommandResult, ControlCommand

from .errors import ForwarderError, RouteNotFoundError
from .management import ForwarderManagementService
from .models import DestinationEndpoint, ForwardMode, Route, RouteDraft, SourceEndpoint

ROUTE_HELP = """转发路由命令:
/route list - 列出全部路由
/route show <id> - 查看路由详情
/route add <source> <destination> [选项] - 添加路由并自动分配 ID
/route set <id> <source> <destination> [选项] - 更新路由
/route enable <id> - 启用路由
/route disable <id> - 停用路由
/route remove <id> - 删除路由
source/destination 可使用数字 ID 或 @username
选项: [forward|copy] [source_topic|-] [destination_topic|-] [--poll <间隔>]
默认自动加入源频道并实时接收; --poll 5m 表示不自动加入, 每 5 分钟拉取一次。
轮询从配置后的新消息开始且不跟踪编辑/删除; 目标群必须已加入。
默认使用 forward; 目标为论坛且 destination_topic 为 - 时, 自动创建与源频道同名的话题。"""

_POLL_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhd]?)$")


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
                    "\n".join(
                        _route_summary(route, *self._service.route_titles(route))
                        for route in routes
                    )
                    or "No forwarding routes."
                )
            if len(arguments) == 2 and arguments[0] == "show":
                route = await self._service.get_route(int(arguments[1]))
                return CommandResult(_route_details(route, *self._service.route_titles(route)))
            if arguments[0] == "add" and len(arguments) >= 3:
                draft = await _parse_route_draft(arguments[1:], self._service)
                route = await self._service.add_generated_route(draft)
                return CommandResult(f"Route {route.id} is configured.")
            if arguments[0] == "set" and len(arguments) >= 4:
                route_id = int(arguments[1])
                draft = await _parse_route_draft(arguments[2:], self._service)
                route = draft.bind(route_id)
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
        except (ForwarderError, ValueError, RouteNotFoundError) as error:
            return CommandResult(error.args[0] if error.args else str(error))
        return CommandResult(ROUTE_HELP)


async def _parse_route_draft(
    arguments: list[str],
    service: ForwarderManagementService,
) -> RouteDraft:
    positional, poll_interval = _extract_poll_option(arguments)
    if not 2 <= len(positional) <= 5:
        raise ValueError("路由参数数量不正确")
    source = await service.resolve_chat(positional[0])
    destination = await service.resolve_chat(positional[1])
    mode = ForwardMode(positional[2]) if len(positional) >= 3 else ForwardMode.FORWARD
    source_topic = _topic_id(positional[3]) if len(positional) >= 4 else None
    destination_topic = _topic_id(positional[4]) if len(positional) >= 5 else None
    return RouteDraft(
        SourceEndpoint(
            source.chat_id,
            source_topic,
            username=source.username,
            poll_interval_seconds=poll_interval,
        ),
        DestinationEndpoint(
            destination.chat_id,
            destination_topic,
            username=destination.username,
        ),
        mode=mode,
    )


def _extract_poll_option(arguments: list[str]) -> tuple[list[str], int | None]:
    positional: list[str] = []
    poll_value: str | None = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--poll":
            if poll_value is not None or index + 1 >= len(arguments):
                raise ValueError("--poll 必须且只能指定一次间隔")
            poll_value = arguments[index + 1]
            index += 2
            continue
        if value.startswith("--poll="):
            if poll_value is not None:
                raise ValueError("--poll 必须且只能指定一次间隔")
            poll_value = value.partition("=")[2]
            index += 1
            continue
        if value.startswith("--"):
            raise ValueError(f"未知选项: {value}")
        positional.append(value)
        index += 1
    return positional, _poll_seconds(poll_value) if poll_value is not None else None


def _poll_seconds(value: str) -> int:
    match = _POLL_DURATION.fullmatch(value.casefold())
    if match is None:
        raise ValueError("轮询间隔格式应为 5m、2h 或 1d")
    amount = int(match.group("amount"))
    multiplier = {"": 60, "m": 60, "h": 3600, "d": 86400}[match.group("unit")]
    return amount * multiplier


def _topic_id(value: str) -> int | None:
    return None if value in {"-", "none"} else int(value)


def _route_summary(
    route: Route,
    source_title: str | None = None,
    destination_title: str | None = None,
) -> str:
    state = "enabled" if route.enabled else "disabled"
    access = (
        f", poll={_format_duration(route.source.poll_interval_seconds)}"
        if route.source.poll_interval_seconds is not None
        else ""
    )
    source = _endpoint(
        route.source.chat_id,
        route.source.topic_id,
        route.source.username,
        source_title,
    )
    destination = _endpoint(
        route.destination.chat_id,
        route.destination.topic_id,
        route.destination.username,
        destination_title,
    )
    return f"{route.id}: {source} -> {destination} ({route.mode.value}, {state}{access})"


def _route_details(
    route: Route,
    source_title: str | None = None,
    destination_title: str | None = None,
) -> str:
    keywords = ", ".join(route.message_filter.keywords) or "none"
    allowed = ", ".join(sorted(route.message_filter.allowed_content_types)) or "all"
    blocked = ", ".join(sorted(route.message_filter.blocked_content_types)) or "none"
    return "\n".join(
        (
            _route_summary(route, source_title, destination_title),
            f"keywords: {keywords}",
            f"allowed content: {allowed}",
            f"blocked content: {blocked}",
            f"service messages: {str(route.message_filter.include_service_messages).lower()}",
            f"fallback to copy: {str(route.fallback_to_copy).lower()}",
            f"source chat id: {route.source.chat_id}",
            f"destination chat id: {route.destination.chat_id}",
        )
    )


def _endpoint(
    chat_id: int,
    topic_id: int | None,
    username: str | None,
    title: str | None,
) -> str:
    reference = f"@{username}" if username is not None else str(chat_id)
    normalized_title = " ".join(title.split()) if title is not None else ""
    unavailable_titles = {reference, str(chat_id), f"Channel {chat_id}"}
    chat = (
        f"{normalized_title} ({reference})"
        if normalized_title and normalized_title not in unavailable_titles
        else reference
    )
    return chat if topic_id is None else f"{chat}/topic/{topic_id}"


def _format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"
