from __future__ import annotations

import pytest
from conftest import FakeTelegramGateway

from yukibot.features.forwarder import (
    ChatAccess,
    ChatIdentity,
    DestinationEndpoint,
    ForwardMode,
    InMemoryChatAccessRepository,
    InMemoryManagedTopicRepository,
    InMemoryPollCursorRepository,
    ManagedTopicService,
    PermanentDeliveryError,
    PollCursor,
    Route,
    RouteDraft,
    SourceEndpoint,
)
from yukibot.features.forwarder.commands import (
    ROUTE_HELP,
    ForwarderCommands,
    _endpoint_reference,
    _EndpointReference,
)
from yukibot.features.forwarder.management import ForwarderManagementService
from yukibot.kernel import ControlCommand


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

    async def add_auto(self, draft: RouteDraft) -> Route:
        route = draft.bind(max(self.routes, default=0) + 1)
        await self.add(route)
        return route

    async def replace(self, route: Route) -> None:
        if route.id not in self.routes:
            raise KeyError(route.id)
        self.replacements += 1
        self.routes[route.id] = route

    async def remove(self, route_id: int) -> bool:
        return self.routes.pop(route_id, None) is not None


class FakeSources:
    def __init__(self) -> None:
        self.identities = {
            "@source": ChatIdentity(-1001, "source"),
            "@target": ChatIdentity(-2001, "target"),
        }
        self.prepared: list[tuple[SourceEndpoint, bool]] = []
        self.resolutions: list[str] = []
        self.latest = 42
        self.topic_titles: dict[int, str] = {}

    def chat_title(self, chat_id: int) -> str:
        return {-1001: "Source channel", -2001: "Target group"}.get(chat_id, str(chat_id))

    async def source_title(self, source: SourceEndpoint) -> str | None:
        title = self.chat_title(source.chat_id)
        if source.topic_id is None:
            return title
        topic_title = self.topic_titles.get(source.topic_id)
        return f"{title}/{topic_title}" if topic_title is not None else title

    async def resolve_chat(self, reference: str) -> ChatIdentity:
        self.resolutions.append(reference)
        return self.identities[reference]

    async def ensure_source(self, source: SourceEndpoint, *, join: bool) -> None:
        self.prepared.append((source, join))

    async def latest_message_id(self, source: SourceEndpoint) -> int:
        return self.latest

    async def fetch_messages_after(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("management must not fetch channel history")


class FailingSources(FakeSources):
    async def ensure_source(self, source: SourceEndpoint, *, join: bool) -> None:
        raise PermanentDeliveryError("无法加入源频道")


def test_help_response_cannot_be_recognized_as_an_outgoing_command() -> None:
    assert not ROUTE_HELP.startswith("/")


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("-1003953295839/546", _EndpointReference("-1003953295839", 546)),
        ("@source/546", _EndpointReference("@source", 546)),
        (
            "https://t.me/c/3953295839/546",
            _EndpointReference("-1003953295839", 546),
        ),
        (
            "https://t.me/public_group/546",
            _EndpointReference("@public_group", 546),
        ),
        ("@source", _EndpointReference("@source")),
    ),
)
def test_endpoint_reference_uses_topic_suffix(
    value: str,
    expected: _EndpointReference,
) -> None:
    assert _endpoint_reference(value) == expected


async def test_route_command_defaults_to_forward_with_copy_fallback() -> None:
    routes = MemoryRoutes()
    commands = ForwarderCommands(ForwarderManagementService(routes))

    result = await commands.handle(ControlCommand("/route", "add -1001 -2001", -9, 1, 42, True))

    assert result.text == "Route 1 is configured."
    assert routes.routes[1].mode is ForwardMode.FORWARD
    assert routes.routes[1].fallback_to_copy


async def test_username_route_auto_joins_source_and_persists_canonical_ids() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    service = ForwarderManagementService(routes, sources=sources)
    commands = ForwarderCommands(service)

    result = await commands.handle(ControlCommand("/route", "add @source @target", -9, 1, 42, True))

    assert result.text == "Route 1 is configured."
    route = routes.routes[1]
    assert route.source == SourceEndpoint(-1001, username="source")
    assert route.destination == DestinationEndpoint(-2001, username="target")
    assert sources.prepared == [(route.source, True)]


async def test_route_command_reads_topics_from_endpoint_suffixes() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    commands = ForwarderCommands(ForwarderManagementService(routes, sources=sources))

    result = await commands.handle(
        ControlCommand("/route", "add @source/7 @target/9 forward", -9, 1, 42, True)
    )

    assert result.text == "Route 1 is configured."
    route = routes.routes[1]
    assert route.source == SourceEndpoint(-1001, 7, username="source")
    assert route.destination == DestinationEndpoint(-2001, 9, username="target")
    assert sources.resolutions == ["@source", "@target"]
    listed = await commands.handle(ControlCommand("/route", "list", -9, 2, 42, True))
    assert listed.text == (
        "1: Source channel (@source)/7 -> Target group (@target)/9 (forward, enabled)"
    )


