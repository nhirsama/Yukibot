from datetime import UTC, datetime

import pytest

from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    IncomingMessage,
    MessageFilter,
    MessageRef,
    Route,
    RouteCycleError,
    ServiceKind,
    ServiceMessage,
    SourceEndpoint,
    assert_acyclic_routes,
)


def message(
    *,
    content_type: ContentType = ContentType.TEXT,
    text: str | None = "Hello Telegram",
    topic_id: int | None = None,
    service: ServiceMessage | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        ref=MessageRef(-1001, 10),
        content_type=content_type,
        occurred_at=datetime.now(UTC),
        text=text,
        topic_id=topic_id,
        service=service,
    )


@pytest.mark.parametrize("incoming_topic", [None, 0, 1])
def test_general_topic_representations_match(incoming_topic: int | None) -> None:
    assert SourceEndpoint(-1001, topic_id=1).matches(-1001, incoming_topic)


def test_source_without_topic_matches_every_topic() -> None:
    source = SourceEndpoint(-1001)
    assert source.matches(-1001, None)
    assert source.matches(-1001, 99)
    assert not source.matches(-1002, 99)


def test_filter_is_case_insensitive_and_blacklist_wins() -> None:
    rule = MessageFilter(
        keywords=("telegram",),
        allowed_content_types=frozenset({ContentType.TEXT, ContentType.STICKER}),
        blocked_content_types=frozenset({ContentType.STICKER}),
    )

    assert rule.allows(message())
    assert not rule.allows(message(content_type=ContentType.STICKER, text=None))


def test_service_messages_are_opt_in() -> None:
    event = message(
        content_type=ContentType.SERVICE,
        text=None,
        service=ServiceMessage(ServiceKind.MEMBERS_JOINED, member_names=("Ling",)),
    )
    assert not MessageFilter().allows(event)
    assert MessageFilter(include_service_messages=True).allows(event)


def test_service_type_requires_service_details() -> None:
    with pytest.raises(ValueError, match="service details"):
        message(content_type=ContentType.SERVICE, text=None)


def test_route_graph_allows_chains_but_rejects_cycles() -> None:
    first = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-1002))
    second = Route(2, SourceEndpoint(-1002), DestinationEndpoint(-1003))
    assert_acyclic_routes((first, second))

    cycle = Route(3, SourceEndpoint(-1003), DestinationEndpoint(-1001))
    with pytest.raises(RouteCycleError, match="cycle detected"):
        assert_acyclic_routes((first, second, cycle))
