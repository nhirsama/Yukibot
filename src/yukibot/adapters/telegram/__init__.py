"""Telethon v2 integration isolated behind stable application contracts."""

from .client import (
    NativeClient,
    PeerRegistry,
    TelethonClientLifecycle,
    create_telethon_client,
    peer_dialog_id,
)
from .event_source import TelethonEventSource, normalize_message
from .rate_limit import SlidingWindowRateLimiter, TelegramRequestLimiter

__all__ = [
    "NativeClient",
    "PeerRegistry",
    "SlidingWindowRateLimiter",
    "TelegramRequestLimiter",
    "TelethonClientLifecycle",
    "TelethonEventSource",
    "create_telethon_client",
    "normalize_message",
    "peer_dialog_id",
]
