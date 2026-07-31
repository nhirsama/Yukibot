"""Telethon v2 integration isolated behind stable application contracts."""

from .client import (
    NativeClient,
    PeerRegistry,
    TelethonClientLifecycle,
    create_telethon_client,
    peer_dialog_id,
)
from .event_source import TelethonEventSource, normalize_message
from .gateway import TelethonGateway

__all__ = [
    "NativeClient",
    "PeerRegistry",
    "TelethonClientLifecycle",
    "TelethonEventSource",
    "TelethonGateway",
    "create_telethon_client",
    "normalize_message",
    "peer_dialog_id",
]
