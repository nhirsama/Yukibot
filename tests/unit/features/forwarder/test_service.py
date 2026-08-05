from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from conftest import FakeTelegramGateway

from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    ForwarderOptions,
    ForwarderService,
    ForwardMode,
    IncomingMessage,
    InMemoryMessageLinkRepository,
    InMemoryRouteRepository,
    MessageFilter,
    MessageLink,
    MessageRef,
    MessagesDeleted,
    PartialDeliveryState,
    Route,
    ServiceKind,
    ServiceMessage,
    SourceEndpoint,
)


def make_message(
    message_id: int,
    *,
    text: str = "hello",
    topic_id: int | None = None,
    reply_to: int | None = None,
    grouped_id: int | None = None,
    content_type: ContentType = ContentType.TEXT,
    service: ServiceMessage | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        ref=MessageRef(-1001, message_id),
        content_type=content_type,
        occurred_at=datetime.now(UTC),
        topic_id=topic_id,
        text=None if service else text,
        reply_to_message_id=reply_to,
        grouped_id=grouped_id,
        service=service,
    )


async def test_matches_routes_and_preserves_reply_mapping() -> None:
    routes = InMemoryRouteRepository(
        [
            Route(
                1,
                SourceEndpoint(-1001, topic_id=7),
                DestinationEndpoint(-2001, topic_id=11),
                message_filter=MessageFilter(keywords=("hello",)),
            ),
            Route(2, SourceEndpoint(-9999), DestinationEndpoint(-2002)),
        ]
    )
    links = InMemoryMessageLinkRepository()
    telegram = FakeTelegramGateway()
    service = ForwarderService(routes, links, telegram)

    first = await service.forward_message(make_message(10, topic_id=7))
    reply = await service.forward_message(make_message(11, topic_id=7, reply_to=10))

    assert first.delivered_messages == 1
    assert reply.delivered_messages == 1
    assert reply.matched_routes == 1
    assert telegram.calls[1].reply_to_message_id == first.outcomes[0].destinations[0].message_id
    assert await links.count() == 2


async def test_native_forward_can_fall_back_to_copy() -> None:
    route = Route(
        1,
        SourceEndpoint(-1001),
        DestinationEndpoint(-2001),
        mode=ForwardMode.FORWARD,
    )
    telegram = FakeTelegramGateway()
    telegram.reject_native_forward = True
    links = InMemoryMessageLinkRepository()
    service = ForwarderService(InMemoryRouteRepository([route]), links, telegram)

    report = await service.forward_message(make_message(10))

    assert [call.mode for call in telegram.calls] == [ForwardMode.FORWARD, ForwardMode.COPY]
    assert report.outcomes[0].mode_used is ForwardMode.COPY
    stored = await links.get(1, MessageRef(-1001, 10))
    assert stored is not None
    assert stored.delivery_mode is ForwardMode.COPY
    assert not report.failures


async def test_native_forward_edit_is_skipped_without_editing_destination() -> None:
    route = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))
    links = InMemoryMessageLinkRepository()
    telegram = FakeTelegramGateway()
    service = ForwarderService(InMemoryRouteRepository([route]), links, telegram)
    message = make_message(10)

    await service.forward_message(message)
    report = await service.synchronize_edit(make_message(10, text="edited"))

    assert report.synchronized == 1
    assert report.failures == ()
    assert telegram.edits == []


async def test_concurrent_duplicate_message_is_delivered_once() -> None:
    route = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))
    telegram = FakeTelegramGateway()
    service = ForwarderService(
        InMemoryRouteRepository([route]), InMemoryMessageLinkRepository(), telegram
    )
    event = make_message(10)

    first, second = await asyncio.gather(
        service.forward_message(event),
        service.forward_message(event),
    )

    assert len(telegram.calls) == 1
    assert first.delivered_messages + second.delivered_messages == 1
    assert first.deduplicated_messages + second.deduplicated_messages == 1


async def test_album_is_sorted_and_each_item_is_mapped() -> None:
    route = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))
    links = InMemoryMessageLinkRepository()
    telegram = FakeTelegramGateway()
    service = ForwarderService(InMemoryRouteRepository([route]), links, telegram)
    album = (
        make_message(12, grouped_id=50, content_type=ContentType.PHOTO),
        make_message(10, grouped_id=50, content_type=ContentType.PHOTO),
        make_message(11, grouped_id=50, content_type=ContentType.VIDEO),
    )

    report = await service.forward_album(album)

    assert [item.ref.message_id for item in telegram.calls[0].messages] == [10, 11, 12]
    assert report.delivered_messages == 3
    assert await links.count() == 3

    replay = await service.forward_album(album)
    assert len(telegram.calls) == 1
    assert replay.delivered_messages == 0
    assert replay.deduplicated_messages == 3


