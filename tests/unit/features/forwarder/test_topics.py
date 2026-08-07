from datetime import UTC, datetime

import pytest
from conftest import FakeTelegramGateway

from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    ForwarderService,
    ForwardMode,
    InMemoryManagedTopicRepository,
    InMemoryMessageLinkRepository,
    InMemoryRouteRepository,
    ManagedTopicService,
    MessageRef,
    PermanentDeliveryError,
    Route,
    ServiceKind,
    ServiceMessage,
    SourceEndpoint,
)
from yukibot.features.forwarder.models import IncomingMessage


def route(
    route_id: int = 1,
    *,
    topic_id: int | None = None,
    source_topic_id: int | None = None,
) -> Route:
    return Route(
        route_id,
        SourceEndpoint(-1001, source_topic_id),
        DestinationEndpoint(-2001, topic_id),
    )


async def test_automatic_topic_is_created_once_reused_by_id_and_explicitly_renamed() -> None:
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    telegram.chat_titles[-1001] = "Source channel"
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)

    first = await topics.resolve(route(1))
    second = await topics.resolve(route(2))
    telegram.chat_titles[-1001] = str(-1001)
    reused = await topics.resolve(route(2))
    renamed = await topics.resolve(route(2), source_title="Renamed channel")

    assert first == second == reused == renamed == DestinationEndpoint(-2001, 500)
    assert len(telegram.created_topics) == 1
    assert telegram.created_topics[0][0:2] == (-2001, "Source channel")
    assert telegram.edited_topics == [(-2001, 500, "Renamed channel")]


async def test_automatic_topic_is_not_created_without_a_resolved_source_title() -> None:
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    telegram.chat_titles.clear()
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)

    with pytest.raises(PermanentDeliveryError, match="has no resolved title"):
        await topics.resolve(route())

    assert telegram.created_topics == []


async def test_different_source_topics_get_distinct_automatic_topics() -> None:
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)

    first = await topics.resolve(
        route(1, source_topic_id=7),
        source_title="Source group/Announcements",
    )
    second = await topics.resolve(
        route(2, source_topic_id=8),
        source_title="Source group/Support",
    )
    repeated = await topics.resolve(
        route(3, source_topic_id=7),
        source_title="Source group/Announcements",
    )

    assert first == repeated == DestinationEndpoint(-2001, 500)
    assert second == DestinationEndpoint(-2001, 501)
    assert [item[1] for item in telegram.created_topics] == [
        "Source group/Announcements",
        "Source group/Support",
    ]


async def test_explicit_topic_and_non_forum_destination_are_not_automatically_managed() -> None:
    telegram = FakeTelegramGateway()
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)

    assert await topics.resolve(route(topic_id=12)) == DestinationEndpoint(-2001, 12)
    assert await topics.resolve(route()) == DestinationEndpoint(-2001)
    assert telegram.created_topics == []


async def test_topic_creation_reuses_stable_random_id_after_persistence_failure() -> None:
    class FailingRepository:
        async def get(  # type: ignore[no-untyped-def]
            self,
            source_chat_id: int,
            source_topic_id: int | None,
            destination_chat_id: int,
        ):
            return None

        async def save(self, topic):  # type: ignore[no-untyped-def]
            raise RuntimeError("database unavailable")

    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    telegram.chat_titles[-1001] = "Source channel"
    topics = ManagedTopicService(FailingRepository(), telegram)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="database unavailable"):
        await topics.resolve(route())
    with pytest.raises(RuntimeError, match="database unavailable"):
        await topics.resolve(route())

    assert telegram.created_topics[0][2] == telegram.created_topics[1][2]


async def test_forwarder_uses_managed_topic_and_syncs_title_change_without_forwarding_it() -> None:
    configured_route = route()
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    telegram.chat_titles[-1001] = "Source channel"
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)
    service = ForwarderService(
        InMemoryRouteRepository((configured_route,)),
        InMemoryMessageLinkRepository(),
        telegram,
        topics=topics,
    )
    message = IncomingMessage(
        MessageRef(-1001, 10),
        ContentType.TEXT,
        datetime.now(UTC),
        text="hello",
    )

    delivered = await service.forward_message(message)
    renamed = await service.forward_message(
        IncomingMessage(
            MessageRef(-1001, 11),
            ContentType.SERVICE,
            datetime.now(UTC),
            service=ServiceMessage(ServiceKind.TITLE_CHANGED, new_title="Renamed channel"),
            outgoing=True,
        )
    )

    assert configured_route.mode is ForwardMode.FORWARD
    assert delivered.outcomes[0].mode_used is ForwardMode.FORWARD
    assert telegram.calls[0].destination == DestinationEndpoint(-2001, 500)
    assert renamed.matched_routes == 0
    assert renamed.delivered_messages == 0
    assert telegram.edited_topics == [(-2001, 500, "Renamed channel")]


async def test_group_rename_does_not_drop_specific_source_topic_name() -> None:
    configured_route = route(source_topic_id=7)
    telegram = FakeTelegramGateway()
    telegram.forum_chats.add(-2001)
    topics = ManagedTopicService(InMemoryManagedTopicRepository(), telegram)
    await topics.resolve(configured_route, source_title="Source group/Announcements")
    service = ForwarderService(
        InMemoryRouteRepository((configured_route,)),
        InMemoryMessageLinkRepository(),
        telegram,
        topics=topics,
    )

    renamed = await service.forward_message(
        IncomingMessage(
            MessageRef(-1001, 11),
            ContentType.SERVICE,
            datetime.now(UTC),
            topic_id=7,
            service=ServiceMessage(ServiceKind.TITLE_CHANGED, new_title="Renamed group"),
            outgoing=True,
        )
    )

    assert renamed.delivered_messages == 0
    assert telegram.edited_topics == []
