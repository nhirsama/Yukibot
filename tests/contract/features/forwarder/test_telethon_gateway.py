"""Contract tests for the forwarder-owned Telethon gateway."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.contract.adapters.telegram.conftest import (
    FakeMessage,
    FakeNativeClient,
    FakePeer,
    FakeRaw,
    MessageMediaEmpty,
)
from yukibot.adapters.telegram import PeerRegistry
from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    ForwardMode,
    IncomingMessage,
    MessageRef,
    NativeForwardUnsupported,
    RetryAfter,
    TelegramGateway,
)
from yukibot.features.forwarder.infrastructure import TelethonGateway


def domain_message(
    message_id: int, *, content_type: ContentType = ContentType.TEXT
) -> IncomingMessage:
    return IncomingMessage(
        MessageRef(-1001, message_id),
        content_type,
        datetime.now(UTC),
        grouped_id=50 if content_type is not ContentType.TEXT else None,
        text="hello" if content_type is ContentType.TEXT else None,
        caption="caption" if content_type is not ContentType.TEXT else None,
    )


def make_gateway() -> tuple[TelethonGateway, FakeNativeClient, PeerRegistry]:
    client = FakeNativeClient()
    peers = PeerRegistry()
    peers.remember(FakePeer(-1001))
    peers.remember(FakePeer(-2001))
    return TelethonGateway(client, peers), client, peers  # type: ignore[arg-type]


def accepts_forwarder_port(gateway: TelegramGateway) -> None:
    assert gateway is not None


async def test_gateway_structurally_satisfies_port_and_copies_formatted_text() -> None:
    gateway, client, _ = make_gateway()
    accepts_forwarder_port(gateway)
    client.messages[(-1001, 10)] = FakeMessage(10, FakePeer(-1001))

    result = await gateway.deliver_message(
        domain_message(10),
        DestinationEndpoint(-2001, topic_id=11),
        mode=ForwardMode.COPY,
        reply_to_message_id=None,
    )

    assert result == MessageRef(-2001, 100)
    assert client.calls == [("message", -2001, None, "<strong>hello</strong>", 11)]


async def test_native_forward_rejects_topic_and_protected_content() -> None:
    gateway, client, _ = make_gateway()
    client.messages[(-1001, 10)] = FakeMessage(10, FakePeer(-1001))

    with pytest.raises(NativeForwardUnsupported, match="topic/reply"):
        await gateway.deliver_message(
            domain_message(10),
            DestinationEndpoint(-2001, topic_id=11),
            mode=ForwardMode.FORWARD,
            reply_to_message_id=None,
        )

    client.messages[(-1001, 10)].can_forward = False
    with pytest.raises(NativeForwardUnsupported, match="protection"):
        await gateway.deliver_message(
            domain_message(10),
            DestinationEndpoint(-2001),
            mode=ForwardMode.FORWARD,
            reply_to_message_id=None,
        )


async def test_copy_album_downloads_and_sends_as_one_album() -> None:
    gateway, client, _ = make_gateway()
    photo_file = object()
    video_file = object()
    client.messages[(-1001, 10)] = FakeMessage(
        10,
        FakePeer(-1001),
        text="photo caption",
        file=photo_file,
        photo=photo_file,
        grouped_id=50,
        _raw=FakeRaw(media=MessageMediaEmpty()),
    )
    client.messages[(-1001, 11)] = FakeMessage(
        11,
        FakePeer(-1001),
        text="video caption",
        file=video_file,
        video=video_file,
        grouped_id=50,
        _raw=FakeRaw(media=MessageMediaEmpty()),
    )

    results = await gateway.deliver_album(
        (
            domain_message(10, content_type=ContentType.PHOTO),
            domain_message(11, content_type=ContentType.VIDEO),
        ),
        DestinationEndpoint(-2001, topic_id=9),
        mode=ForwardMode.COPY,
        reply_to_message_id=None,
    )

    assert results == (MessageRef(-2001, 100), MessageRef(-2001, 101))
    assert client.album is not None
    assert [item[0] for item in client.album.items] == ["photo", "video"]
    assert all(item[1] == b"media-bytes" for item in client.album.items)
    assert client.album.reply_to == 9


async def test_flood_wait_is_translated_to_retry_after() -> None:
    class RpcFailure(Exception):
        name = "FLOOD_WAIT"
        value = 12

    gateway, client, _ = make_gateway()
    client.error = RpcFailure("wait")

    with pytest.raises(RetryAfter) as caught:
        await gateway.send_text("hello", DestinationEndpoint(-2001), reply_to_message_id=None)

    assert caught.value.seconds == 12


async def test_edit_refetches_source_and_delete_targets_destination() -> None:
    gateway, client, _ = make_gateway()
    client.messages[(-1001, 10)] = FakeMessage(10, FakePeer(-1001))

    await gateway.edit_from_source(domain_message(10), MessageRef(-2001, 100))
    await gateway.delete_message(MessageRef(-2001, 100))

    assert client.calls[0] == ("edit", -2001, 100, None, "<strong>hello</strong>")
    assert client.calls[1] == ("delete", -2001, (100,), True)