async def test_album_copy_fallback_mode_is_persisted_for_replay() -> None:
    route = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))
    links = InMemoryMessageLinkRepository()
    telegram = FakeTelegramGateway()
    telegram.reject_native_forward = True
    service = ForwarderService(InMemoryRouteRepository([route]), links, telegram)
    album = (
        make_message(10, grouped_id=50, content_type=ContentType.PHOTO),
        make_message(11, grouped_id=50, content_type=ContentType.PHOTO),
    )

    delivered = await service.forward_album(album)
    replay = await service.forward_album(album)

    assert delivered.outcomes[0].mode_used is ForwardMode.COPY
    assert replay.outcomes[0].mode_used is ForwardMode.COPY
    assert replay.deduplicated_messages == 2


async def test_partial_album_mapping_is_not_resent() -> None:
    route = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))
    first = MessageLink(1, MessageRef(-1001, 10), MessageRef(-2001, 100))
    links = InMemoryMessageLinkRepository([first])
    telegram = FakeTelegramGateway()
    service = ForwarderService(InMemoryRouteRepository([route]), links, telegram)
    album = (
        make_message(10, grouped_id=50, content_type=ContentType.PHOTO),
        make_message(11, grouped_id=50, content_type=ContentType.PHOTO),
    )

    report = await service.forward_album(album)

    assert not telegram.calls
    assert isinstance(report.failures[0].error, PartialDeliveryState)


async def test_service_message_is_rendered_as_plain_text() -> None:
    route = Route(
        1,
        SourceEndpoint(-1001),
        DestinationEndpoint(-2001),
        message_filter=MessageFilter(include_service_messages=True),
    )
    telegram = FakeTelegramGateway()
    service = ForwarderService(
        InMemoryRouteRepository([route]), InMemoryMessageLinkRepository(), telegram
    )
    event = make_message(
        10,
        topic_id=7,
        content_type=ContentType.SERVICE,
        service=ServiceMessage(ServiceKind.MEMBERS_JOINED, member_names=("A", "B")),
    )

    report = await service.forward_message(event)

    assert report.delivered_messages == 1
    assert telegram.sent_texts[0][0] == "A, B joined the group in topic 7."


async def test_edit_and_delete_are_synchronized_for_every_route() -> None:
    source = MessageRef(-1001, 10)
    first = MessageLink(1, source, MessageRef(-2001, 100))
    second = MessageLink(2, source, MessageRef(-2002, 200))
    links = InMemoryMessageLinkRepository([first, second])
    telegram = FakeTelegramGateway()
    service = ForwarderService(InMemoryRouteRepository(), links, telegram)
    edited = make_message(10, text="edited")

    edit_report = await service.synchronize_edit(edited)
    delete_report = await service.synchronize_delete(
        MessagesDeleted((10,), datetime.now(UTC), chat_id=-1001)
    )

    assert edit_report.synchronized == 2
    assert delete_report.synchronized == 2
    assert set(telegram.deletes) == {first.destination, second.destination}
    assert await links.count() == 0


async def test_delete_without_chat_id_is_ignored_by_default() -> None:
    link = MessageLink(1, MessageRef(-1001, 10), MessageRef(-2001, 100))
    links = InMemoryMessageLinkRepository([link])
    telegram = FakeTelegramGateway()
    service = ForwarderService(InMemoryRouteRepository(), links, telegram)

    report = await service.synchronize_delete(MessagesDeleted((10,), datetime.now(UTC)))

    assert report.ignored_reason == "source_chat_unknown"
    assert not telegram.deletes
    assert await links.count() == 1


async def test_ambiguous_delete_can_be_explicitly_enabled() -> None:
    links = InMemoryMessageLinkRepository(
        [
            MessageLink(1, MessageRef(-1001, 10), MessageRef(-2001, 100)),
            MessageLink(2, MessageRef(-1002, 10), MessageRef(-2002, 200)),
        ]
    )
    telegram = FakeTelegramGateway()
    service = ForwarderService(
        InMemoryRouteRepository(),
        links,
        telegram,
        ForwarderOptions(allow_ambiguous_deletes=True),
    )

    report = await service.synchronize_delete(MessagesDeleted((10,), datetime.now(UTC)))

    assert report.synchronized == 2
    assert await links.count() == 0
