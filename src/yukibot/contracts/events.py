"""Immutable integration events published inside the process."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .telegram import TelegramMessage


@dataclass(frozen=True, slots=True)
class TelegramMessageReceived:
    message: TelegramMessage


@dataclass(frozen=True, slots=True)
class TelegramMessageEdited:
    message: TelegramMessage


@dataclass(frozen=True, slots=True)
class TelegramMessagesDeleted:
    message_ids: tuple[int, ...]
    occurred_at: datetime
    chat_id: int | None = None

    def __post_init__(self) -> None:
        if not self.message_ids:
            raise ValueError("message_ids must not be empty")
        if any(message_id <= 0 for message_id in self.message_ids):
            raise ValueError("message_ids must be positive")
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
