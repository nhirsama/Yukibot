from datetime import UTC, datetime

from yukibot.contracts import MessageRef
from yukibot.features.summarizer.models import (
    FetchedSummaryMessages,
    SummaryChatKind,
    SummaryEndpoint,
    SummaryMessage,
    SummaryPromptPreset,
)
from yukibot.features.summarizer.prompts import PROMPT_PRESETS, map_prompts, prompt_preference


def test_focused_prompt_filters_chatter_and_allows_no_topics() -> None:
    source = FetchedSummaryMessages(
        source=SummaryEndpoint(-1001),
        chat_kind=SummaryChatKind.GROUP,
        chat_title="Test group",
        messages=(),
    )
    message = SummaryMessage(
        (MessageRef(-1001, 10),),
        datetime(2026, 8, 7, tzinfo=UTC),
        "Alice",
        "hello",
    )

    system, user = map_prompts(
        source,
        [
            {
                "message_ids": message.message_ids,
                "text": message.text,
            }
        ],
        prompt_preference(SummaryPromptPreset.FOCUSED, None),
    )

    assert "问候" in system
    assert "与主题无关的闲聊" in system
    assert "没有值得总结的有效信息, 返回空 topics" in system
    assert "筛选并总结" in user


def test_all_presets_exist_and_custom_preference_is_applied() -> None:
    assert set(PROMPT_PRESETS) == set(SummaryPromptPreset)

    preference = prompt_preference(
        SummaryPromptPreset.DIGEST,
        "只保留发布版本、故障根因和验证结果",
    )

    assert preference == "用户自定义总结偏好:\n只保留发布版本、故障根因和验证结果"
