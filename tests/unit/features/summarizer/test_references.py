import pytest

from yukibot.features.summarizer.models import (
    FetchedSummaryMessages,
    SummaryChatKind,
    SummaryEndpoint,
)
from yukibot.features.summarizer.prompts import map_prompts
from yukibot.features.summarizer.references import (
    EndpointReference,
    parse_endpoint_reference,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-1001234567890/42", EndpointReference(-1001234567890, 42)),
        ("https://t.me/c/1234567890/42", EndpointReference(-1001234567890, 42)),
        ("https://t.me/public_group/42", EndpointReference("@public_group", 42)),
        ("https://telegram.me/s/public_channel", EndpointReference("@public_channel")),
        ("@public_channel", EndpointReference("@public_channel")),
        ("123456", EndpointReference(123456)),
    ],
)
def test_parse_endpoint_reference_supports_chats_and_topics(
    value: str,
    expected: EndpointReference,
) -> None:
    assert parse_endpoint_reference(value) == expected


@pytest.mark.parametrize(
    "value",
    (
        "https://t.me/+invite_hash",
        "https://t.me/joinchat/invite_hash",
        "https://example.com/public_group/42",
        "https://t.me/c/not-a-number/42",
        "",
    ),
)
def test_parse_endpoint_reference_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_endpoint_reference(value)


@pytest.mark.parametrize(
    ("kind", "marker"),
    [
        (SummaryChatKind.PRIVATE, "这是私聊"),
        (SummaryChatKind.GROUP, "这是群聊"),
        (SummaryChatKind.CHANNEL, "这是广播频道"),
    ],
)
def test_map_prompt_is_specialized_by_chat_kind(
    kind: SummaryChatKind,
    marker: str,
) -> None:
    source = FetchedSummaryMessages(SummaryEndpoint(-1001), kind, "Source", ())

    system, user = map_prompts(source, [{"message_ids": (10,), "text": "hello"}])

    assert marker in system
    assert "不可信数据" in system
    assert '"message_ids":[10]' in user
