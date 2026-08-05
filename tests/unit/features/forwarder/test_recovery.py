from __future__ import annotations

from collections.abc import Sequence

from yukibot.features.forwarder.memory import (
    InMemoryChatAccessRepository,
    InMemoryRouteRepository,
)
from yukibot.features.forwarder.models import DestinationEndpoint, Route, SourceEndpoint
from yukibot.features.forwarder.recovery import (
    ChatAccess,
    ChatInspection,
    MembershipRebuilder,
    MembershipRecoveryService,
    MembershipState,
    RebuildJoinResult,
)


class RecoveryGatewayStub:
    def __init__(self, inspections: Sequence[ChatInspection]) -> None:
        self.inspections = {item.access.chat_id: item for item in inspections}
        self.inspected: list[tuple[int, ...]] = []
        self.joined: list[int] = []

    async def inspect_chats(self, chat_ids: Sequence[int]) -> Sequence[ChatInspection]:
        self.inspected.append(tuple(chat_ids))
        return tuple(
            self.inspections.get(chat_id, ChatInspection(ChatAccess(chat_id), False))
            for chat_id in chat_ids
        )

    async def join_chat(self, access: ChatAccess) -> RebuildJoinResult:
        self.joined.append(access.chat_id)
        return RebuildJoinResult.JOINED


async def test_check_refreshes_joined_metadata_and_classifies_unique_chats() -> None:
    routes = InMemoryRouteRepository(
        (
            Route(
                1,
                SourceEndpoint(-1001, username="old_source"),
                DestinationEndpoint(-2001),
            ),
            Route(
                2,
                SourceEndpoint(-3001, username="polled", poll_interval_seconds=300),
                DestinationEndpoint(-4001),
            ),
        )
    )
    accesses = InMemoryChatAccessRepository(
        (ChatAccess(-1001, "Old title", "old_source", "https://t.me/old_source"),)
    )
    gateway = RecoveryGatewayStub(
        (
            ChatInspection(
                ChatAccess(
                    -1001,
                    "Renamed source",
                    "new_source",
                    "https://t.me/new_source",
                ),
                True,
            ),
            ChatInspection(
                ChatAccess(-2001, "Private target", invite_link="https://t.me/+private"),
                True,
            ),
        )
    )
    rebuilder = MembershipRebuilder(gateway, random_interval=lambda _low, _high: 300)
    service = MembershipRecoveryService(routes, accesses, gateway, rebuilder)

    report = await service.check()

    by_id = {item.access.chat_id: item for item in report.items}
    assert by_id[-1001].state is MembershipState.JOINED
    assert by_id[-1001].access.title == "Renamed source"
    assert by_id[-1001].access.join_reference == "https://t.me/new_source"
    assert by_id[-2001].state is MembershipState.JOINED
    assert by_id[-2001].access.invite_link == "https://t.me/+private"
    assert by_id[-3001].state is MembershipState.NOT_REQUIRED
    assert by_id[-4001].state is MembershipState.UNAVAILABLE
    assert report.updated == 2
    assert gateway.inspected == [(-4001, -3001, -2001, -1001)]
    stored = {item.chat_id: item for item in await accesses.get_many((-1001, -2001))}
    assert stored[-1001].username == "new_source"
    assert stored[-2001].title == "Private target"


async def test_check_preserves_recorded_private_invite_when_no_new_link_is_visible() -> None:
    routes = InMemoryRouteRepository((Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001)),))
    original = ChatAccess(-1001, "Private", invite_link="https://t.me/+existing")
    accesses = InMemoryChatAccessRepository((original,))
    gateway = RecoveryGatewayStub(
        (
            ChatInspection(
                ChatAccess(-1001, "Private"),
                True,
                metadata_error="FloodWaitError",
            ),
            ChatInspection(ChatAccess(-2001, "Target"), True),
        )
    )
    rebuilder = MembershipRebuilder(gateway)
    service = MembershipRecoveryService(routes, accesses, gateway, rebuilder)

    await service.check()
    assert await accesses.get_many((-1001,)) == (original,)

    gateway.inspections[-1001] = ChatInspection(ChatAccess(-1001, "Private"), True)
    await service.check()
    assert await accesses.get_many((-1001,)) == (original,)


async def test_rebuild_queues_only_recoverable_missing_chats_with_five_minimum_spacing() -> None:
    routes = InMemoryRouteRepository(
        (
            Route(
                1,
                SourceEndpoint(-1001, poll_interval_seconds=300),
                DestinationEndpoint(-2001, username="target_one"),
            ),
            Route(
                2,
                SourceEndpoint(-1002, poll_interval_seconds=300),
                DestinationEndpoint(-2002, username="target_two"),
            ),
            Route(
                3,
                SourceEndpoint(-1003, poll_interval_seconds=300),
                DestinationEndpoint(-2003),
            ),
        )
    )
    gateway = RecoveryGatewayStub(())
    accesses = InMemoryChatAccessRepository()
    rebuilder = MembershipRebuilder(
        gateway,
        clock=lambda: 100.0,
        random_interval=lambda _low, _high: 300.0,
    )
    service = MembershipRecoveryService(routes, accesses, gateway, rebuilder)

    report = await service.rebuild()

    assert report.count(MembershipState.MISSING) == 2
    assert report.count(MembershipState.UNAVAILABLE) == 1
    assert rebuilder.progress.total == 2
    assert await rebuilder.process_once(100.0)
    assert gateway.joined == [-2002]
    assert rebuilder.progress.next_attempt_at == 400.0
    assert not await rebuilder.process_once(399.0)
    assert await rebuilder.process_once(400.0)
    assert gateway.joined == [-2002, -2001]
    assert not rebuilder.progress.active
    assert rebuilder.progress.joined == 2
