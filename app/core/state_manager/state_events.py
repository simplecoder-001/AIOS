# app/core/state_manager/state_events.py
"""
Domain events emitted by the AIOS State Manager.

These events are published through the EventBus whenever subsystem states
change, snapshots are created/restored, or the state manager itself starts
and stops.

Import order intentionally avoids cycles:

    constants/events
        ↓
    event_bus/event_types
        ↓
    state_manager/*
        ↓
    state_events

The module depends only on the Event envelope and constants. It does not
depend on StateMachine, StateRegistry, or StatePersistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from types import MappingProxyType
from uuid import uuid4

from app.core.constants import BaseState
from app.core.constants.events import (
    EventCategory,
    EventDeliveryMode,
    EventPriority,
)
from app.core.event_bus.event_types import Event

__all__ = [
    "StateEvent",
    "StateTransitionEvent",
    "StateRegisteredEvent",
    "StateUnregisteredEvent",
    "StateSnapshotCreatedEvent",
    "StateSnapshotRestoredEvent",
    "StateManagerStartedEvent",
    "StateManagerStoppedEvent",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class StateEvent:
    """
    Base event for all state-manager events.
    """

    system: str
    timestamp: datetime = field(
        default_factory=_utcnow
    )
    metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def to_event(
        self,
        event_type: str,
        *,
        priority: EventPriority = EventPriority.NORMAL,
        delivery_mode: EventDeliveryMode = (
            EventDeliveryMode.SYNC
        ),
    ) -> Event:
        """
        Convert this domain event into an EventBus envelope.
        """
        return Event(
            event_id=uuid4().hex,
            event_type=event_type,
            category=EventCategory.SYSTEM,
            priority=priority,
            delivery_mode=delivery_mode,
            timestamp=self.timestamp,
            payload=self.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "timestamp": (
                self.timestamp.isoformat()
            ),
            "metadata": dict(
                self.metadata
            ),
        }


# ----------------------------------------------------------------------
# Registration events
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class StateRegisteredEvent(StateEvent):
    """
    Emitted when a subsystem state is registered.
    """


@dataclass(slots=True, frozen=True)
class StateUnregisteredEvent(StateEvent):
    """
    Emitted when a subsystem state is removed.
    """


# ----------------------------------------------------------------------
# Transition events
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class StateTransitionEvent(StateEvent):
    """
    Emitted whenever a subsystem changes state.
    """

    previous_state: BaseState
    current_state: BaseState
    version: int

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()

        payload.update(
            {
                "previous_state": (
                    self.previous_state.value
                ),
                "current_state": (
                    self.current_state.value
                ),
                "version": self.version,
            }
        )

        return payload


# ----------------------------------------------------------------------
# Snapshot events
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class StateSnapshotCreatedEvent(
    StateEvent
):
    """
    Emitted after a snapshot is created.
    """

    snapshot_id: str


@dataclass(slots=True, frozen=True)
class StateSnapshotRestoredEvent(
    StateEvent
):
    """
    Emitted after a snapshot is restored.
    """

    snapshot_id: str


# ----------------------------------------------------------------------
# Lifecycle events
# ----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class StateManagerStartedEvent(
    StateEvent
):
    """
    Emitted when the State Manager starts.
    """


@dataclass(slots=True, frozen=True)
class StateManagerStoppedEvent(
    StateEvent
):
    """
    Emitted when the State Manager stops.
    """