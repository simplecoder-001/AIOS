# app/core/event_bus/event_types.py
"""
Core event envelope for the AIOS Event Bus.
===========================================
The runtime *shape* of an event as it travels through the bus — publisher,
dispatcher, middleware, event store, and every subscriber exchange
:class:`Event` instances.

Separation of concerns
-----------------------
* Event *names* / categories / delivery modes live in
  ``app/core/constants/events.py`` (static catalog).
* Priority *behavior* (resolution, ordering, aging) lives in
  ``event_priority.py``; the :class:`EventPriority` level enum is re-exported
  from there and used here.
* Flow *propagation* metadata lives in ``event_context.py``; each event may
  embed the :class:`EventContext` describing the logical flow it belongs to.
* This module owns the *envelope*: identity, timing, payload, priority, and
  the link to its context.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from app.core.constants.events import EventCategory, EventDeliveryMode, default_priority
from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_context import EventContext

__all__ = [
    "EventStatus",
    "Event",
]


class EventStatus(str, Enum):
    """Lifecycle status of an event as it moves through the bus."""

    CREATED = "created"          # constructed, not yet published
    PUBLISHED = "published"      # accepted by the bus
    DISPATCHING = "dispatching"  # currently being delivered to subscribers
    HANDLED = "handled"          # all subscribers processed successfully
    FAILED = "failed"            # one or more handlers raised
    DROPPED = "dropped"          # filtered out / no subscribers / expired


@dataclass
class Event:
    """A single occurrence of a named event flowing through the Event Bus.

    Parameters
    ----------
    name:
        Canonical event string from ``constants/events.py`` (e.g.
        ``VoiceEvent.WAKE_TRIGGERED.value``). Subscribers match on this value.
    payload:
        Arbitrary structured data. Never place secrets/PII here.
    category:
        Top-level :class:`EventCategory` used for filtering and routing.
    source:
        Logical origin of the event (e.g. ``"fg1_voice_system"``).
    priority:
        Dispatch priority. When ``None`` it is auto-derived from the event
        name at construction so emergency/security events escalate correctly.
    delivery_mode:
        How the bus should deliver this event to subscribers.
    context:
        The :class:`EventContext` describing the logical flow. When omitted, a
        context is created so the event always carries correlation identity.

    Auto-populated
    --------------
    event_id : unique ``uuid4`` hex, assigned at creation.
    timestamp : Unix epoch seconds (``time.time()``) at creation.
    status : lifecycle status, starts at :attr:`EventStatus.CREATED`.
    """

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    category: EventCategory = EventCategory.SYSTEM
    source: Optional[str] = None
    priority: Optional[EventPriority] = None
    delivery_mode: EventDeliveryMode = EventDeliveryMode.ASYNC
    context: Optional[EventContext] = None

    # Auto-populated identity / timing / lifecycle.
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex, init=False)
    timestamp: float = field(default_factory=time.time, init=False)
    status: EventStatus = field(default=EventStatus.CREATED, init=False)

    def __post_init__(self) -> None:
        # Derive priority from the catalog when not explicit.
        if self.priority is None:
            self.priority = default_priority(self.name)
        # Ensure every event carries a flow context.
        if self.context is None:
            self.context = EventContext(source=self.source)
        # Keep source consistent between envelope and context.
        elif self.source is None:
            self.source = self.context.source

    # --------------------------------------------------------------- helpers
    @property
    def correlation_id(self) -> str:
        """Correlation id of the flow this event belongs to."""
        assert self.context is not None  # guaranteed by __post_init__
        return self.context.correlation_id

    @property
    def age_seconds(self) -> float:
        """Seconds elapsed since the event was created (used by aging policy)."""
        return max(0.0, time.time() - self.timestamp)

    @property
    def is_emergency(self) -> bool:
        """True if this event dispatches at EMERGENCY priority."""
        return self.priority is EventPriority.EMERGENCY

    def mark(self, status: EventStatus) -> "Event":
        """Advance the lifecycle status (fluent, chainable)."""
        self.status = status
        return self

    def with_payload(self, **kwargs: Any) -> "Event":
        """Merge additional keys into the payload and return self."""
        self.payload.update(kwargs)
        return self

    def caused(self, name: str, **payload: Any) -> "Event":
        """Create a child event caused by this one, within the same flow.

        The child inherits a derived :class:`EventContext` (same
        ``correlation_id``) whose ``causation_id`` points at this event's id.
        """
        assert self.context is not None
        child_ctx = self.context.child(causation_id=self.event_id, source=self.source)
        return Event(
            name=name,
            payload=dict(payload),
            source=self.source,
            context=child_ctx,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for the event store, audit log, telemetry."""
        return {
            "event_id": self.event_id,
            "name": self.name,
            "category": self.category.value,
            "source": self.source,
            "priority": int(self.priority) if self.priority is not None else None,
            "delivery_mode": self.delivery_mode.value,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "context": self.context.to_dict() if self.context is not None else None,
            "payload": self.payload,
        }

    def __str__(self) -> str:
        return f"[{self.name}] id={self.event_id[:8]} status={self.status.value}"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Event(name={self.name!r}, id={self.event_id!r}, "
            f"category={self.category.value!r}, priority={self.priority!r}, "
            f"status={self.status.value!r})"
        )
