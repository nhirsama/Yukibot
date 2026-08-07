"""Contract tests for the forwarder-owned Telethon gateway."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.contract.adapters.telegram.conftest import (
    FakeDialog,
    FakeMessage,
    FakeNativeClient,
    FakePeer,
    FakeRaw,
    MessageMediaEmpty,
)
from yukibot.adapters.telegram import PeerRegistry
from yukibot.features.forwarder import (
    ChatIdentity,
    ContentType,
    DestinationEndpoint,
    ForwardMode,
    IncomingMessage,
    MessageRef,
    NativeForwardUnsupported,
    PermanentDeliveryError,
    RetryAfter,
    SourceEndpoint,
    TelegramGateway,
)
from yukibot.features.forwarder.infrastructure import TelethonGateway
from yukibot.features.forwarder.recovery import ChatAccess, RebuildJoinResult


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


async def test_native_forward_supports_topic_and_rejects_reply_or_protected_content() -> None:
    gateway, client, _ = make_gateway()
    client.messages[(-1001, 10)] = FakeMessage(10, FakePeer(-1001))

    result = await gateway.deliver_message(
        domain_message(10),
        DestinationEndpoint(-2001, topic_id=11),
        mode=ForwardMode.FORWARD,
        reply_to_message_id=None,
    )
    assert result == MessageRef(-2001, 100)
    assert client.calls == [("forward", -2001, (10,), -1001, 11)]

    with pytest.raises(NativeForwardUnsupported, match="reply target"):
        await gateway.deliver_message(
            domain_message(10),
            DestinationEndpoint(-2001, topic_id=11),
            mode=ForwardMode.FORWARD,
            reply_to_message_id=12,
        )

    client.messages[(-1001, 10)].can_forward = False
    with pytest.raises(NativeForwardUnsupported, match="protection"):
        await gateway.deliver_message(
            domain_message(10),
            DestinationEndpoint(-2001),
            mode=ForwardMode.FORWARD,
            reply_to_message_id=None,
        )


async def test_gateway_exposes_forum_metadata_and_topic_operations() -> None:
    gateway, client, peers = make_gateway()
    peers.remember(FakePeer(-1001, "Source channel"))
    peers.remember(FakePeer(-2001, "Target forum", forum=True))

    assert gateway.chat_title(-1001) == "Source channel"
    assert gateway.is_forum(-2001)
    topic_id = await gateway.create_forum_topic(-2001, "Source channel", random_id=77)
    await gateway.edit_forum_topic(-2001, topic_id, title="Renamed channel")

    assert topic_id == 100
    assert client.calls == [
        ("topic-create", -2001, "Source channel", 77),
        ("topic-edit", -2001, 100, "Renamed channel"),
    ]


async def test_source_title_includes_forum_topic_name_only_for_specific_topic() -> None:
    gateway, client, peers = make_gateway()
    source = FakePeer(-1001, "Source group", forum=True)
    peers.remember(source)
    client.forum_topic_titles[(-1001, 42)] = "Announcements"

    assert await gateway.source_title(SourceEndpoint(-1001)) == "Source group"
    assert await gateway.source_title(SourceEndpoint(-1001, topic_id=42)) == (
        "Source group/Announcements"
    )
    assert client.calls == [("topic-title", -1001, 42)]


def test_missing_source_peer_has_no_invented_topic_title() -> None:
    gateway, _, _ = make_gateway()

    assert gateway.chat_title(-3001) is None


async def test_gateway_resolves_username_joins_and_polls_public_source() -> None:
    gateway, client, _ = make_gateway()
    source_peer = FakePeer(-3001, "Public source", username="public_source", left=True)
    client.resolved["@public_source"] = source_peer
    client.messages[(-3001, 11)] = FakeMessage(11, source_peer, text="first")
    client.messages[(-3001, 12)] = FakeMessage(12, source_peer, text="second")

    identity = await gateway.resolve_chat("@public_source")
    source = SourceEndpoint(
        identity.chat_id,
        username=identity.username,
        poll_interval_seconds=300,
    )
    await gateway.ensure_source(source, join=True)
    latest = await gateway.latest_message_id(source)
    messages = await gateway.fetch_messages_after(source, 10, limit=100)

    assert identity == ChatIdentity(-3001, "public_source")
    assert not source_peer.left
    assert latest == 12
    assert [message.ref.message_id for message in messages] == [11, 12]
    assert client.calls == [
        ("resolve", "@public_source"),
        ("join", -3001),
        ("latest", -3001),
        ("history", -3001, 10, 100),
    ]


async def test_gateway_resolves_public_link_and_existing_private_invite() -> None:
    gateway, client, peers = make_gateway()
    public = FakePeer(-3001, "Public source", username="public_source")
    private = FakePeer(-3002, "Private source")
    client.resolved["@public_source"] = public
    client.invite_checks["private_hash"] = private

    public_identity = await gateway.resolve_chat("https://t.me/public_source")
    private_identity = await gateway.resolve_chat("https://t.me/+private_hash")

    assert public_identity == ChatIdentity(-3001, "public_source")
    assert private_identity == ChatIdentity(
        -3002,
        invite_link="https://t.me/+private_hash",
    )
    assert peers.get(-3002) is private
    assert client.calls == [
        ("resolve", "@public_source"),
        ("check-invite", "private_hash"),
    ]


@pytest.mark.parametrize(
    "reference",
    (
        "https://t.me/+private_hash",
        "https://telegram.me/joinchat/private_hash",
        "tg://join?invite=private_hash",
    ),
)
async def test_gateway_joins_private_invite_when_not_already_a_member(reference: str) -> None:
    gateway, client, peers = make_gateway()
    joined = FakePeer(-3002, "Private source")
    client.invite_joins["private_hash"] = (joined,)

    identity = await gateway.resolve_chat(reference)

    assert identity == ChatIdentity(-3002, invite_link=reference)
    assert peers.get(-3002) is joined
    assert client.calls == [
        ("check-invite", "private_hash"),
        ("join-invite", "private_hash"),
    ]


async def test_gateway_rejects_private_invite_without_exactly_one_joined_chat() -> None:
    gateway, client, _ = make_gateway()
    client.invite_joins["empty_hash"] = ()
    client.invite_joins["multiple_hash"] = (FakePeer(-3001), FakePeer(-3002))

    with pytest.raises(ValueError, match="exactly one chat"):
        await gateway.resolve_chat("https://t.me/+empty_hash")
    with pytest.raises(ValueError, match="exactly one chat"):
        await gateway.resolve_chat("https://t.me/+multiple_hash")


async def test_gateway_reports_pending_private_invite_approval() -> None:
    gateway, client, _ = make_gateway()
    approval_error = type("InviteRequestSentError", (Exception,), {})()
    client.invite_join_errors["approval_hash"] = approval_error

    with pytest.raises(PermanentDeliveryError, match="审批通过后请重新执行"):
        await gateway.resolve_chat("https://t.me/+approval_hash")


async def test_gateway_checks_metadata_and_rebuilds_public_and_private_chats() -> None:
    gateway, client, peers = make_gateway()
    public = FakePeer(-3001, "Public source", username="public_source")
    private = FakePeer(-3002, "Private source")
    client.dialogs.extend((FakeDialog(public), FakeDialog(private)))
    client.invite_links[-3002] = "https://t.me/+private_hash"

    inspected = await gateway.inspect_chats((-3002, -3001, -3999))

    by_id = {item.access.chat_id: item for item in inspected}
    assert by_id[-3001].joined
    assert by_id[-3001].access.join_reference == "https://t.me/public_source"
    assert by_id[-3002].access.title == "Private source"
    assert by_id[-3002].access.invite_link == "https://t.me/+private_hash"
    assert not by_id[-3999].joined
    assert client.calls == [("invite-link", -3002)]

    client.dialogs.clear()
    public.left = True
    client.resolved["@public_source"] = public
    private_joined = FakePeer(-3002, "Private source")
    client.invite_joins["private_hash"] = (private_joined,)
    assert await gateway.join_chat(ChatAccess(-3001, username="public_source")) is (
        RebuildJoinResult.JOINED
    )
    assert (
        await gateway.join_chat(ChatAccess(-3002, invite_link="https://t.me/+private_hash"))
        is RebuildJoinResult.JOINED
    )
    assert peers.get(-3001) is public
    assert peers.get(-3002) is private_joined
    assert client.calls[-3:] == [
        ("resolve", "@public_source"),
        ("join", -3001),
        ("join-invite", "private_hash"),
    ]


async def test_copy_album_downloads_and_sends_as_one_album() -> None:
    gateway, client, _ = make_gateway()
    photo_file = object()
    video_file = object()
    client.messages[(-1001, 10)] = FakeMessage(
        10,
        FakePeer(-1001),
        text="photo caption",
        text_html="<strong>photo caption</strong>",
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


async def test_copy_single_media_downloads_before_uploading() -> None:
    gateway, client, _ = make_gateway()
    photo_file = object()
    client.messages[(-1001, 10)] = FakeMessage(
        10,
        FakePeer(-1001),
        text="photo caption",
        text_html="<strong>photo caption</strong>",
        file=photo_file,
        photo=photo_file,
    )

    await gateway.deliver_message(
        domain_message(10, content_type=ContentType.PHOTO),
        DestinationEndpoint(-2001, topic_id=9),
        mode=ForwardMode.COPY,
        reply_to_message_id=None,
    )

    assert client.calls[0] == ("download", photo_file)
    assert client.calls[1][0:2] == ("photo", -2001)
    assert client.calls[1][2].getvalue() == b"media-bytes"
    assert client.calls[1][3:] == ("<strong>photo caption</strong>", 9)


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
