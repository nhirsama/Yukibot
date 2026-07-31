"""Stable contracts shared by the application core and its adapters."""

from .database import (
    Database,
    DatabaseConnection,
    DatabaseError,
    ExecuteResult,
    IntegrityViolation,
    Migration,
    Row,
    SqlParameters,
    SqlValue,
)
from .events import TelegramMessageEdited, TelegramMessageReceived, TelegramMessagesDeleted
from .telegram import (
    MessageRef,
    TelegramContentType,
    TelegramMessage,
    TelegramServiceKind,
    TelegramServiceMessage,
)

__all__ = [
    "Database",
    "DatabaseConnection",
    "DatabaseError",
    "ExecuteResult",
    "IntegrityViolation",
    "MessageRef",
    "Migration",
    "Row",
    "SqlParameters",
    "SqlValue",
    "TelegramContentType",
    "TelegramMessage",
    "TelegramMessageEdited",
    "TelegramMessageReceived",
    "TelegramMessagesDeleted",
    "TelegramServiceKind",
    "TelegramServiceMessage",
]
