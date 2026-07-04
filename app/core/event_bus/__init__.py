# app/core/event_bus/__init__.py
"""
AIOS Event Bus package — public API.
====================================
The backbone of the event-driven architecture. Every subsystem communicates by
publishing and subscribing to :class:`Event` objects through a single, shared,
thread-safe :class:`EventBus` rather than calling one another directly.

Usage
-----
Import from the package root, not the submodules:

    from app.core.event_bus import (
        EventBus,
        Event,
        EventFilter,
        NameFilter,
        register_event_bus,
    )

    # Wire a shared bus into the DI container (bootstrap).
    bus = register_event_bus(container, strict=True)

    # Producer side — a source-bound publisher per feature group.
    events = bus.publisher("fg1_voice_system")
    events.emit(VoiceEvent.WAKE_TRIGGERED.value)

    # Consumer side — subscribe a handler with a filter.
    @bus.on(VoiceEvent.TRANSCRIPTION_FINAL.value)
    def on_final(event: Event) -> None:
        ...

Architecture
------------
* ``event_types``      — the :class:`Event` envelope + :class:`EventStatus`.
* ``event_priority``   — priority resolution, ordering, aging.
* ``event_context``    — ambient flow-correlation context.
* ``event_filter``     — composable subscriber-side predicates.
* ``event_registry``   — known event-name catalog + dynamic registration.
* ``event_serializer`` — JSON (de)serialization for store/transport.
* ``middleware``       — cross-cutting pipeline around every event.
* ``subscriber`` / ``publisher`` — consumer and producer surfaces.
* ``dispatcher``       — selection, ordering, and delivery engine.
* ``event_store``      — durable audit / replay record.
* ``bus``              — the facade wiring everything together.

Canonical event *names* live in ``app.core.constants.events`` and are
re-exported here for convenience.
"""

from __future__ import annotations

# --- Envelope & lifecycle -------------------------------------------------
from app.core.event_bus.event_types import Event, EventStatus

# --- Priority -------------------------------------------------------------
from app.core.event_bus.event_priority import (
    EventPriority,
    PriorityClass,
    PrioritizedEvent,
    PriorityAgingPolicy,
    resolve_priority,
)

# --- Context --------------------------------------------------------------
from app.core.event_bus.event_context import (
    EventContext,
    current_context,
    set_current_context,
    use_context,
    get_or_create_context,
)

# --- Filters --------------------------------------------------------------
from app.core.event_bus.event_filter import (
    EventFilter,
    AcceptAllFilter,
    NameFilter,
    NamePrefixFilter,
    NamePatternFilter,
    CategoryFilter,
    PriorityFilter,
    SourceFilter,
    PayloadFilter,
    PredicateFilter,
    AndFilter,
    OrFilter,
    NotFilter,
)

# --- Registry & serialization --------------------------------------------
from app.core.event_bus.event_registry import EventDescriptor, EventRegistry
from app.core.event_bus.event_serializer import EventSerializer

# --- Middleware -----------------------------------------------------------
from app.core.event_bus.middleware import (
    Middleware,
    MiddlewareChain,
    LoggingMiddleware,
    PriorityStampMiddleware,
    ContextPropagationMiddleware,
    DeduplicationMiddleware,
    RateLimitMiddleware,
    MetricsMiddleware,
)

# --- Producer / consumer surfaces ----------------------------------------
from app.core.event_bus.publisher import Publisher, ScopedPublisher
from app.core.event_bus.subscriber import (
    Subscriber,
    ErrorPolicy,
    SubscriptionState,
)

# --- Engine, store & facade ----------------------------------------------
from app.core.event_bus.dispatcher import Dispatcher
from app.core.event_bus.event_store import EventStore
from app.core.event_bus.bus import EventBus, register_event_bus

# --- Canonical event names (re-exported from the constants catalog) -------
from app.core.constants.events import (
    EventCategory,
    EventDeliveryMode,
    SystemEvent,
    LifecycleEvent,
    VoiceEvent,
    BrainEvent,
    SecurityEvent,
    GuiEvent,
    AgentEvent,
    LearningEvent,
    PluginEvent,
)

__all__ = [
    # envelope & lifecycle
    "Event",
    "EventStatus",
    # priority
    "EventPriority",
    "PriorityClass",
    "PrioritizedEvent",
    "PriorityAgingPolicy",
    "resolve_priority",
    # context
    "EventContext",
    "current_context",
    "set_current_context",
    "use_context",
    "get_or_create_context",
    # filters
    "EventFilter",
    "AcceptAllFilter",
    "NameFilter",
    "NamePrefixFilter",
    "NamePatternFilter",
    "CategoryFilter",
    "PriorityFilter",
    "SourceFilter",
    "PayloadFilter",
    "PredicateFilter",
    "AndFilter",
    "OrFilter",
    "NotFilter",
    # registry & serialization
    "EventDescriptor",
    "EventRegistry",
    "EventSerializer",
    # middleware
    "Middleware",
    "MiddlewareChain",
    "LoggingMiddleware",
    "PriorityStampMiddleware",
    "ContextPropagationMiddleware",
    "DeduplicationMiddleware",
    "RateLimitMiddleware",
    "MetricsMiddleware",
    # producer / consumer
    "Publisher",
    "ScopedPublisher",
    "Subscriber",
    "ErrorPolicy",
    "SubscriptionState",
    # engine, store & facade
    "Dispatcher",
    "EventStore",
    "EventBus",
    "register_event_bus",
    # canonical event names
    "EventCategory",
    "EventDeliveryMode",
    "SystemEvent",
    "LifecycleEvent",
    "VoiceEvent",
    "BrainEvent",
    "SecurityEvent",
    "GuiEvent",
    "AgentEvent",
    "LearningEvent",
    "PluginEvent",
]

__version__ = "1.0.0"
