"""Pure domain models for message forwarding."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from yukibot.contracts import (
    MessageRef as MessageRef,
)
from yukibot.contracts import (
    TelegramContentType as ContentType,
)
from yukibot.contracts import (
    TelegramMessage as IncomingMessage,
)
from yukibot.contracts import (
    TelegramMessagesDeleted as MessagesDeleted,
)
from yukibot.contracts import (
    TelegramServiceKind as ServiceKind,
)
from yukibot.contracts import (
    TelegramServiceMessage as ServiceMessage,
)

__all__ = [
    "ContentType",
    "DestinationEndpoint",
    "ForwardMode",
    "IncomingMessage",
    "ManagedTopic",
    "MessageFilter",
    "MessageLink",
    "MessageRef",
    "MessagesDeleted",
    "Route",
    "ServiceKind",
    "ServiceMessage",
    "SourceEndpoint",
    "normalize_general_topic",
]


class ForwardMode(StrEnum):
    COPY = "copy"
    FORWARD = "forward"


def normalize_general_topic(topic_id: int | None) -> int:
    """Normalize Telegram's None/0/1 representations of the General topic."""

    return 1 if topic_id in (None, 0, 1) else topic_id


def _validate_chat_id(chat_id: int) -> None:
    if chat_id == 0:
        raise ValueError("chat_id must not be zero")


def _validate_topic_id(topic_id: int | None) -> None:
    if topic_id is not None and topic_id < 0:
        raise ValueError("topic_id must not be negative")


@dataclass(frozen=True, slots=True)
class SourceEndpoint:
    """A source chat and an optional topic filter; None means every topic."""

    chat_id: int
    topic_id: int | None = None

    def __post_init__(self) -> None:
        _validate_chat_id(self.chat_id)
        _validate_topic_id(self.topic_id)

    def matches(self, chat_id: int, topic_id: int | None) -> bool:
        if self.chat_id != chat_id:
            return False
        if self.topic_id is None:
            return True
        return normalize_general_topic(self.topic_id) == normalize_general_topic(topic_id)


@dataclass(frozen=True, slots=True)
class DestinationEndpoint:
    """A destination chat; None means no explicit forum topic."""

    chat_id: int
    topic_id: int | None = None

    def __post_init__(self) -> None:
        _validate_chat_id(self.chat_id)
        _validate_topic_id(self.topic_id)


@dataclass(frozen=True, slots=True)
class ManagedTopic:
    """A topic created and named from one source chat in one destination forum."""

    source_chat_id: int
    destination_chat_id: int
    topic_id: int
    title: str

    def __post_init__(self) -> None:
        _validate_chat_id(self.source_chat_id)
        _validate_chat_id(self.destination_chat_id)
        if self.topic_id <= 0:
            raise ValueError("topic_id must be positive")
        if not self.title:
            raise ValueError("topic title must not be empty")


@dataclass(frozen=True, slots=True)
class MessageFilter:
    keywords: tuple[str, ...] = ()
    allowed_content_types: frozenset[ContentType] = field(default_factory=frozenset)
    blocked_content_types: frozenset[ContentType] = field(default_factory=frozenset)
    include_service_messages: bool = False

    def __post_init__(self) -> None:
        normalized = tuple(keyword.strip() for keyword in self.keywords if keyword.strip())
        object.__setattr__(self, "keywords", normalized)

    def allows(self, message: IncomingMessage, *, check_keywords: bool = True) -> bool:
        if message.content_type is ContentType.SERVICE and not self.include_service_messages:
            return False
        if self.allowed_content_types and message.content_type not in self.allowed_content_types:
            return False
        if message.content_type in self.blocked_content_types:
            return False
        if check_keywords and self.keywords:
            text = message.searchable_text.casefold()
            return any(keyword.casefold() in text for keyword in self.keywords)
        return True

    def allows_album(self, messages: tuple[IncomingMessage, ...]) -> bool:
        content_allowed = all(self.allows(message, check_keywords=False) for message in messages)
        if not messages or not content_allowed:
            return False
        if not self.keywords:
            return True
        text = "\n".join(message.searchable_text for message in messages).casefold()
        return any(keyword.casefold() in text for keyword in self.keywords)


@dataclass(frozen=True, slots=True)
class Route:
    id: int
    source: SourceEndpoint
    destination: DestinationEndpoint
    mode: ForwardMode = ForwardMode.FORWARD
    message_filter: MessageFilter = field(default_factory=MessageFilter)
    enabled: bool = True
    fallback_to_copy: bool = True

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("route id must be positive")

    def matches(self, message: IncomingMessage) -> bool:
        return (
            self.enabled
            and self.source.matches(message.ref.chat_id, message.topic_id)
            and self.message_filter.allows(message)
        )

    def matches_album(self, messages: tuple[IncomingMessage, ...]) -> bool:
        if not messages:
            return False
        return (
            self.enabled
            and self.source.matches(messages[0].ref.chat_id, messages[0].topic_id)
            and self.message_filter.allows_album(messages)
        )


@dataclass(frozen=True, slots=True)
class MessageLink:
    route_id: int
    source: MessageRef
    destination: MessageRef

    def __post_init__(self) -> None:
        if self.route_id <= 0:
            raise ValueError("route_id must be positive")
