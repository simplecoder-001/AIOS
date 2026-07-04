# app/core/event_bus/event_context.py
"""
Ambient propagation context for the AIOS Event Bus.
===================================================
An :class:`EventContext` carries the *cross-cutting* metadata that should
travel with a logical flow — a voice turn, an AI-brain request, a task
execution — as it fans out across publishers, the dispatcher, async handlers,
and worker threads.

Where the :class:`~app.core.event_bus.event_types.Event` envelope holds *what
happened*, the context holds *the flow it belongs to*: correlation and
causation identity, the originating source/actor, and free-form trace baggage.

Propagation model
-----------------
The active context is stored in a :class:`contextvars.ContextVar`. This is the
correct primitive for AIOS because it propagates automatically across
``await`` boundaries in the asyncio pipeline *and* is isolated per worker
thread, matching the multi-threaded voice/brain architecture. Publishers read
the current context to stamp new events; the dispatcher activates a context for
the duration of handler execution so nested publishes inherit the same flow.
"""

from __future__ import annotations

import contextvars
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

__all__ = [
    "EventContext",
    "current_context",
    "set_current_context",
    "use_context",
    "get_or_create_context",
]


# The single ambient slot. ``None`` means "no active flow"; callers that need a
# context should use :func:`get_or_create_context` to lazily start one.
_CURRENT: contextvars.ContextVar[Optional["EventContext"]] = contextvars.ContextVar(
    "aios_event_context", default=None
)


@dataclass
class EventContext:
    """Cross-cutting metadata describing the logical flow an event belongs to.

    Parameters
    ----------
    correlation_id:
        Groups every event in one logical flow. Auto-generated when omitted so
        a context always identifies a flow.
    causation_id:
        The ``event_id`` of the event that directly caused the current step,
        enabling precise cause/effect reconstruction in the event store.
    source:
        Logical origin of the flow (e.g. ``"fg1_voice_system"``).
    actor:
        The authenticated principal driving the flow (e.g. a speaker/role id),
        used for audit correlation. Never store secrets/PII here.
    baggage:
        Free-form propagated key/values (trace ids, session id, language, etc.).
        Carried across the whole flow but never interpreted by the bus itself.

    Auto-populated
    --------------
    context_id:
        Unique identity for this context instance (``uuid4`` hex).
    created_at:
        Unix epoch seconds captured at construction.
    """

    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    causation_id: Optional[str] = None
    source: Optional[str] = None
    actor: Optional[str] = None
    baggage: Dict[str, Any] = field(default_factory=dict)

    context_id: str = field(default_factory=lambda: uuid.uuid4().hex, init=False)
    created_at: float = field(default_factory=time.time, init=False)

    # ------------------------------------------------------------------ API
    def child(
        self,
        *,
        causation_id: Optional[str] = None,
        source: Optional[str] = None,
        actor: Optional[str] = None,
        **extra_baggage: Any,
    ) -> "EventContext":
        """Derive a nested context that stays within the same flow.

        The child keeps the parent's ``correlation_id`` (same flow) but gets a
        fresh ``context_id``, may advance ``causation_id`` to the triggering
        event, and inherits — then extends — the parent's baggage.
        """
        merged = dict(self.baggage)
        merged.update(extra_baggage)
        return EventContext(
            correlation_id=self.correlation_id,
            causation_id=causation_id if causation_id is not None else self.causation_id,
            source=source if source is not None else self.source,
            actor=actor if actor is not None else self.actor,
            baggage=merged,
        )

    def with_baggage(self, **kwargs: Any) -> "EventContext":
        """Merge extra baggage into this context and return self (fluent)."""
        self.baggage.update(kwargs)
        return self

    def get(self, key: str, default: Any = None) -> Any:
        """Read a single baggage value."""
        return self.baggage.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the event store, audit log, and telemetry."""
        return {
            "context_id": self.context_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "source": self.source,
            "actor": self.actor,
            "created_at": self.created_at,
            "baggage": dict(self.baggage),
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<EventContext corr={self.correlation_id[:8]} "
            f"ctx={self.context_id[:8]} source={self.source!r}>"
        )


# --------------------------------------------------------------------------- #
# Ambient accessors
# --------------------------------------------------------------------------- #
def current_context() -> Optional[EventContext]:
    """Return the active :class:`EventContext`, or ``None`` if none is set."""
    return _CURRENT.get()


def set_current_context(context: Optional[EventContext]) -> contextvars.Token:
    """Set the active context, returning a token to restore the prior value.

    Prefer :func:`use_context` for scoped activation; use this directly only
    when manual token management is required.
    """
    return _CURRENT.set(context)


def get_or_create_context(**kwargs: Any) -> EventContext:
    """Return the active context, creating and activating one if absent.

    Any keyword arguments are forwarded to :class:`EventContext` only when a
    new context is created; an already-active context is returned unchanged.
    """
    ctx = _CURRENT.get()
    if ctx is None:
        ctx = EventContext(**kwargs)
        _CURRENT.set(ctx)
    return ctx


@contextmanager
def use_context(context: EventContext) -> Iterator[EventContext]:
    """Activate ``context`` for the duration of a ``with`` block.

        with use_context(ctx):
            bus.publish(Event(...))   # inherits ctx's correlation/causation

    The previous context (if any) is restored on exit, so nested flows and
    concurrent tasks never leak context into one another.
    """
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
