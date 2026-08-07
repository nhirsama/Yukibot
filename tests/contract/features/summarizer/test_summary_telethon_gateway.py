from datetime import UTC, datetime, timedelta

import pytest

from tests.contract.adapters.telegram.conftest import (
    FakeMessage,
    FakeNativeClient,
    FakePeer,
)
from yukibot.adapters.telegram import PeerRegistry
from yukibot.features.summarizer.infrastructure import TelethonSummaryGateway
from yukibot.features.summarizer.models import SummaryChatKind, SummaryEndpoint
from yukibot.features.summarizer.ports import SummaryTelegram


def accepts_summary_port(gateway: SummaryTelegram) -> None:
    assert gateway is not None


@pytest.mark.parametrize(
    "reference",
    (
        "-1001234567890/42",
        "https://t.me/c/1234567890/42",
        "https://t.me/public_group/42",
    ),
)
async def test_gateway_resolves_supported_topic_references(reference: str) -> None:
    client = FakeNativeClient()
    peers = PeerRegistry()
    peer = FakePeer(-1001234567890, "Forum", forum=True, username="public_group")
    client.resolved[-1001234567890] = peer
    client.resolved["@public_group"] = peer
    gateway = TelethonSummaryGateway(client, peers)  # type: ignore[arg-type]
    accepts_summary_port(gateway)

    endpoint = await gateway.resolve_endpoint(reference)

    assert endpoint == SummaryEndpoint(-1001234567890, 42, "public_group")


async def test_gateway_fetches_normalized_topic_history_and_sends_to_topic() -> None:
    client = FakeNativeClient()
    peers = PeerRegistry()
    source = FakePeer(-1001, "Source group", forum=True, username="source_group")
    target = FakePeer(-1002, "Target group", forum=True)
    source.megagroup = True  # type: ignore[attr-defined]
    peers.remember(source)
    peers.remember(target)
    now = datetime.now(UTC)
    client.messages[(-1001, 42)] = FakeMessage(42, source, text="Topic root", date=now)
    client.messages[(-1001, 43)] = FakeMessage(
        43,
        source,
        text="Details at https://example.com/item",
        date=now,
        sender=FakePeer(7, "Alice"),
        replied_message_id=42,
        outgoing=True,
    )
    gateway = TelethonSummaryGateway(client, peers)  # type: ignore[arg-type]
    endpoint = SummaryEndpoint(-1001, 42, "source_group")

    fetched = await gateway.fetch_recent(endpoint, since=now - timedelta(hours=1), limit=None)
    sent = await gateway.send_text(SummaryEndpoint(-1002, 99), "summary")

    assert fetched.source == endpoint
    assert fetched.chat_kind is SummaryChatKind.GROUP
    assert [item.message_ids for item in fetched.messages] == [(42,), (43,)]
    assert fetched.messages[1].sender_name == "Alice"
    assert fetched.messages[1].outgoing is True
    assert fetched.messages[1].links == ("https://example.com/item",)
    assert sent.chat_id == -1002
    assert client.calls[0][0] == "recent"
    assert client.calls[0][3] is None
    assert client.calls[0][4] == 42
    assert client.calls[-1] == ("message", -1002, "summary", None, 99)


async def test_gateway_rejects_topic_for_non_forum_chat() -> None:
    client = FakeNativeClient()
    client.resolved["@ordinary"] = FakePeer(-1001, "Ordinary", username="ordinary")
    gateway = TelethonSummaryGateway(client, PeerRegistry())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="不是论坛群组"):
        await gateway.resolve_endpoint("https://t.me/ordinary/42")
