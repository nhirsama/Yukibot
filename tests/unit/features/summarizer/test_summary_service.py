from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from yukibot.contracts import MessageRef
from yukibot.features.summarizer.errors import SummaryModelUnavailableError
from yukibot.features.summarizer.models import (
    FetchedSummaryMessages,
    SummaryActionItem,
    SummaryChatKind,
    SummaryDocument,
    SummaryEndpoint,
    SummaryMessage,
    SummaryModelConfig,
    SummaryPromptPreset,
    SummaryRule,
    SummaryRuleDraft,
    SummaryRun,
    SummaryTopic,
)
from yukibot.features.summarizer.service import SummarizerService, _message_batches


class MemorySummaryRepository:
    def __init__(self) -> None:
        self.rules: dict[int, SummaryRule] = {}
        self.runs: list[SummaryRun] = []
        self.model_config: SummaryModelConfig | None = SummaryModelConfig("test", "structured-test")

    async def list_all(self) -> tuple[SummaryRule, ...]:
        return tuple(self.rules[key] for key in sorted(self.rules))

    async def add_auto(self, draft: SummaryRuleDraft) -> SummaryRule:
        rule = draft.bind(max(self.rules, default=0) + 1)
        self.rules[rule.id] = rule
        return rule

    async def replace(self, rule: SummaryRule) -> None:
        if rule.id not in self.rules:
            raise KeyError(rule.id)
        self.rules[rule.id] = rule

    async def remove(self, rule_id: int) -> bool:
        return self.rules.pop(rule_id, None) is not None

    async def save(self, run: SummaryRun) -> None:
        self.runs.append(run)

    async def get_model_config(self) -> SummaryModelConfig | None:
        return self.model_config

    async def save_model_config(self, config: SummaryModelConfig) -> None:
        self.model_config = config

    async def clear_model_config(self) -> bool:
        existed = self.model_config is not None
        self.model_config = None
        return existed


class FakeSummaryTelegram:
    def __init__(self) -> None:
        self.endpoints = {
            "source": SummaryEndpoint(-1001, username="source_channel"),
            "destination": SummaryEndpoint(-1002, topic_id=42),
        }
        self.fetched = FetchedSummaryMessages(
            self.endpoints["source"], SummaryChatKind.CHANNEL, "Source", ()
        )
        self.fetches: list[tuple[SummaryEndpoint, datetime, int]] = []
        self.sent: list[tuple[SummaryEndpoint, str]] = []

    async def resolve_endpoint(self, reference: str) -> SummaryEndpoint:
        return self.endpoints[reference]

    async def fetch_recent(
        self,
        source: SummaryEndpoint,
        *,
        since: datetime,
        limit: int | None,
    ) -> FetchedSummaryMessages:
        self.fetches.append((source, since, limit))
        return self.fetched

    async def send_text(self, destination: SummaryEndpoint, text: str) -> MessageRef:
        self.sent.append((destination, text))
        return MessageRef(destination.chat_id, 100 + len(self.sent))


class FakeSummaryGenerator:
    def __init__(self, responses: list[SummaryDocument]) -> None:
        self.responses = responses
        self.prompts: list[tuple[str, str]] = []
        self.configs: list[SummaryModelConfig] = []
        self.reset_calls = 0

    async def reset(self) -> None:
        self.reset_calls += 1

    async def generate(
        self,
        *,
        config: SummaryModelConfig,
        system_prompt: str,
        user_prompt: str,
    ) -> SummaryDocument:
        self.configs.append(config)
        self.prompts.append((system_prompt, user_prompt))
        return self.responses.pop(0)


