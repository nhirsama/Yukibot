"""The /route command owned by the Forwarder module."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from yukibot.kernel import CommandResult, ControlCommand

from .errors import ForwarderError, RouteNotFoundError
from .management import ForwarderManagementService
from .models import (
    ChatIdentity,
    DestinationEndpoint,
    ForwardMode,
    Route,
    RouteDraft,
    SourceEndpoint,
)
from .recovery import (
    MembershipItem,
    MembershipRecoveryService,
    MembershipReport,
    MembershipState,
    RebuildProgress,
)

ROUTE_HELP = """转发路由命令:
/route list - 列出全部路由
/route show <id> - 查看路由详情
/route add <source> <destination> [选项] - 添加路由并自动分配 ID
/route set <id> <source> <destination> [选项] - 更新路由
/route enable <id> - 启用路由
/route disable <id> - 停用路由
/route remove <id> - 删除路由
/route check - 刷新频道名称、链接并检查当前账号加入状态
/route rebuild - 按启用路由重建当前账号的频道和群组
/route rebuild --all - 同时包含已停用路由
/route rebuild status - 查看当前重建进度
/route rebuild cancel - 取消当前重建
source/destination 可使用数字 ID、@username、公开链接或私有邀请链接; 话题在引用末尾加 /话题ID
选项: [forward|copy] [--poll <间隔>]
默认自动加入源频道并实时接收; --poll 5m 表示不自动加入, 每 5 分钟拉取一次。
私有邀请链接会先加入对应聊天; 其他方式配置的目标群必须已加入。
轮询从配置后的新消息开始且不跟踪编辑/删除。
默认使用 forward; 目标为论坛且目标引用未指定话题时, 自动创建与来源同名的话题。"""

_POLL_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhd]?)$")
_NUMERIC_TOPIC = re.compile(r"^(?P<chat>-?[1-9][0-9]*)/(?P<topic>[1-9][0-9]*)$")
_USERNAME_TOPIC = re.compile(r"^(?P<chat>@[^/\s]+)/(?P<topic>[1-9][0-9]*)$")
_TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}


@dataclass(frozen=True, slots=True)
class _EndpointReference:
    chat: str
    topic_id: int | None = None


class ForwarderCommands:
    def __init__(
        self,
        service: ForwarderManagementService,
        recovery: MembershipRecoveryService | None = None,
    ) -> None:
        self._service = service
        self._recovery = recovery

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
                draft, identities = await _parse_route_draft(arguments[1:], self._service)
                route = await self._service.add_generated_route(draft)
                await self._service.remember_chat_accesses(identities)
                return CommandResult(f"Route {route.id} is configured.")
            if arguments[0] == "set" and len(arguments) >= 4:
                route_id = int(arguments[1])
                draft, identities = await _parse_route_draft(arguments[2:], self._service)
                route = draft.bind(route_id)
                await self._service.replace_route(route)
                await self._service.remember_chat_accesses(identities)
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
            if arguments == ["check"] and self._recovery is not None:
                return CommandResult(_membership_report(await self._recovery.check()))
            if arguments and arguments[0] == "rebuild" and self._recovery is not None:
                if arguments == ["rebuild", "status"]:
                    return CommandResult(_rebuild_progress(self._recovery.progress()))
                if arguments == ["rebuild", "cancel"]:
                    cancelled = self._recovery.cancel()
                    return CommandResult("当前重建已取消。" if cancelled else "当前没有重建任务。")
                if arguments in (["rebuild"], ["rebuild", "--all"]):
                    report = await self._recovery.rebuild(
                        include_disabled=arguments == ["rebuild", "--all"]
                    )
                    return CommandResult(_rebuild_started(report))
        except (ForwarderError, ValueError, RouteNotFoundError) as error:
            return CommandResult(error.args[0] if error.args else str(error))
        return CommandResult(ROUTE_HELP)


async def _parse_route_draft(
    arguments: list[str],
    service: ForwarderManagementService,
) -> tuple[RouteDraft, tuple[ChatIdentity, ChatIdentity]]:
    positional, poll_interval = _extract_poll_option(arguments)
    if not 2 <= len(positional) <= 3:
        raise ValueError("路由参数数量不正确")
    mode = ForwardMode(positional[2]) if len(positional) >= 3 else ForwardMode.FORWARD
    source_reference = _endpoint_reference(positional[0])
    destination_reference = _endpoint_reference(positional[1])
    if poll_interval is not None and _is_private_invite(source_reference.chat):
        raise ValueError("轮询源不能使用私有邀请链接, 请改用实时模式")
    source = await service.resolve_chat(source_reference.chat)
    destination = await service.resolve_chat(destination_reference.chat)
    return (
        RouteDraft(
            SourceEndpoint(
                source.chat_id,
                source_reference.topic_id,
                username=source.username,
                poll_interval_seconds=poll_interval,
            ),
            DestinationEndpoint(
                destination.chat_id,
                destination_reference.topic_id,
                username=destination.username,
            ),
            mode=mode,
        ),
        (source, destination),
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


def _is_private_invite(reference: str) -> bool:
    normalized = reference.strip().casefold()
    return (
        "t.me/+" in normalized
        or "telegram.me/+" in normalized
        or "/joinchat/" in normalized
        or normalized.startswith("tg://join?")
    )


def _endpoint_reference(value: str) -> _EndpointReference:
    reference = value.strip()
    for pattern in (_NUMERIC_TOPIC, _USERNAME_TOPIC):
        if match := pattern.fullmatch(reference):
            return _EndpointReference(match.group("chat"), int(match.group("topic")))

    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() not in _TELEGRAM_HOSTS:
        return _EndpointReference(reference)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if parts and parts[0] == "c":
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError("Telegram 私有群话题链接格式不正确")
        return _EndpointReference(f"-100{parts[1]}", int(parts[2]))
    if (
        len(parts) == 2
        and not parts[0].startswith("+")
        and parts[0] != "joinchat"
        and parts[1].isdigit()
    ):
        return _EndpointReference(f"@{parts[0]}", int(parts[1]))
    return _EndpointReference(reference)


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
    return chat if topic_id is None else f"{chat}/{topic_id}"


def _format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _membership_report(report: MembershipReport) -> str:
    lines = [
        "频道检查完成: "
        f"已加入 {report.count(MembershipState.JOINED)}, "
        f"未加入 {report.count(MembershipState.MISSING)}, "
        f"无法自动重建 {report.count(MembershipState.UNAVAILABLE)}, "
        f"无需加入 {report.count(MembershipState.NOT_REQUIRED)}, "
        f"资料更新 {report.updated}。"
    ]
    lines.extend(_membership_line(item) for item in report.items)
    return _bounded_output(lines)


def _rebuild_started(report: MembershipReport) -> str:
    pending = tuple(item for item in report.items if item.state is MembershipState.MISSING)
    unavailable = tuple(item for item in report.items if item.state is MembershipState.UNAVAILABLE)
    lines = [
        f"重建任务已启动, 共 {len(pending)} 个待加入频道; 每次尝试间隔不低于 5 分钟并带随机波动。"
        if pending
        else "没有可自动重建的未加入频道。"
    ]
    if unavailable:
        lines.append("以下频道缺少用户名或可用邀请链接, 请人工处理:")
        lines.extend(_membership_line(item, include_state=False) for item in unavailable)
    lines.append("使用 /route rebuild status 查看进度。")
    return _bounded_output(lines)


def _membership_line(item: MembershipItem, *, include_state: bool = True) -> str:
    labels = {
        MembershipState.JOINED: "已加入",
        MembershipState.MISSING: "未加入",
        MembershipState.UNAVAILABLE: "无法自动重建",
        MembershipState.NOT_REQUIRED: "无需加入",
    }
    access = item.access
    title = access.title or access.username or str(access.chat_id)
    link = access.join_reference or "无公开链接"
    routes = ",".join(str(route_id) for route_id in item.route_ids)
    roles = ",".join(item.roles)
    prefix = f"[{labels[item.state]}] " if include_state else "- "
    suffix = f"; 元数据读取失败={item.metadata_error}" if item.metadata_error else ""
    return (
        f"{prefix}{title} | id={access.chat_id} | link={link} | "
        f"roles={roles} | routes={routes}{suffix}"
    )


def _rebuild_progress(progress: RebuildProgress) -> str:
    state = (
        "运行中"
        if progress.active
        else "已完成"
        if progress.total > 0 and progress.completed == progress.total
        else "未运行"
    )
    lines = [
        f"重建状态: {state}; 总数 {progress.total}; 完成 {progress.completed}; "
        f"新加入 {progress.joined}; 已加入 {progress.already_joined}; "
        f"等待审批 {progress.approval_pending}; 失败 {progress.failed}。"
    ]
    if progress.current_chat_id is not None:
        lines.append(f"当前频道: {progress.current_chat_id}")
    if progress.next_attempt_at is not None:
        lines.append(f"下次尝试时间戳: {progress.next_attempt_at:.0f}")
    lines.extend(f"失败 {chat_id}: {error}" for chat_id, error in progress.failures)
    return _bounded_output(lines)


def _bounded_output(lines: list[str], *, limit: int = 3900) -> str:
    output: list[str] = []
    length = 0
    for line in lines:
        added = len(line) + (1 if output else 0)
        if length + added > limit:
            output.append("其余结果因消息长度限制省略。")
            break
        output.append(line)
        length += added
    return "\n".join(output)