async def test_invite_link_route_persists_stable_ids_and_join_references() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    source_link = "https://t.me/+source_hash"
    target_link = "https://t.me/joinchat/target_hash"
    sources.identities.update(
        {
            source_link: ChatIdentity(-1001, invite_link=source_link),
            target_link: ChatIdentity(-2001, invite_link=target_link),
        }
    )
    accesses = InMemoryChatAccessRepository()
    service = ForwarderManagementService(routes, sources=sources, chat_accesses=accesses)
    commands = ForwarderCommands(service)

    result = await commands.handle(
        ControlCommand("/route", f"add {source_link} {target_link}", -9, 1, 42, True)
    )

    assert result.text == "Route 1 is configured."
    assert routes.routes[1].source == SourceEndpoint(-1001)
    assert routes.routes[1].destination == DestinationEndpoint(-2001)
    assert await accesses.get_many((-1001, -2001)) == (
        ChatAccess(-2001, "Target group", invite_link=target_link),
        ChatAccess(-1001, "Source channel", invite_link=source_link),
    )


async def test_poll_mode_rejects_private_source_invite_before_resolving_chats() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    commands = ForwarderCommands(ForwarderManagementService(routes, sources=sources))

    result = await commands.handle(
        ControlCommand(
            "/route",
            "add https://t.me/+source_hash @target --poll 5m",
            -9,
            1,
            42,
            True,
        )
    )

    assert result.text == "轮询源不能使用私有邀请链接, 请改用实时模式"
    assert sources.resolutions == []
    assert routes.routes == {}


async def test_poll_option_does_not_join_and_initializes_cursor_at_latest_message() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    cursors = InMemoryPollCursorRepository()
    service = ForwarderManagementService(
        routes,
        sources=sources,
        poll_cursors=cursors,
    )
    commands = ForwarderCommands(service)

    await commands.handle(
        ControlCommand(
            "/route",
            "add @source @target forward --poll 5m",
            -9,
            1,
            42,
            True,
        )
    )

    route = routes.routes[1]
    assert route.source.poll_interval_seconds == 300
    assert sources.prepared == [(route.source, False)]
    assert await cursors.get(-1001) == PollCursor(-1001, 42)

    listed = await commands.handle(ControlCommand("/route", "list", -9, 2, 42, True))
    assert listed.text == (
        "1: Source channel (@source) -> Target group (@target) (forward, enabled, poll=5m)"
    )


async def test_source_access_error_is_returned_as_command_response() -> None:
    routes = MemoryRoutes()
    commands = ForwarderCommands(ForwarderManagementService(routes, sources=FailingSources()))

    result = await commands.handle(ControlCommand("/route", "add @source @target", -9, 1, 42, True))

    assert result.text == "无法加入源频道"
    assert routes.routes == {}


async def test_generated_route_id_is_monotonic_and_duplicate_configuration_is_idempotent() -> None:
    routes = MemoryRoutes()
    routes.routes[7] = Route(7, SourceEndpoint(-7001), DestinationEndpoint(-7002))
    service = ForwarderManagementService(routes)
    draft = RouteDraft(SourceEndpoint(-1001), DestinationEndpoint(-2001))

    first = await service.add_generated_route(draft)
    repeated = await service.add_generated_route(draft)

    assert first.id == 8
    assert repeated == first
    assert sorted(routes.routes) == [7, 8]


async def test_set_keeps_generated_id_and_replaces_configuration() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    commands = ForwarderCommands(ForwarderManagementService(routes, sources=sources))
    await commands.handle(ControlCommand("/route", "add @source @target", -9, 1, 42, True))

    result = await commands.handle(
        ControlCommand("/route", "set 1 @source @target copy", -9, 2, 42, True)
    )

    assert result.text == "Route 1 is updated."
    assert routes.routes[1].mode is ForwardMode.COPY


async def test_set_updates_saved_invite_links() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    source_link = "https://t.me/+source_hash"
    target_link = "https://t.me/+target_hash"
    sources.identities.update(
        {
            source_link: ChatIdentity(-1001, invite_link=source_link),
            target_link: ChatIdentity(-2001, invite_link=target_link),
        }
    )
    accesses = InMemoryChatAccessRepository()
    commands = ForwarderCommands(
        ForwarderManagementService(routes, sources=sources, chat_accesses=accesses)
    )
    await commands.handle(ControlCommand("/route", "add @source @target", -9, 1, 42, True))

    result = await commands.handle(
        ControlCommand("/route", f"set 1 {source_link} {target_link}", -9, 2, 42, True)
    )

    assert result.text == "Route 1 is updated."
    assert await accesses.get_many((-1001, -2001)) == (
        ChatAccess(-2001, "Target group", invite_link=target_link),
        ChatAccess(-1001, "Source channel", invite_link=source_link),
    )


async def test_adding_route_prepares_automatic_forum_topic_immediately() -> None:
    routes = MemoryRoutes()
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    telegram.chat_titles[-1001] = "Source channel"
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)
    service = ForwarderManagementService(routes, topics)

    await service.add_route(Route(7, SourceEndpoint(-1001), DestinationEndpoint(-2001)))

    assert telegram.created_topics[0][0:2] == (-2001, "Source channel")


async def test_topic_route_names_automatic_forum_topic_with_group_and_topic() -> None:
    routes = MemoryRoutes()
    sources = FakeSources()
    sources.topic_titles[7] = "Announcements"
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)
    service = ForwarderManagementService(routes, topics, sources=sources)

    await service.add_route(Route(7, SourceEndpoint(-1001, topic_id=7), DestinationEndpoint(-2001)))

    assert telegram.created_topics[0][0:2] == (-2001, "Source channel/Announcements")


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