class ConcurrentSummaryGenerator:
    def __init__(self) -> None:
        self.active = {"map": 0, "reduce": 0}
        self.peak = {"map": 0, "reduce": 0}
        self.ready = {"map": asyncio.Event(), "reduce": asyncio.Event()}

    async def reset(self) -> None:
        pass

    async def generate(
        self,
        *,
        config: SummaryModelConfig,
        system_prompt: str,
        user_prompt: str,
    ) -> SummaryDocument:
        del config, system_prompt
        payload = json.loads(user_prompt.rsplit("\n", maxsplit=1)[1])
        phase = "map" if "messages" in payload else "reduce"
        evidence = (
            [message_id for item in payload["messages"] for message_id in item["message_ids"]]
            if phase == "map"
            else [
                message_id
                for candidate in payload["candidates"]
                for topic in candidate["topics"]
                for message_id in topic["evidence_message_ids"]
            ]
        )
        self.active[phase] += 1
        self.peak[phase] = max(self.peak[phase], self.active[phase])
        if self.active[phase] >= 2:
            self.ready[phase].set()
        try:
            await asyncio.wait_for(self.ready[phase].wait(), timeout=1)
            return SummaryDocument(
                (
                    SummaryTopic(
                        f"{phase}-{min(evidence)}",
                        "中" * 1500,
                        tuple(evidence),
                    ),
                )
            )
        finally:
            self.active[phase] -= 1


def message(
    message_id: int,
    text: str,
    *,
    minute: int = 0,
    sender_id: int = 7,
    sender_name: str = "Alice",
    outgoing: bool = False,
) -> SummaryMessage:
    return SummaryMessage(
        (MessageRef(-1001, message_id),),
        datetime(2026, 8, 6, 10, minute, tzinfo=UTC),
        sender_name,
        text,
        sender_id,
        outgoing=outgoing,
    )


def test_oversized_single_message_is_split_without_losing_text() -> None:
    original = "中" * 10000

    batches = _message_batches(
        (message(10, original),),
        input_token_limit=32768,
        output_token_limit=4096,
    )

    fragments = tuple(fragment for batch in batches for fragment in batch)
    assert len(fragments) >= 2
    assert all(fragment.message_ids == (10,) for fragment in fragments)
    assert "".join(fragment.text.removeprefix("[长消息分段]\n") for fragment in fragments) == (
        original
    )


async def test_rule_management_uses_defaults_and_rejects_zero_window() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    generator = FakeSummaryGenerator([])
    service = SummarizerService(repository, repository, repository, telegram, generator)

    configured = await service.add_rule("source", "destination")
    telegram.endpoints["source"] = SummaryEndpoint(-1001, username="renamed_source")
    duplicate = await service.add_rule("source", "destination")

    assert duplicate.id == configured.id
    assert duplicate.source.username == "renamed_source"
    assert configured.window_seconds == 86400
    assert len(repository.rules) == 1
    with pytest.raises(ValueError, match="between 60 seconds"):
        await service.add_rule("source", "destination", window_seconds=0)


async def test_model_configuration_is_managed_as_business_data() -> None:
    repository = MemorySummaryRepository()
    repository.model_config = None
    generator = FakeSummaryGenerator([])
    service = SummarizerService(
        repository,
        repository,
        repository,
        FakeSummaryTelegram(),
        generator,
    )

    configured = await service.configure_model(
        " OpenAI ",
        " gpt-test ",
        api_key=" secret ",
        base_url="https://models.example/v1/",
    )
    tuned = await service.tune_model(
        input_token_limit=16000,
        output_token_limit=2000,
        temperature=0.2,
        timeout=60,
        max_retries=3,
        max_concurrency=4,
    )
    selected = await service.set_prompt_preset("technical")
    customized = await service.set_custom_prompt("只保留故障根因和验证结果")

    assert configured.provider == "openai"
    assert configured.api_key == "secret"
    assert "secret" not in repr(configured)
    assert tuned.input_token_limit == 16000
    assert tuned.max_concurrency == 4
    assert selected.prompt_preset is SummaryPromptPreset.TECHNICAL
    assert selected.custom_prompt is None
    assert customized.custom_prompt == "只保留故障根因和验证结果"
    assert await service.clear_model_config()
    assert await service.get_model_config() is None
    assert generator.reset_calls == 3
    with pytest.raises(ValueError, match="token limits must be positive"):
        SummaryModelConfig("openai", "gpt-test", output_token_limit=-1)


