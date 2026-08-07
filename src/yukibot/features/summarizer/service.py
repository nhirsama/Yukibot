"""Message-aware map/reduce summarization application service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import (
    NoMessagesToSummarizeError,
    SummarizerError,
    SummaryModelUnavailableError,
    SummaryRuleNotFoundError,
)
from .models import (
    FetchedSummaryMessages,
    SummaryActionItem,
    SummaryChatKind,
    SummaryDocument,
    SummaryEndpoint,
    SummaryExecution,
    SummaryMessage,
    SummaryModelConfig,
    SummaryPromptPreset,
    SummaryRule,
    SummaryRuleDraft,
    SummaryRun,
    SummaryTopic,
)
from .ports import (
    StructuredSummaryGenerator,
    SummaryModelConfigRepository,
    SummaryRuleRepository,
    SummaryRunRepository,
    SummaryTelegram,
)
from .prompts import PROMPT_VERSION, map_prompts, prompt_preference, reduce_prompts

_PROMPT_RESERVE_TOKENS = 2000
_MAX_BATCH_INPUT_TOKENS = 12000


class SummarizerService:
    def __init__(
        self,
        rules: SummaryRuleRepository,
        runs: SummaryRunRepository,
        model_configs: SummaryModelConfigRepository,
        telegram: SummaryTelegram,
        generator: StructuredSummaryGenerator,
        *,
        default_window_seconds: int = 86400,
        max_topics: int = 12,
    ) -> None:
        if not 60 <= default_window_seconds <= 30 * 86400:
            raise ValueError("default summary window must be between 60 seconds and 30 days")
        if max_topics <= 0:
            raise ValueError("summary limits must be positive")
        self._rules = rules
        self._runs = runs
        self._model_configs = model_configs
        self._telegram = telegram
        self._generator = generator
        self._default_window_seconds = default_window_seconds
        self._max_topics = max_topics

    @property
    def default_window_seconds(self) -> int:
        return self._default_window_seconds

    async def resolve_endpoint(self, reference: str) -> SummaryEndpoint:
        return await self._telegram.resolve_endpoint(reference)

    async def get_model_config(self) -> SummaryModelConfig | None:
        return await self._model_configs.get_model_config()

    async def configure_model(
        self,
        provider: str,
        model: str,
        *,
        api_key: str | None,
        base_url: str | None,
    ) -> SummaryModelConfig:
        current = await self._model_configs.get_model_config()
        config = (
            replace(
                current,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
            )
            if current is not None
            else SummaryModelConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
            )
        )
        await self._generator.reset()
        await self._model_configs.save_model_config(config)
        return config

    async def tune_model(
        self,
        *,
        input_token_limit: int,
        output_token_limit: int,
        temperature: float,
        timeout: float,
        max_retries: int,
        max_concurrency: int | None = None,
    ) -> SummaryModelConfig:
        current = await self._require_model_config()
        config = replace(
            current,
            input_token_limit=input_token_limit,
            output_token_limit=output_token_limit,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            max_concurrency=(
                current.max_concurrency if max_concurrency is None else max_concurrency
            ),
        )
        await self._generator.reset()
        await self._model_configs.save_model_config(config)
        return config

    async def set_prompt_preset(self, preset: str) -> SummaryModelConfig:
        current = await self._require_model_config()
        try:
            selected = SummaryPromptPreset(preset.strip().casefold())
        except ValueError as error:
            raise ValueError(f"未知总结预设: {preset}") from error
        config = replace(current, prompt_preset=selected, custom_prompt=None)
        await self._model_configs.save_model_config(config)
        return config

    async def set_custom_prompt(self, prompt: str) -> SummaryModelConfig:
        current = await self._require_model_config()
        config = replace(current, custom_prompt=prompt)
        await self._model_configs.save_model_config(config)
        return config

    async def clear_model_config(self) -> bool:
        await self._generator.reset()
        return await self._model_configs.clear_model_config()

    async def list_rules(self) -> tuple[SummaryRule, ...]:
        return tuple(await self._rules.list_all())

    async def get_rule(self, rule_id: int) -> SummaryRule:
        rule = next((item for item in await self._rules.list_all() if item.id == rule_id), None)
        if rule is None:
            raise SummaryRuleNotFoundError(f"summary rule {rule_id} does not exist")
        return rule

    async def add_rule(
        self,
        source_reference: str,
        destination_reference: str,
        *,
        window_seconds: int | None = None,
    ) -> SummaryRule:
        draft = await self._draft(
            source_reference,
            destination_reference,
            window_seconds=window_seconds,
        )
        existing = next(
            (rule for rule in await self._rules.list_all() if draft.matches(rule)),
            None,
        )
        if existing is None:
            return await self._rules.add_auto(draft)
        refreshed = replace(draft.bind(existing.id), enabled=existing.enabled)
        if refreshed != existing:
            await self._rules.replace(refreshed)
        return refreshed

    async def replace_rule(
        self,
        rule_id: int,
        source_reference: str,
        destination_reference: str,
        *,
        window_seconds: int | None = None,
    ) -> SummaryRule:
        existing = await self.get_rule(rule_id)
        draft = await self._draft(
            source_reference,
            destination_reference,
            window_seconds=window_seconds,
        )
        rule = replace(draft.bind(rule_id), enabled=existing.enabled)
        try:
            await self._rules.replace(rule)
        except KeyError as error:
            raise SummaryRuleNotFoundError(f"summary rule {rule_id} does not exist") from error
        return rule

    async def set_enabled(self, rule_id: int, *, enabled: bool) -> SummaryRule:
        rule = replace(await self.get_rule(rule_id), enabled=enabled)
        await self._rules.replace(rule)
        return rule

    async def remove_rule(self, rule_id: int) -> None:
        if not await self._rules.remove(rule_id):
            raise SummaryRuleNotFoundError(f"summary rule {rule_id} does not exist")

    async def run_rule(
        self,
        rule_id: int,
        *,
        window_seconds: int | None = None,
    ) -> SummaryExecution:
        rule = await self.get_rule(rule_id)
        if not rule.enabled:
            raise SummarizerError(f"summary rule {rule_id} is disabled")
        model_config = await self._require_model_config()
        effective_window = rule.window_seconds if window_seconds is None else window_seconds
        if not 60 <= effective_window <= 30 * 86400:
            raise ValueError("summary window must be between 60 seconds and 30 days")
        started_at = datetime.now(UTC)
        fetched = await self._telegram.fetch_recent(
            rule.source,
            since=started_at - timedelta(seconds=effective_window),
            limit=None,
        )
        useful = tuple(message for message in fetched.messages if message.text.strip())
        if not useful:
            raise NoMessagesToSummarizeError("所选时间范围内没有可总结的文字消息")
        source = replace(fetched, messages=_merge_messages(useful))
        document = await self._summarize(source, model_config)
        chunks = _split_for_telegram(_render(document, source))
        sent = tuple([await self._telegram.send_text(rule.destination, chunk) for chunk in chunks])
        completed_at = datetime.now(UTC)
        raw_ids = [message_id for message in useful for message_id in message.message_ids]
        await self._runs.save(
            SummaryRun(
                rule.id,
                started_at,
                completed_at,
                min(raw_ids),
                max(raw_ids),
                len(useful),
                model_config.provider,
                model_config.model,
                PROMPT_VERSION,
                document,
            )
        )
        return SummaryExecution(rule, len(useful), len(document.topics), sent)

    async def _draft(
        self,
        source_reference: str,
        destination_reference: str,
        *,
        window_seconds: int | None,
    ) -> SummaryRuleDraft:
        source = await self._telegram.resolve_endpoint(source_reference)
        destination = await self._telegram.resolve_endpoint(destination_reference)
        return SummaryRuleDraft(
            source,
            destination,
            self._default_window_seconds if window_seconds is None else window_seconds,
        )

    async def _require_model_config(self) -> SummaryModelConfig:
        config = await self._model_configs.get_model_config()
        if config is None:
            raise SummaryModelUnavailableError(
                "消息总结模型未配置, 请使用 /summary model set 配置。"
            )
        return config

    async def _summarize(
        self,
        source: FetchedSummaryMessages,
        model_config: SummaryModelConfig,
    ) -> SummaryDocument:
        preference = prompt_preference(
            model_config.prompt_preset,
            model_config.custom_prompt,
        )
        batches = _message_batches(
            source.messages,
            input_token_limit=model_config.input_token_limit,
            output_token_limit=model_config.output_token_limit,
            preference=preference,
        )
        semaphore = asyncio.Semaphore(model_config.max_concurrency)
        mapped = await _gather_cancel_on_error(
            [
                self._summarize_batch(
                    source,
                    batch,
                    model_config,
                    preference,
                    semaphore,
                )
                for batch in batches
            ]
        )
        documents = tuple(document for document in mapped if document.topics)
        if not documents:
            return SummaryDocument()
        return await self._reduce_documents(
            source,
            documents,
            model_config,
            preference,
            semaphore,
        )

    async def _summarize_batch(
        self,
        source: FetchedSummaryMessages,
        batch: tuple[SummaryMessage, ...],
        model_config: SummaryModelConfig,
        preference: str,
        semaphore: asyncio.Semaphore,
    ) -> SummaryDocument:
        allowed = frozenset(message_id for message in batch for message_id in message.message_ids)
        system, user = map_prompts(
            source,
            [_message_payload(message) for message in batch],
            preference,
        )
        async with semaphore:
            generated = await self._generator.generate(
                config=model_config,
                system_prompt=system,
                user_prompt=user,
            )
        return _ground_document(generated, allowed, max_topics=self._max_topics)

    async def _reduce_documents(
        self,
        source: FetchedSummaryMessages,
        documents: tuple[SummaryDocument, ...],
        model_config: SummaryModelConfig,
        preference: str,
        semaphore: asyncio.Semaphore,
    ) -> SummaryDocument:
        current = documents
        allowed_ids = frozenset(
            message_id for message in source.messages for message_id in message.message_ids
        )
        while len(current) > 1:
            groups = _document_batches(
                source,
                current,
                input_token_limit=model_config.input_token_limit,
                output_token_limit=model_config.output_token_limit,
                preference=preference,
            )
            if all(len(group) == 1 for group in groups):
                return _merge_documents(current, self._max_topics)
            current = await _gather_cancel_on_error(
                [
                    self._reduce_group(
                        source,
                        group,
                        model_config,
                        preference,
                        semaphore,
                        allowed_ids,
                    )
                    for group in groups
                ]
            )
        return current[0]

    async def _reduce_group(
        self,
        source: FetchedSummaryMessages,
        group: tuple[SummaryDocument, ...],
        model_config: SummaryModelConfig,
        preference: str,
        semaphore: asyncio.Semaphore,
        allowed_ids: frozenset[int],
    ) -> SummaryDocument:
        if len(group) == 1:
            return group[0]
        system, user = reduce_prompts(source, group, preference)
        async with semaphore:
            reduced = await self._generator.generate(
                config=model_config,
                system_prompt=system,
                user_prompt=user,
            )
        grounded = _ground_document(reduced, allowed_ids, max_topics=self._max_topics)
        return grounded if grounded.topics else _merge_documents(group, self._max_topics)


def _merge_messages(messages: tuple[SummaryMessage, ...]) -> tuple[SummaryMessage, ...]:
    ordered = sorted(
        messages,
        key=lambda message: (message.occurred_at, message.refs[0].message_id),
    )
    merged: list[SummaryMessage] = []
    for message in ordered:
        if not merged:
            merged.append(message)
            continue
        previous = merged[-1]
        same_album = message.grouped_id is not None and message.grouped_id == previous.grouped_id
        consecutive = (
            message.grouped_id is None
            and previous.grouped_id is None
            and message.sender_id == previous.sender_id
            and message.sender_name == previous.sender_name
            and message.reply_to_message_id == previous.reply_to_message_id
            and len(previous.refs) < 3
            and (message.occurred_at - previous.occurred_at).total_seconds() <= 180
        )
        if same_album or consecutive:
            merged[-1] = SummaryMessage(
                (*previous.refs, *message.refs),
                previous.occurred_at,
                previous.sender_name,
                f"{previous.text}\n{message.text}",
                previous.sender_id,
                previous.reply_to_message_id,
                previous.grouped_id or message.grouped_id,
                previous.forwarded_from or message.forwarded_from,
                tuple(dict.fromkeys((*previous.links, *message.links))),
            )
        else:
            merged.append(message)
    return tuple(merged)


def _message_payload(message: SummaryMessage) -> dict[str, object]:
    return {
        "message_ids": message.message_ids,
        "time": message.occurred_at.isoformat(),
        "sender": {"id": message.sender_id, "name": message.sender_name},
        "reply_to_message_id": message.reply_to_message_id,
        "forwarded_from": message.forwarded_from,
        "text": message.text,
        "links": message.links,
    }


def _message_batches(
    messages: tuple[SummaryMessage, ...],
    *,
    input_token_limit: int,
    output_token_limit: int,
    preference: str = "",
) -> tuple[tuple[SummaryMessage, ...], ...]:
    token_budget = _batch_token_budget(
        input_token_limit,
        output_token_limit,
        extra_reserved_tokens=_estimate_tokens(preference),
    )
    batches: list[list[SummaryMessage]] = [[]]
    used = 0
    for message in messages:
        for prepared in _split_message(message, token_budget):
            size = _message_tokens(prepared)
            if batches[-1] and used + size > token_budget:
                batches.append([])
                used = 0
            batches[-1].append(prepared)
            used += size
    return tuple(tuple(batch) for batch in batches if batch)


def _document_batches(
    source: FetchedSummaryMessages,
    documents: tuple[SummaryDocument, ...],
    *,
    input_token_limit: int,
    output_token_limit: int,
    preference: str = "",
) -> tuple[tuple[SummaryDocument, ...], ...]:
    token_budget = _batch_token_budget(
        input_token_limit,
        output_token_limit,
        extra_reserved_tokens=_estimate_tokens(preference),
    )
    batches: list[list[SummaryDocument]] = [[]]
    for document in documents:
        candidate = (*batches[-1], document)
        system, user = reduce_prompts(source, candidate, preference)
        if batches[-1] and _estimate_tokens(system + user) > token_budget:
            batches.append([document])
        else:
            batches[-1].append(document)
    return tuple(tuple(batch) for batch in batches if batch)


def _batch_token_budget(
    input_token_limit: int,
    output_token_limit: int,
    *,
    extra_reserved_tokens: int = 0,
) -> int:
    available = (
        input_token_limit - output_token_limit - _PROMPT_RESERVE_TOKENS - extra_reserved_tokens
    )
    if available < 128:
        raise ValueError("summary model token limits leave no usable input budget")
    return min(available, _MAX_BATCH_INPUT_TOKENS)


def _message_tokens(message: SummaryMessage) -> int:
    payload = json.dumps(_message_payload(message), ensure_ascii=False, separators=(",", ":"))
    return _estimate_tokens(payload)


def _estimate_tokens(text: str) -> int:
    ascii_count = sum(character.isascii() for character in text)
    return (ascii_count + 2) // 3 + (len(text) - ascii_count) * 2


def _split_message(message: SummaryMessage, token_budget: int) -> tuple[SummaryMessage, ...]:
    if _message_tokens(message) <= token_budget:
        return (message,)
    marker = "[长消息分段]\n"
    fragments: list[SummaryMessage] = []
    start = 0
    while start < len(message.text):
        lower, upper = start + 1, len(message.text)
        end = lower
        while lower <= upper:
            midpoint = (lower + upper) // 2
            candidate = replace(message, text=marker + message.text[start:midpoint])
            if _message_tokens(candidate) <= token_budget:
                end = midpoint
                lower = midpoint + 1
            else:
                upper = midpoint - 1
        fragments.append(replace(message, text=marker + message.text[start:end]))
        start = end
    return tuple(fragments)


def _ground_document(
    document: SummaryDocument,
    allowed_ids: frozenset[int],
    *,
    max_topics: int,
) -> SummaryDocument:
    topics: list[SummaryTopic] = []
    seen: set[str] = set()
    for topic in document.topics:
        title = " ".join(topic.title.split())
        summary = " ".join(topic.summary.split())
        key = title.casefold()
        evidence = tuple(
            dict.fromkeys(item for item in topic.evidence_message_ids if item in allowed_ids)
        )
        if not title or not summary or not evidence or key in seen:
            continue
        seen.add(key)
        topics.append(
            SummaryTopic(
                title,
                summary,
                evidence,
                _clean_strings(topic.participants),
                _clean_strings(topic.decisions),
                tuple(
                    SummaryActionItem(
                        " ".join(item.task.split()),
                        " ".join(item.owner.split()) if item.owner else None,
                        " ".join(item.deadline.split()) if item.deadline else None,
                    )
                    for item in topic.action_items
                    if item.task.strip()
                ),
                _clean_strings(topic.open_questions),
            )
        )
        if len(topics) >= max_topics:
            break
    return SummaryDocument(tuple(topics))


def _merge_documents(documents: tuple[SummaryDocument, ...], max_topics: int) -> SummaryDocument:
    topics: list[SummaryTopic] = []
    seen: set[str] = set()
    for document in documents:
        for topic in document.topics:
            key = topic.title.casefold()
            if key in seen:
                continue
            seen.add(key)
            topics.append(topic)
            if len(topics) >= max_topics:
                return SummaryDocument(tuple(topics))
    return SummaryDocument(tuple(topics))


def _clean_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(cleaned for value in values if (cleaned := " ".join(value.split()))))


async def _gather_cancel_on_error[T](
    coroutines: list[Coroutine[Any, Any, T]],
) -> tuple[T, ...]:
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _render(document: SummaryDocument, source: FetchedSummaryMessages) -> str:
    lines = [f"{source.chat_title} 消息总结"]
    if not document.topics:
        lines.extend(("", "所选时间范围内没有值得总结的有效信息。"))
        return "\n".join(lines)
    for index, topic in enumerate(document.topics, start=1):
        lines.extend(("", f"{index}. {topic.title}", topic.summary))
        if source.chat_kind is not SummaryChatKind.CHANNEL and topic.participants:
            lines.append(f"参与者: {', '.join(topic.participants)}")
        if topic.decisions:
            lines.append(f"结论: {'; '.join(topic.decisions)}")
        for item in topic.action_items:
            detail = item.task
            if item.owner:
                detail += f" | 负责人: {item.owner}"
            if item.deadline:
                detail += f" | 截止: {item.deadline}"
            lines.append(f"行动项: {detail}")
        if topic.open_questions:
            lines.append(f"待确认: {'; '.join(topic.open_questions)}")
        evidence = sorted(set(topic.evidence_message_ids))
        first_message_id, last_message_id = evidence[0], evidence[-1]
        message_range = (
            str(first_message_id)
            if first_message_id == last_message_id
            else f"{first_message_id}-{last_message_id}"
        )
        lines.append(
            f"原消息: {_message_reference(source, first_message_id)} | 消息范围: {message_range}"
        )
    return "\n".join(lines)


def _message_reference(source: FetchedSummaryMessages, message_id: int) -> str:
    endpoint = source.source
    if source.chat_kind is not SummaryChatKind.PRIVATE and endpoint.username is not None:
        return f"https://t.me/{endpoint.username}/{message_id}"
    raw = str(endpoint.chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return f"#{message_id}"


def _split_for_telegram(text: str, limit: int = 3900) -> tuple[str, ...]:
    if len(text) <= limit:
        return (text,)
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return tuple(chunks)
