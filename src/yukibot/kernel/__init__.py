"""Telegram-independent application kernel."""

from .application import Application
from .errors import (
    DuplicateFeatureError,
    KernelError,
    LifecycleStartError,
    LifecycleStopError,
    SupervisorClosedError,
)
from .event_bus import (
    DispatchFailure,
    DispatchReport,
    EventBus,
    InProcessEventBus,
    Subscription,
)
from .feature import Feature
from .lifecycle import LifecycleManager, LifecycleState
from .shutdown import ShutdownCoordinator
from .supervisor import SupervisorLifecycle, TaskFailure, TaskSupervisor

__all__ = [
    "Application",
    "DispatchFailure",
    "DispatchReport",
    "DuplicateFeatureError",
    "EventBus",
    "Feature",
    "InProcessEventBus",
    "KernelError",
    "LifecycleManager",
    "LifecycleStartError",
    "LifecycleState",
    "LifecycleStopError",
    "ShutdownCoordinator",
    "Subscription",
    "SupervisorClosedError",
    "SupervisorLifecycle",
    "TaskFailure",
    "TaskSupervisor",
]
