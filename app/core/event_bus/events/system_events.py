# app/core/event_bus/events/system_events.py

"""
System-level events for the AIOS Event Bus.

These events represent application-wide state changes and infrastructure
notifications that can be consumed by bootstrap, lifecycle management,
telemetry, logging, GUI, and feature groups.

Dependencies:
    - app.core.event_bus.event_types
    - app.core.event_bus.event_priority
    - app.core.constants
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class SystemEventType(StrEnum):
    """
    Canonical system event names.

    String enums serialize cleanly into logs, telemetry,
    SQLite, JSON, and event stores.
    """

    APPLICATION_STARTING = "system.application.starting"
    APPLICATION_STARTED = "system.application.started"

    APPLICATION_STOPPING = "system.application.stopping"
    APPLICATION_STOPPED = "system.application.stopped"

    SYSTEM_READY = "system.ready"

    HEALTH_DEGRADED = "system.health.degraded"
    HEALTH_RECOVERED = "system.health.recovered"

    CONFIG_RELOADED = "system.config.reloaded"

    RESOURCE_PRESSURE = "system.resource.pressure"

    ERROR = "system.error"
    FATAL_ERROR = "system.fatal_error"


@dataclass(slots=True, kw_only=True)
class SystemEvent(Event):
    """
    Base event for all system-level notifications.

    Provides:
        - globally unique event id
        - UTC timestamp
        - event category
        - priority
        - correlation metadata
        - arbitrary payload
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.SYSTEM,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None


# ---------------------------------------------------------------------
# Startup / Shutdown Events
# ---------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class ApplicationStartingEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.APPLICATION_STARTING,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class ApplicationStartedEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.APPLICATION_STARTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class ApplicationStoppingEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.APPLICATION_STOPPING,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class ApplicationStoppedEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.APPLICATION_STOPPED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )


# ---------------------------------------------------------------------
# Health Events
# ---------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class SystemReadyEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.SYSTEM_READY,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class HealthDegradedEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.HEALTH_DEGRADED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    subsystem: str
    reason: str


@dataclass(slots=True, kw_only=True)
class HealthRecoveredEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.HEALTH_RECOVERED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.NORMAL,
        init=False,
    )

    subsystem: str


# ---------------------------------------------------------------------
# Configuration Events
# ---------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class ConfigReloadedEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.CONFIG_RELOADED,
        init=False,
    )

    changed_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Resource Events
# ---------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class ResourcePressureEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.RESOURCE_PRESSURE,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    cpu_percent: float
    memory_percent: float
    gpu_memory_percent: float | None = None


# ---------------------------------------------------------------------
# Error Events
# ---------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class SystemErrorEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.ERROR,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    exception_type: str
    message: str
    subsystem: str


@dataclass(slots=True, kw_only=True)
class FatalSystemErrorEvent(SystemEvent):
    name: str = field(
        default=SystemEventType.FATAL_ERROR,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    exception_type: str
    message: str
    subsystem: str


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

__all__ = [
    "SystemEventType",
    "SystemEvent",
    "ApplicationStartingEvent",
    "ApplicationStartedEvent",
    "ApplicationStoppingEvent",
    "ApplicationStoppedEvent",
    "SystemReadyEvent",
    "HealthDegradedEvent",
    "HealthRecoveredEvent",
    "ConfigReloadedEvent",
    "ResourcePressureEvent",
    "SystemErrorEvent",
    "FatalSystemErrorEvent",
]