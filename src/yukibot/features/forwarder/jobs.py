"""Durable forwarder jobs and deterministic event identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from yukibot.contracts import (
    TelegramMessage,
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
)

type ForwardJobEvent = TelegramMessageReceived | TelegramMessageEdited | TelegramMessagesDeleted


class ForwardJobKind(StrEnum):
    RECEIVE = "receive"
    EDIT = "edit"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class PendingForwardJob:
    kind: ForwardJobKind
    event: ForwardJobEvent
    deduplication_key: str
    available_at: float
    group_key: str | None = None


@dataclass(frozen=True, slots=True)
class ForwardJob:
    id: int
    kind: ForwardJobKind
    event: ForwardJobEvent
    attempts: int
    group_key: str | None = None

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("job id must be positive")
        if self.attempts <= 0:
            raise ValueError("job attempts must be positive after claiming")


def pending_jobs_for_event(
    event: ForwardJobEvent,
    *,
    now: float,
    album_delay: float,
) -> tuple[PendingForwardJob, ...]:
    if album_delay < 0:
        raise ValueError("album_delay must not be negative")
    if isinstance(event, TelegramMessageReceived):
        message = event.message
        group_key = (
            f"album:{message.ref.chat_id}:{message.grouped_id}"
            if message.grouped_id is not None
            else None
        )
        return (
            PendingForwardJob(
                ForwardJobKind.RECEIVE,
                event,
                f"receive:{message.ref.chat_id}:{message.ref.message_id}",
                now + album_delay if group_key is not None else now,
                group_key,
            ),
        )
    if isinstance(event, TelegramMessageEdited):
        message = event.message
        version = message.edited_at or message.occurred_at
        fingerprint = _editable_content_fingerprint(message)
        return (
            PendingForwardJob(
                ForwardJobKind.EDIT,
                event,
                f"edit:{message.ref.chat_id}:{message.ref.message_id}:"
                f"{version.isoformat()}:{fingerprint}",
                now,
            ),
        )

    chat = event.chat_id if event.chat_id is not None else "unknown"
    return tuple(
        PendingForwardJob(
            ForwardJobKind.DELETE,
            TelegramMessagesDeleted((message_id,), event.occurred_at, chat_id=event.chat_id),
            f"delete:{chat}:{message_id}",
            now,
        )
        for message_id in event.message_ids
    )


def _editable_content_fingerprint(message: TelegramMessage) -> str:
    service = message.service
    fields = (
        message.content_type.value,
        message.text or "",
        message.caption or "",
        service.kind.value if service is not None else "",
        service.actor_name or "" if service is not None else "",
        "\x1e".join(service.member_names) if service is not None else "",
        service.new_title or "" if service is not None else "",
    )
    return sha256("\x1f".join(fields).encode()).hexdigest()[:16]
