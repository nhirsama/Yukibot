"""Errors raised by application kernel mechanisms."""

from __future__ import annotations

from collections.abc import Sequence


class KernelError(Exception):
    """Base class for kernel failures."""


class DuplicateFeatureError(KernelError, ValueError):
    """Two lifecycle components use the same name."""


class SupervisorClosedError(KernelError, RuntimeError):
    """A task was submitted after supervisor shutdown began."""


class LifecycleStartError(KernelError):
    def __init__(self, feature_name: str, cause: BaseException) -> None:
        self.feature_name = feature_name
        self.cause = cause
        super().__init__(f"failed to start feature {feature_name!r}: {cause}")


class LifecycleStopError(KernelError):
    def __init__(self, failures: Sequence[tuple[str, BaseException]]) -> None:
        self.failures = tuple(failures)
        names = ", ".join(name for name, _ in failures)
        super().__init__(f"failed to stop features: {names}")
