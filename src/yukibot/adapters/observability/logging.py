"""Minimal structured JSON logging based on the standard library."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

_STANDARD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_SENSITIVE_FRAGMENTS = ("api_hash", "password", "secret", "session", "token")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_FIELDS or key.startswith("_"):
                continue
            payload[key] = "[redacted]" if _is_sensitive(key) else value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str, *, stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def _is_sensitive(key: str) -> bool:
    normalized = key.casefold()
    return any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)
