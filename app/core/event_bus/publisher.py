# app/core/event_bus/publisher.py
"""
Producer-facing publishing facade for the AIOS Event Bus.
=========================================================
Where subscribers *consume* events, a :class:`Publisher` is the ergonomic
surface producers use to *emit* them. It shields feature groups from envelope
construction details: a caller supplies an event name (and optional payload),
and the publisher stamps the source, attaches the active flow context, resolves
the delivery mode, and forwards the finished :class:`Event` to the bus.

Why a dedicated publisher?
--------------------------
* **Consistent provenance** — every event from a subsystem carries the same
  ``source`` label without repeating it at each call site.
* **Automatic flow correlation** — the ambient :class:`EventContext` is bound
  onto emitted events, so causal chains form without manual id plumbing.
* **One integration point** — the bus injects a single ``sink`` callable; the
  publisher never imports the bus, keeping the dependency direction clean and
  the module import-safe.

A :class:`ScopedPublisher` binds a fixed source so feature groups can hold a
pre-configured emitter (e.g. ``self._events = bus.publisher("fg1_voice_system")``).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from app.core.constants.events import EventCategory, EventDeliveryMode
from app.core.event_bus.event_context import EventContext, current_context
from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event
from app.core.exceptions import EventPublishError
from app.logging import Logger

__all__ = ["Publisher", "ScopedPublisher"]

# The bus supplies this: it accepts a fully-formed Event and returns the event
# it accepted (or None if the pipeline dropped it).
EventSink = Callable[[Event], Optional[Event]]


class Publisher:
    """Constructs events and forwards them to the bus sink.

    Parameters
    ----------
    sink:
        Callable injected by the bus that accepts an :class:`Event` and runs it
        through middleware + dispatch, returning the accepted event or ``None``
        if it was dropped.
    default_source:
        Source label applied when a publish call does not override it.
    logger:
        Optional logger for publish diagnostics.
    """

    def __init__(
        self,
        sink: EventSink,
        *,
        default_source: Optional[str] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        if not callable(sink):
            raise TypeError("Publisher sink must be callable")
        self._sink = sink
        self._default_source = default_source
        self._logger = logger

    # ------------------------------------------------------------- emit APIs
    def emit(
        self,
        name: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        category: EventCategory = EventCategory.SYSTEM,
        source: Optional[str] = None,
        priority: Optional[EventPriority] = None,
        delivery_mode: EventDeliveryMode = EventDeliveryMode.ASYNC,
        context: Optional[EventContext] = None,
        **payload_kwargs: Any,
    ) -> Optional[Event]:
        """Build and publish an event, returning the accepted event or ``None``.

        Payload may be supplied as a dict, as keyword arguments, or both (the
        keyword arguments take precedence on key conflicts).
        """
        merged: Dict[str, Any] = dict(payload or {})
        merged.update(payload_kwargs)

        event = Event(
            name=name,
            payload=merged,
            category=category,
            source=source or self._default_source,
            priority=priority,
            delivery_mode=delivery_mode,
            context=context or current_context(),
        )
        return self.publish(event)

    def publish(self, event: Event) -> Optional[Event]:
        """Publish an already-constructed :class:`Event` through the sink.

        Applies the default source when the event has none, then forwards to
        the bus. Any sink failure is wrapped as :class:`EventPublishError`.
        """
        if event.source is None and self._default_source is not None:
            event.source = self._default_source
            if event.context is not None and event.context.source is None:
                event.context.source = self._default_source

        try:
            accepted = self._sink(event)
        except Exception as exc:  # noqa: BLE001 - uniform error routing
            raise EventPublishError(
                f"Failed to publish event {event.name!r}",
                cause=exc,
            ) from exc

        if accepted is None and self._logger:
            self._logger.debug(
                "Event dropped by bus pipeline",
                extra={"event": event.name, "event_id": event.event_id},
            )
        return accepted

    def emit_from(self, parent: Event, name: str, **payload: Any) -> Optional[Event]:
        """Publish an event caused by ``parent``, preserving the flow chain.

        Delegates to :meth:`Event.caused` so the new event inherits the parent's
        correlation id and records the parent's id as its causation id.
        """
        child = parent.caused(name, **payload)
        return self.publish(child)

    # ---------------------------------------------------------------- scoping
    def scoped(self, source: str) -> "ScopedPublisher":
        """Return a publisher permanently bound to ``source``."""
        return ScopedPublisher(self._sink, source=source, logger=self._logger)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Publisher source={self._default_source!r}>"


class ScopedPublisher(Publisher):
    """A :class:`Publisher` bound to a fixed, non-overridable source.

    Feature groups hold one of these so every event they emit is stamped with
    their identity, e.g.::

        self._events = bus.publisher("fg1_voice_system")
        self._events.emit(VoiceEvent.WAKE_TRIGGERED.value)
    """

    def __init__(
        self,
        sink: EventSink,
        *,
        source: str,
        logger: Optional[Logger] = None,
    ) -> None:
        super().__init__(sink, default_source=source, logger=logger)
        self._source = source

    @property
    def source(self) -> str:
        return self._source

    def emit(self, name: str, payload: Optional[Dict[str, Any]] = None, **kwargs: Any):  # type: ignore[override]
        """Emit with the bound source enforced (any ``source`` kwarg ignored)."""
        kwargs.pop("source", None)
        return super().emit(name, payload, source=self._source, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<ScopedPublisher source={self._source!r}>"
