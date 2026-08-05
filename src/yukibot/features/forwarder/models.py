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
    "ChatIdentity",
    "ContentType",
    "DestinationEndpoint",
    "ForwardMode",
    "IncomingMessage",
    "ManagedTopic",
    "MessageFilter",
    "MessageLink",
    "MessageRef",
    "MessagesDeleted",
    "PollCursor",
    "Route",
    "RouteDraft",
    "ServiceKind",
    "ServiceMessage",
    "SourceEndpoint",
    "normalize_general_topic",
]


class ForwardMode(StrEnum):
    COPY = "copy"
    FORWARD = "forward"


@dataclass(frozen=True, slots=True)
class ChatIdentity:
    """A Telegram chat resolved from either a numeric ID or public username."""

    chat_id: int
    username: str | None = None

    def __post_init__(self) -> None:
        _validate_chat_id(self.chat_id)
        object.__setattr__(self, "username", _normalize_username(self.username))


def normalize_general_topic(topic_id: int | None) -> int:
    """Normalize Telegram's None/0/1 representations of the General topic."""

    return 1 if topic_id in (None, 0, 1) else topic_id


def _validate_chat_id(chat_id: int) -> None:
    if chat_id == 0:
        raise ValueError("chat_id must not be zero")


def _validate_topic_id(topic_id: int | None) -> None:
    if topic_id is not None and topic_id < 0:
        raise ValueError("topic_id must not be negative")


def _normalize_username(username: str | None) -> str | None:
    if username is None:
        return None
    normalized = username.strip().removeprefix("@").strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("Telegram username must not be empty or contain whitespace")
    return normalized


@dataclass(frozen=True, slots=True)
class SourceEndpoint:
    """A source chat and an optional topic filter; None means every topic."""

    chat_id: int
    topic_id: int | None = None
    username: str | None = None
    poll_interval_seconds: int | None = None

    def __post_init__(self) -> None:
        _validate_chat_id(self.chat_id)
        _validate_topic_id(self.topic_id)
        object.__setattr__(self, "username", _normalize_username(self.username))
        if self.poll_interval_seconds is not None and self.poll_interval_seconds < 60:
            raise ValueError("poll interval must be at least 60 seconds")

    @property
    def is_polled(self) -> bool:
        return self.poll_interval_seconds is not None

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
    username: str | None = None

    def __post_init__(self) -> None:
        _validate_chat_id(self.chat_id)
        _validate_topic_id(self.topic_id)
        object.__setattr__(self, "username", _normalize_username(self.username))


@dataclass(frozen=True, slots=True)
class PollCursor:
    """Highest source message ID durably handed to the forwarding queue."""

    source_chat_id: int
    last_message_id: int

    def __post_init__(self) -> None:
        _validate_chat_id(self.source_chat_id)
        if self.last_message_id < 0:
            raise ValueError("last_message_id must not be negative")


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
class RouteDraft:
    """A route configuration whose persistent ID has not been allocated yet."""

    source: SourceEndpoint
    destination: DestinationEndpoint
    mode: ForwardMode = ForwardMode.FORWARD
    message_filter: MessageFilter = field(default_factory=MessageFilter)
    enabled: bool = True
    fallback_to_copy: bool = True

    def bind(self, route_id: int) -> Route:
        return Route(
            route_id,
            self.source,
            self.destination,
            mode=self.mode,
            message_filter=self.message_filter,
            enabled=self.enabled,
            fallback_to_copy=self.fallback_to_copy,
        )

    def matches(self, route: Route) -> bool:
        return (
            self.source == route.source
            and self.destination == route.destination
            and self.mode is route.mode
            and self.message_filter == route.message_filter
            and self.fallback_to_copy == route.fallback_to_copy
        )


@dataclass(frozen=True, slots=True)
class MessageLink:
    route_id: int
    source: MessageRef
    destination: MessageRef

    def __post_init__(self) -> None:
        if self.route_id <= 0:
            raise ValueError("route_id must be positive")
