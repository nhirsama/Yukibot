"""Pure domain models for message summarization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from yukibot.contracts import MessageRef


class SummaryChatKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class SummaryPromptPreset(StrEnum):
    FOCUSED = "focused"
    DECISIONS = "decisions"
    TECHNICAL = "technical"
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class SummaryModelConfig:
    provider: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    input_token_limit: int = 32768
    output_token_limit: int = 4096
    temperature: float = 0.1
    timeout: float = 120.0
    max_retries: int = 2
    prompt_preset: SummaryPromptPreset = SummaryPromptPreset.FOCUSED
    custom_prompt: str | None = None
    max_concurrency: int = 3

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold()
        model = self.model.strip()
        if not provider or "/" in provider or any(character.isspace() for character in provider):
            raise ValueError("summary provider must be one provider name")
        if not model:
            raise ValueError("summary model must not be blank")
        if self.input_token_limit <= 0 or self.output_token_limit <= 0:
            raise ValueError("summary model token limits must be positive")
        if self.input_token_limit <= self.output_token_limit + 2000:
            raise ValueError("summary token limits leave no usable input budget")
        if not 0 <= self.temperature <= 2:
            raise ValueError("summary model temperature must be between 0 and 2")
        if not 0 < self.timeout <= 1800 or not 0 <= self.max_retries <= 10:
            raise ValueError("summary model timeout and retry count are invalid")
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("summary model concurrency must be between 1 and 8")
        try:
            prompt_preset = SummaryPromptPreset(self.prompt_preset)
        except ValueError as error:
            raise ValueError("unknown summary prompt preset") from error
        api_key = self.api_key.strip() if self.api_key is not None else None
        base_url = self.base_url.strip().rstrip("/") if self.base_url is not None else None
        custom_prompt = self.custom_prompt.strip() if self.custom_prompt is not None else None
        if custom_prompt is not None and len(custom_prompt) > 4000:
            raise ValueError("custom summary prompt must not exceed 4000 characters")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key or None)
        object.__setattr__(self, "base_url", base_url or None)
        object.__setattr__(self, "prompt_preset", prompt_preset)
        object.__setattr__(self, "custom_prompt", custom_prompt or None)


def _normalize_username(username: str | None) -> str | None:
    if username is None:
        return None
    normalized = username.strip().removeprefix("@").strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("Telegram username must not be empty or contain whitespace")
    return normalized


@dataclass(frozen=True, slots=True)
class SummaryEndpoint:
    chat_id: int
    topic_id: int | None = None
    username: str | None = None

    def __post_init__(self) -> None:
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if self.topic_id is not None and self.topic_id <= 0:
            raise ValueError("topic_id must be positive")
        object.__setattr__(self, "username", _normalize_username(self.username))


@dataclass(frozen=True, slots=True)
class SummaryRule:
    id: int
    source: SummaryEndpoint
    destination: SummaryEndpoint
    window_seconds: int = 86400
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("summary rule id must be positive")
        if not 60 <= self.window_seconds <= 30 * 86400:
            raise ValueError("summary window must be between 60 seconds and 30 days")


@dataclass(frozen=True, slots=True)
class SummaryRuleDraft:
    source: SummaryEndpoint
    destination: SummaryEndpoint
    window_seconds: int = 86400
    enabled: bool = True

    def __post_init__(self) -> None:
        if not 60 <= self.window_seconds <= 30 * 86400:
            raise ValueError("summary window must be between 60 seconds and 30 days")

    def bind(self, rule_id: int) -> SummaryRule:
        return SummaryRule(
            rule_id,
            self.source,
            self.destination,
            self.window_seconds,
            self.enabled,
        )

    def matches(self, rule: SummaryRule) -> bool:
        return (
            self.source.chat_id == rule.source.chat_id
            and self.source.topic_id == rule.source.topic_id
            and self.destination.chat_id == rule.destination.chat_id
            and self.destination.topic_id == rule.destination.topic_id
            and self.window_seconds == rule.window_seconds
        )


@dataclass(frozen=True, slots=True)
class SummaryMessage:
    refs: tuple[MessageRef, ...]
    occurred_at: datetime
    sender_name: str
    text: str
    sender_id: int | None = None
    reply_to_message_id: int | None = None
    grouped_id: int | str | None = None
    forwarded_from: str | None = None
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.refs:
            raise ValueError("summary message refs must not be empty")
        if any(ref.chat_id != self.refs[0].chat_id for ref in self.refs):
            raise ValueError("merged summary messages must belong to one chat")
        if not self.sender_name.strip():
            raise ValueError("sender_name must not be blank")
        if not self.text.strip():
            raise ValueError("summary message text must not be blank")

    @property
    def message_ids(self) -> tuple[int, ...]:
        return tuple(ref.message_id for ref in self.refs)


@dataclass(frozen=True, slots=True)
class FetchedSummaryMessages:
    source: SummaryEndpoint
    chat_kind: SummaryChatKind
    chat_title: str
    messages: tuple[SummaryMessage, ...]


@dataclass(frozen=True, slots=True)
class SummaryActionItem:
    task: str
    owner: str | None = None
    deadline: str | None = None


@dataclass(frozen=True, slots=True)
class SummaryTopic:
    title: str
    summary: str
    evidence_message_ids: tuple[int, ...]
    participants: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    action_items: tuple[SummaryActionItem, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SummaryDocument:
    topics: tuple[SummaryTopic, ...] = ()


@dataclass(frozen=True, slots=True)
class SummaryExecution:
    rule: SummaryRule
    message_count: int
    topic_count: int
    sent_messages: tuple[MessageRef, ...]


@dataclass(frozen=True, slots=True)
class SummaryRun:
    rule_id: int
    started_at: datetime
    completed_at: datetime
    first_message_id: int
    last_message_id: int
    message_count: int
    provider: str
    model: str
    prompt_version: int
    document: SummaryDocument = field(default_factory=SummaryDocument)

    def __post_init__(self) -> None:
        if self.rule_id <= 0:
            raise ValueError("summary run rule id must be positive")
        if self.message_count <= 0:
            raise ValueError("summary run message count must be positive")
        if self.first_message_id <= 0 or self.last_message_id < self.first_message_id:
            raise ValueError("summary run message range is invalid")
