"""Stable Telethon integration isolated behind application contracts."""

from .client import (
    AccountIdentity,
    NativeClient,
    PeerRegistry,
    TelethonClientAdapter,
    TelethonClientLifecycle,
    create_telethon_client,
    peer_dialog_id,
)
from .commands import IncomingCommandRouter, TelegramCommandRouter
from .event_source import TelethonEventSource, normalize_message
from .rate_limit import SlidingWindowRateLimiter, TelegramRequestLimiter

__all__ = [
    "AccountIdentity",
    "IncomingCommandRouter",
    "NativeClient",
    "PeerRegistry",
    "SlidingWindowRateLimiter",
    "TelegramCommandRouter",
    "TelegramRequestLimiter",
    "TelethonClientAdapter",
    "TelethonClientLifecycle",
    "TelethonEventSource",
    "create_telethon_client",
    "normalize_message",
    "peer_dialog_id",
]
