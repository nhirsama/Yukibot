"""Reusable, Telegram-SDK-independent forwarding feature."""

from .album import AlbumBuffer
from .errors import (
    DeliveryResultMismatch,
    ForwarderError,
    MessageNotFound,
    MessageNotModified,
    NativeForwardUnsupported,
    PermanentDeliveryError,
    RetryAfter,
    RouteCycleError,
)
from .forwarder import Forwarder
from .memory import InMemoryMessageLinkRepository, InMemoryRouteRepository
from .models import (
    ContentType,
    DestinationEndpoint,
    ForwardMode,
    IncomingMessage,
    MessageFilter,
    MessageLink,
    MessageRef,
    MessagesDeleted,
    Route,
    ServiceKind,
    ServiceMessage,
    SourceEndpoint,
    normalize_general_topic,
)
from .ports import MessageLinkRepository, RouteRepository, TelegramGateway
from .rate_limit import SlidingWindowRateLimiter
from .routing import assert_acyclic_routes
from .service import (
    DeliveryFailure,
    DeliveryOutcome,
    ForwarderOptions,
    ForwarderService,
    ForwardingReport,
    SyncFailure,
    SyncOperation,
    SyncReport,
    format_service_message,
)

__all__ = [
    "AlbumBuffer",
    "ContentType",
    "DeliveryFailure",
    "DeliveryOutcome",
    "DeliveryResultMismatch",
    "DestinationEndpoint",
    "ForwardMode",
    "Forwarder",
    "ForwarderError",
    "ForwarderOptions",
    "ForwarderService",
    "ForwardingReport",
    "InMemoryMessageLinkRepository",
    "InMemoryRouteRepository",
    "IncomingMessage",
    "MessageFilter",
    "MessageLink",
    "MessageLinkRepository",
    "MessageNotFound",
    "MessageNotModified",
    "MessageRef",
    "MessagesDeleted",
    "NativeForwardUnsupported",
    "PermanentDeliveryError",
    "RetryAfter",
    "Route",
    "RouteCycleError",
    "RouteRepository",
    "ServiceKind",
    "ServiceMessage",
    "SlidingWindowRateLimiter",
    "SourceEndpoint",
    "SyncFailure",
    "SyncOperation",
    "SyncReport",
    "TelegramGateway",
    "assert_acyclic_routes",
    "format_service_message",
    "normalize_general_topic",
]
