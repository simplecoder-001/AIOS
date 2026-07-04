# app/core/event_bus/event_serializer.py
"""
Event (de)serialization for the AIOS Event Bus.
===============================================
Converts :class:`~app.core.event_bus.event_types.Event` instances to and from
JSON so events can be persisted by the event store, written to audit logs, and
transported across queues or process boundaries.

Round-trip fidelity
-------------------
The envelope and its :class:`EventContext` carry auto-generated fields
(``event_id``, ``timestamp``, ``status``, ``context_id``, ``created_at``) that
are declared ``init=False``. A naive reconstruction would mint *new* identity
and timing, breaking correlation and audit ordering. This serializer therefore
rebuilds the object graph and restores those fields explicitly, so a
deserialized event is identical to the original — same id, same timestamp,
same lifecycle status.

Enums (:class:`EventCategory`, :class:`EventDeliveryMode`, :class:`EventPriority`,
:class:`EventStatus`) are stored by their primitive value and coerced back to
the correct type on load.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, Optional

from app.core.constants.events import EventCategory, EventDeliveryMode
from app.core.event_bus.event_context import EventContext
from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventStatus
from app.core.exceptions import EventSerializationError

__all__ = ["EventSerializer"]


class EventSerializer:
    """Stateless codec between :class:`Event` and JSON / plain dicts.

    All methods are class methods; the serializer holds no state and is safe to
    share across threads. ``to_dict`` / ``from_dict`` handle the structural
    mapping; ``serialize`` / ``deserialize`` add the JSON string layer.
    """

    # ------------------------------------------------------------ to formats
    @classmethod
    def to_dict(cls, event: Event) -> Dict[str, Any]:
        """Return a JSON-safe dict for ``event``.

        Delegates to :meth:`Event.to_dict`, which already emits primitive
        values (enum ``.value``, nested context dict) in a stable shape.
        """
        try:
            return event.to_dict()
        except Exception as exc:  # noqa: BLE001 - uniform error routing
            raise EventSerializationError(
                f"Failed to serialize event {event.name!r} to dict",
                cause=exc,
            ) from exc

    @classmethod
    def serialize(cls, event: Event, *, indent: Optional[int] = None) -> str:
        """Serialize ``event`` to a JSON string."""
        try:
            return json.dumps(cls.to_dict(event), ensure_ascii=False, indent=indent)
        except (TypeError, ValueError) as exc:
            raise EventSerializationError(
                f"Failed to JSON-encode event {event.name!r}",
                cause=exc,
            ) from exc

    @classmethod
    def to_bytes(cls, event: Event) -> bytes:
        """Serialize to compact UTF-8 bytes for queue/store transport."""
        return cls.serialize(event).encode("utf-8")

    # ---------------------------------------------------------- from formats
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Event:
        """Reconstruct an :class:`Event` from a dict produced by :meth:`to_dict`.

        Auto-generated identity/timing/status fields are restored (not
        regenerated) to preserve correlation and audit ordering.
        """
        if not isinstance(data, dict):
            raise EventSerializationError(
                f"Cannot deserialize event from {type(data).__name__}; dict required"
            )
        if "name" not in data:
            raise EventSerializationError("Event payload missing required key 'name'")

        try:
            context = cls._context_from_dict(data.get("context"))

            priority_raw = data.get("priority")
            priority = EventPriority(priority_raw) if priority_raw is not None else None

            event = Event(
                name=data["name"],
                payload=dict(data.get("payload") or {}),
                category=EventCategory(data.get("category", EventCategory.SYSTEM.value)),
                source=data.get("source"),
                priority=priority,
                delivery_mode=EventDeliveryMode(
                    data.get("delivery_mode", EventDeliveryMode.ASYNC.value)
                ),
                context=context,
            )

            # Restore init=False fields so identity/timing survive the round trip.
            if data.get("event_id"):
                object.__setattr__(event, "event_id", data["event_id"])
            if data.get("timestamp") is not None:
                object.__setattr__(event, "timestamp", float(data["timestamp"]))
            if data.get("status"):
                object.__setattr__(event, "status", EventStatus(data["status"]))

            return event
        except EventSerializationError:
            raise
        except Exception as exc:  # noqa: BLE001 - uniform error routing
            raise EventSerializationError(
                f"Failed to reconstruct event from dict (name={data.get('name')!r})",
                cause=exc,
            ) from exc

    @classmethod
    def deserialize(cls, raw: str | bytes) -> Event:
        """Reconstruct an :class:`Event` from a JSON string or UTF-8 bytes."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise EventSerializationError(
                "Failed to JSON-decode event payload",
                cause=exc,
            ) from exc
        return cls.from_dict(data)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _context_from_dict(raw: Optional[Dict[str, Any]]) -> Optional[EventContext]:
        """Rebuild an :class:`EventContext`, restoring its init=False fields."""
        if raw is None:
            return None
        context = EventContext(
            correlation_id=raw.get("correlation_id") or "",
            causation_id=raw.get("causation_id"),
            source=raw.get("source"),
            actor=raw.get("actor"),
            baggage=dict(raw.get("baggage") or {}),
        )
        if raw.get("context_id"):
            object.__setattr__(context, "context_id", raw["context_id"])
        if raw.get("created_at") is not None:
            object.__setattr__(context, "created_at", float(raw["created_at"]))
        return context