async def test_run_requires_business_model_configuration_before_reading_history() -> None:
    repository = MemorySummaryRepository()
    repository.model_config = None
    telegram = FakeSummaryTelegram()
    repository.rules[1] = SummaryRule(
        1, telegram.endpoints["source"], telegram.endpoints["destination"]
    )
    service = SummarizerService(
        repository,
        repository,
        repository,
        telegram,
        FakeSummaryGenerator([]),
    )

    with pytest.raises(SummaryModelUnavailableError, match="/summary model set"):
        await service.run_rule(1)
    assert telegram.fetches == []


async def test_run_merges_messages_grounds_model_output_and_sends_to_topic() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    telegram.fetched = FetchedSummaryMessages(
        telegram.endpoints["source"],
        SummaryChatKind.CHANNEL,
        "Source channel",
        (message(10, "first"), message(11, "second", minute=1)),
    )
    generator = FakeSummaryGenerator(
        [
            SummaryDocument(
                (
                    SummaryTopic(
                        "  Release   update ",
                        " Version   one shipped. ",
                        (10, 11, 999, 10),
                        (" Alice ", "Alice"),
                        (" Published ",),
                        (SummaryActionItem(" Verify ", " Alice "),),
                    ),
                )
            )
        ]
    )
    rule = SummaryRule(1, telegram.endpoints["source"], telegram.endpoints["destination"], 3600)
    repository.rules[1] = rule
    service = SummarizerService(repository, repository, repository, telegram, generator)

    execution = await service.run_rule(1)

    assert execution.message_count == 2
    assert execution.topic_count == 1
    assert telegram.sent[0][0] == SummaryEndpoint(-1002, topic_id=42)
    assert "Release update" in telegram.sent[0][1]
    assert "https://t.me/source_channel/10" in telegram.sent[0][1]
    assert "https://t.me/source_channel/11" not in telegram.sent[0][1]
    assert "消息范围: 10-11" in telegram.sent[0][1]
    assert "/999" not in telegram.sent[0][1]
    assert '"message_ids":[10,11]' in generator.prompts[0][1]
    assert generator.configs == [repository.model_config]
    assert len(repository.runs) == 1
    assert repository.runs[0].message_count == 2
    assert repository.runs[0].document.topics[0].evidence_message_ids == (10, 11)
    assert telegram.fetches[0][0] == rule.source
    assert telegram.fetches[0][2] is None
    age = datetime.now(UTC) - telegram.fetches[0][1]
    assert timedelta(minutes=59) < age < timedelta(minutes=61)


async def test_same_chat_summary_excludes_outgoing_messages() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    endpoint = telegram.endpoints["source"]
    telegram.endpoints["destination"] = endpoint
    telegram.fetched = FetchedSummaryMessages(
        endpoint,
        SummaryChatKind.GROUP,
        "Shared group",
        (
            message(10, "有效信息"),
            message(11, "上一条机器人总结", sender_id=999, sender_name="Bot", outgoing=True),
        ),
    )
    generator = FakeSummaryGenerator([SummaryDocument((SummaryTopic("主题", "结论", (10,)),))])
    repository.rules[1] = SummaryRule(1, endpoint, endpoint)
    service = SummarizerService(repository, repository, repository, telegram, generator)

    execution = await service.run_rule(1)

    assert execution.message_count == 1
    assert '"message_ids":[10]' in generator.prompts[0][1]
    assert "11" not in generator.prompts[0][1]


async def test_run_treats_an_empty_document_as_no_useful_information() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    telegram.fetched = FetchedSummaryMessages(
        telegram.endpoints["source"],
        SummaryChatKind.GROUP,
        "Chatty group",
        (message(10, "早上好"),),
    )
    repository.rules[1] = SummaryRule(
        1, telegram.endpoints["source"], telegram.endpoints["destination"]
    )
    service = SummarizerService(
        repository,
        repository,
        repository,
        telegram,
        FakeSummaryGenerator([SummaryDocument()]),
    )

    execution = await service.run_rule(1)

    assert execution.topic_count == 0
    assert "没有值得总结的有效信息" in telegram.sent[0][1]
    assert repository.runs[0].document == SummaryDocument()


