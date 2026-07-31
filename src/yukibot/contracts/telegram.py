"""Telegram data normalized at the adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TelegramContentType(StrEnum):
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


class TelegramServiceKind(StrEnum):
    MEMBERS_JOINED = "members_joined"
    MEMBER_LEFT = "member_left"
    MESSAGE_PINNED = "message_pinned"
    TITLE_CHANGED = "title_changed"
    TOPIC_CREATED = "topic_created"
    TOPIC_CLOSED = "topic_closed"
    TOPIC_REOPENED = "topic_reopened"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class MessageRef:
    chat_id: int
    message_id: int

    def __post_init__(self) -> None:
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        if self.message_id <= 0:
            raise ValueError("message_id must be positive")


@dataclass(frozen=True, slots=True)
class TelegramServiceMessage:
    kind: TelegramServiceKind
    actor_name: str | None = None
    member_names: tuple[str, ...] = ()
    new_title: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    ref: MessageRef
    content_type: TelegramContentType
    occurred_at: datetime
    sender_id: int | None = None
    topic_id: int | None = None
    grouped_id: int | str | None = None
    text: str | None = None
    caption: str | None = None
    reply_to_message_id: int | None = None
    service: TelegramServiceMessage | None = None
    outgoing: bool = False
    edited_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.topic_id is not None and self.topic_id < 0:
            raise ValueError("topic_id must not be negative")
        if self.reply_to_message_id is not None and self.reply_to_message_id <= 0:
            raise ValueError("reply_to_message_id must be positive")
        if self.content_type is TelegramContentType.SERVICE and self.service is None:
            raise ValueError("service details are required for service messages")
        if self.service is not None and self.content_type is not TelegramContentType.SERVICE:
            raise ValueError("service details are only valid for service messages")

    @property
    def searchable_text(self) -> str:
        return self.text or self.caption or ""
