# app/core/event_bus/events/lifecycle_events.py

"""
Lifecycle events for AIOS.

These events drive the application's startup, initialization,
feature-group orchestration, service lifecycle management, and
graceful shutdown.

Primary consumers:
    - bootstrap/startup.py
    - bootstrap/shutdown.py
    - bootstrap/initializer.py
    - bootstrap/lifecycle_manager.py
    - architecture/runtime_manager.py
    - architecture/module_registry.py
    - dependency_injection/container.py
    - feature groups

Design goals:
    - Immutable event objects
    - Thread-safe dataclass events
    - Correlation support
    - Structured payloads
    - Event sourcing friendly
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class LifecycleEventType(StrEnum):
    """
    Canonical lifecycle event names.
    """

    BOOTSTRAP_STARTED = "lifecycle.bootstrap.started"
    BOOTSTRAP_COMPLETED = "lifecycle.bootstrap.completed"

    INITIALIZATION_STARTED = "lifecycle.initialization.started"
    INITIALIZATION_COMPLETED = "lifecycle.initialization.completed"

    SERVICE_STARTING = "lifecycle.service.starting"
    SERVICE_STARTED = "lifecycle.service.started"

    SERVICE_STOPPING = "lifecycle.service.stopping"
    SERVICE_STOPPED = "lifecycle.service.stopped"

    FEATURE_GROUP_LOADING = "lifecycle.feature_group.loading"
    FEATURE_GROUP_LOADED = "lifecycle.feature_group.loaded"

    FEATURE_GROUP_UNLOADING = "lifecycle.feature_group.unloading"
    FEATURE_GROUP_UNLOADED = "lifecycle.feature_group.unloaded"

    SHUTDOWN_STARTED = "lifecycle.shutdown.started"
    SHUTDOWN_COMPLETED = "lifecycle.shutdown.completed"

    RESTART_REQUESTED = "lifecycle.restart.requested"

    LIFECYCLE_FAILED = "lifecycle.failed"


@dataclass(slots=True, kw_only=True)
class LifecycleEvent(Event):
    """
    Base lifecycle event.

    Shared metadata for all lifecycle events.
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.LIFECYCLE,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None


# ============================================================================
# Bootstrap Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class BootstrapStartedEvent(LifecycleEvent):
    name: str = field(
        default=LifecycleEventType.BOOTSTRAP_STARTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class BootstrapCompletedEvent(LifecycleEvent):
    name: str = field(
        default=LifecycleEventType.BOOTSTRAP_COMPLETED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    duration_seconds: float | None = None


# ============================================================================
# Initialization Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class InitializationStartedEvent(LifecycleEvent):
    name: str = field(
        default=LifecycleEventType.INITIALIZATION_STARTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class InitializationCompletedEvent(LifecycleEvent):
    name: str = field(
        default=LifecycleEventType.INITIALIZATION_COMPLETED,
        init=False,
    )

    duration_seconds: float | None = None


# ============================================================================
# Service Lifecycle Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ServiceStartingEvent(LifecycleEvent):
    """
    Fired before a service starts.
    """

    name: str = field(
        default=LifecycleEventType.SERVICE_STARTING,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    service_name: str
    service_type: str | None = None


@dataclass(slots=True, kw_only=True)
class ServiceStartedEvent(LifecycleEvent):
    """
    Fired after a service successfully starts.
    """

    name: str = field(
        default=LifecycleEventType.SERVICE_STARTED,
        init=False,
    )

    service_name: str
    startup_time_seconds: float | None = None


@dataclass(slots=True, kw_only=True)
class ServiceStoppingEvent(LifecycleEvent):
    """
    Fired before service shutdown.
    """

    name: str = field(
        default=LifecycleEventType.SERVICE_STOPPING,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    service_name: str


@dataclass(slots=True, kw_only=True)
class ServiceStoppedEvent(LifecycleEvent):
    """
    Fired after service shutdown.
    """

    name: str = field(
        default=LifecycleEventType.SERVICE_STOPPED,
        init=False,
    )

    service_name: str
    shutdown_time_seconds: float | None = None


# ============================================================================
# Feature Group Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class FeatureGroupLoadingEvent(LifecycleEvent):
    """
    Fired before a feature group is initialized.
    """

    name: str = field(
        default=LifecycleEventType.FEATURE_GROUP_LOADING,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    feature_group: str


@dataclass(slots=True, kw_only=True)
class FeatureGroupLoadedEvent(LifecycleEvent):
    """
    Fired after a feature group becomes available.
    """

    name: str = field(
        default=LifecycleEventType.FEATURE_GROUP_LOADED,
        init=False,
    )

    feature_group: str
    load_time_seconds: float | None = None


@dataclass(slots=True, kw_only=True)
class FeatureGroupUnloadingEvent(LifecycleEvent):
    """
    Fired before a feature group unloads.
    """

    name: str = field(
        default=LifecycleEventType.FEATURE_GROUP_UNLOADING,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    feature_group: str


@dataclass(slots=True, kw_only=True)
class FeatureGroupUnloadedEvent(LifecycleEvent):
    """
    Fired after a feature group unloads.
    """

    name: str = field(
        default=LifecycleEventType.FEATURE_GROUP_UNLOADED,
        init=False,
    )

    feature_group: str


# ============================================================================
# Shutdown / Restart Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ShutdownStartedEvent(LifecycleEvent):
    name: str = field(
        default=LifecycleEventType.SHUTDOWN_STARTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class ShutdownCompletedEvent(LifecycleEvent):
    name: str = field(
        default=LifecycleEventType.SHUTDOWN_COMPLETED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    duration_seconds: float | None = None


@dataclass(slots=True, kw_only=True)
class RestartRequestedEvent(LifecycleEvent):
    """
    Request a controlled application restart.
    """

    name: str = field(
        default=LifecycleEventType.RESTART_REQUESTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    reason: str | None = None


# ============================================================================
# Failure Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class LifecycleFailedEvent(LifecycleEvent):
    """
    Emitted whenever any lifecycle stage fails.
    """

    name: str = field(
        default=LifecycleEventType.LIFECYCLE_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    stage: str
    message: str
    exception_type: str | None = None
    recoverable: bool = True


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "LifecycleEventType",
    "LifecycleEvent",
    "BootstrapStartedEvent",
    "BootstrapCompletedEvent",
    "InitializationStartedEvent",
    "InitializationCompletedEvent",
    "ServiceStartingEvent",
    "ServiceStartedEvent",
    "ServiceStoppingEvent",
    "ServiceStoppedEvent",
    "FeatureGroupLoadingEvent",
    "FeatureGroupLoadedEvent",
    "FeatureGroupUnloadingEvent",
    "FeatureGroupUnloadedEvent",
    "ShutdownStartedEvent",
    "ShutdownCompletedEvent",
    "RestartRequestedEvent",
    "LifecycleFailedEvent",
]