async def test_large_history_uses_map_reduce_and_discards_invented_evidence() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    telegram.fetched = FetchedSummaryMessages(
        telegram.endpoints["source"],
        SummaryChatKind.GROUP,
        "Busy group",
        (
            message(10, "a" * 2200),
            message(20, "b" * 2200, minute=10, sender_id=8, sender_name="Bob"),
        ),
    )
    generator = FakeSummaryGenerator(
        [
            SummaryDocument((SummaryTopic("First", "A", (10,)),)),
            SummaryDocument((SummaryTopic("Second", "B", (20,)),)),
            SummaryDocument((SummaryTopic("Combined", "A and B", (10, 20, 999)),)),
        ]
    )
    repository.model_config = SummaryModelConfig(
        "test",
        "small-context",
        input_token_limit=4000,
        output_token_limit=500,
    )
    repository.rules[1] = SummaryRule(
        1, telegram.endpoints["source"], telegram.endpoints["destination"]
    )
    service = SummarizerService(repository, repository, repository, telegram, generator)

    execution = await service.run_rule(1)

    assert execution.topic_count == 1
    assert len(generator.prompts) == 3
    assert "分批摘要候选" in generator.prompts[-1][1]
    assert repository.runs[0].document.topics[0].evidence_message_ids == (10, 20)
    assert "/999" not in telegram.sent[0][1]


async def test_chinese_history_uses_conservative_batches_and_hierarchical_reduce() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    telegram.fetched = FetchedSummaryMessages(
        telegram.endpoints["source"],
        SummaryChatKind.GROUP,
        "Busy group",
        tuple(
            message(
                10 + index,
                "中" * 4000,
                minute=index,
                sender_id=100 + index,
                sender_name=f"User {index}",
            )
            for index in range(5)
        ),
    )
    map_documents = [
        SummaryDocument((SummaryTopic(f"Map {index}", "中" * 1500, (10 + index,)),))
        for index in range(5)
    ]
    generator = FakeSummaryGenerator(
        [
            *map_documents,
            SummaryDocument((SummaryTopic("Group A", "A", (10, 11, 12)),)),
            SummaryDocument((SummaryTopic("Group B", "B", (13, 14)),)),
            SummaryDocument((SummaryTopic("Final", "Combined", (10, 11, 12, 13, 14)),)),
        ]
    )
    repository.model_config = SummaryModelConfig("test", "structured-test")
    repository.rules[1] = SummaryRule(
        1, telegram.endpoints["source"], telegram.endpoints["destination"]
    )
    service = SummarizerService(repository, repository, repository, telegram, generator)

    execution = await service.run_rule(1)

    map_prompts_seen = [
        prompt for prompt in generator.prompts if "请筛选并总结下面 JSON" in prompt[1]
    ]
    reduce_prompts_seen = [prompt for prompt in generator.prompts if "分批摘要候选" in prompt[1]]
    assert len(map_prompts_seen) == 5
    assert len(reduce_prompts_seen) == 3
    assert execution.topic_count == 1
    assert repository.runs[0].document.topics[0].evidence_message_ids == (
        10,
        11,
        12,
        13,
        14,
    )


async def test_map_and_independent_reduce_groups_run_with_bounded_concurrency() -> None:
    repository = MemorySummaryRepository()
    telegram = FakeSummaryTelegram()
    telegram.fetched = FetchedSummaryMessages(
        telegram.endpoints["source"],
        SummaryChatKind.GROUP,
        "Busy group",
        tuple(
            message(
                100 + index,
                "中" * 4000,
                minute=index,
                sender_id=100 + index,
                sender_name=f"User {index}",
            )
            for index in range(6)
        ),
    )
    repository.model_config = SummaryModelConfig(
        "test",
        "concurrent-test",
        max_concurrency=2,
    )
    repository.rules[1] = SummaryRule(
        1, telegram.endpoints["source"], telegram.endpoints["destination"]
    )
    generator = ConcurrentSummaryGenerator()
    service = SummarizerService(repository, repository, repository, telegram, generator)

    execution = await service.run_rule(1)

    assert execution.topic_count == 1
    assert generator.peak == {"map": 2, "reduce": 2}
