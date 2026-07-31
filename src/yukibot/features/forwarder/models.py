"""Pure domain models for message forwarding."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ForwardMode(StrEnum):
    COPY = "copy"
    FORWARD = "forward"


class ContentType(StrEnum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"
    STICKER = "sticker"
    ANIMATION = "animation"
    POLL = "poll"
    LOCATION = "location"
    CONTACT = "contact"
    VENUE = "venue"
    DICE = "dice"
    GAME = "game"
    SERVICE = "service"
    OTHER = "other"


class ServiceKind(StrEnum):
    MEMBERS_JOINED = "members_joined"
    MEMBER_LEFT = "member_left"
    MESSAGE_PINNED = "message_pinned"
    TITLE_CHANGED = "title_changed"
    TOPIC_CREATED = "topic_created"
    TOPIC_CLOSED = "topic_closed"
    TOPIC_REOPENED = "topic_reopened"
    OTHER = "other"


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
class MessageRef:
    chat_id: int
    message_id: int

    def __post_init__(self) -> None:
        _validate_chat_id(self.chat_id)
        if self.message_id <= 0:
            raise ValueError("message_id must be positive")


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
class ServiceMessage:
    kind: ServiceKind
    actor_name: str | None = None
    member_names: tuple[str, ...] = ()
    new_title: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    ref: MessageRef
    content_type: ContentType
    occurred_at: datetime
    topic_id: int | None = None
    sender_id: int | None = None
    text: str | None = None
    caption: str | None = None
    reply_to_message_id: int | None = None
    media_group_id: int | str | None = None
    service: ServiceMessage | None = None
    outgoing: bool = False

    def __post_init__(self) -> None:
        _validate_topic_id(self.topic_id)
        if self.reply_to_message_id is not None and self.reply_to_message_id <= 0:
            raise ValueError("reply_to_message_id must be positive")
        if self.content_type is ContentType.SERVICE and self.service is None:
            raise ValueError("service details are required for service messages")
        if self.service is not None and self.content_type is not ContentType.SERVICE:
            raise ValueError("service details are only valid for service messages")

    @property
    def searchable_text(self) -> str:
        return self.text or self.caption or ""


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
    mode: ForwardMode = ForwardMode.COPY
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


@dataclass(frozen=True, slots=True)
class MessagesDeleted:
    message_ids: tuple[int, ...]
    chat_id: int | None = None

    def __post_init__(self) -> None:
        if not self.message_ids:
            raise ValueError("message_ids must not be empty")
        if any(message_id <= 0 for message_id in self.message_ids):
            raise ValueError("message_ids must be positive")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
