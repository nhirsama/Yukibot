"""SDK-independent errors understood by the forwarding module."""


class ForwarderError(Exception):
    """Base class for errors exposed by a Telegram adapter."""


class NativeForwardUnsupported(ForwarderError):
    """The source message cannot be sent using Telegram native forwarding."""


class MessageNotModified(ForwarderError):
    """The destination already has the requested content."""


class MessageNotFound(ForwarderError):
    """The referenced source or destination message no longer exists."""


class PermanentDeliveryError(ForwarderError):
    """The operation cannot succeed without a configuration or permission change."""


class DeliveryResultMismatch(ForwarderError):
    """Telegram returned a different number of messages than the module sent."""


class PartialDeliveryState(ForwarderError):
    """Only part of an album has a persisted destination mapping."""


class RouteCycleError(ForwarderError, ValueError):
    """Enabled routes contain a forwarding cycle."""


class RouteNotFoundError(ForwarderError, KeyError):
    """The requested forwarding route does not exist."""


class RetryAfter(ForwarderError):
    """The operation may be retried after the requested delay."""

    def __init__(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        self.seconds = seconds
        super().__init__(f"retry after {seconds:g} seconds")
