# app/core/event_bus/event_priority.py
"""
Runtime priority machinery for the AIOS Event Bus.
==================================================
The *catalog* of priority levels (:class:`EventPriority`) and the static
name→priority policy (``default_priority``, ``EMERGENCY_EVENTS``) are owned by
``app/core/constants/events.py``. This module owns the *runtime behavior* the
dispatcher and the priority queue need:

* Resolve the effective priority of a live :class:`Event` (explicit priority,
  else the catalog default, with emergency escalation always winning).
* Provide a total ordering key so a heap-based priority queue delivers
  higher-priority events first and preserves FIFO order within a level.
* Apply optional *priority aging* so long-waiting low-priority events do not
  starve under sustained high-priority load.

Design rules mirror the rest of ``core``: standard library only, import-safe,
no cycles (depends solely on constants and the event envelope).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Tuple
import itertools

from app.core.constants.events import (
    EMERGENCY_EVENTS,
    EventPriority,
    default_priority,
)

if TYPE_CHECKING:  # avoid a runtime import cycle with event_types
    from app.core.event_bus.event_types import Event

__all__ = [
    "EventPriority",
    "PriorityClass",
    "resolve_priority",
    "PrioritizedEvent",
    "PriorityAgingPolicy",
]


class PriorityClass(IntEnum):
    """Coarse dispatch classes the bus uses to pick a delivery lane.

    This is a runtime grouping *over* the finer-grained catalog
    :class:`EventPriority`, letting the dispatcher route without inspecting
    every discrete level. Inherits ``IntEnum`` so lanes compare and sort
    naturally (higher = more urgent).
    """

    BACKGROUND = 0   # LOW — deferred, idle-time work
    STANDARD = 1     # NORMAL — the common case
    ELEVATED = 2     # HIGH / CRITICAL — expedited delivery
    IMMEDIATE = 3    # EMERGENCY — preempts the queue entirely

    @classmethod
    def from_priority(cls, priority: EventPriority) -> "PriorityClass":
        """Map a fine-grained :class:`EventPriority` onto a dispatch class."""
        if priority >= EventPriority.EMERGENCY:
            return cls.IMMEDIATE
        if priority >= EventPriority.HIGH:
            return cls.ELEVATED
        if priority >= EventPriority.NORMAL:
            return cls.STANDARD
        return cls.BACKGROUND


def resolve_priority(event: "Event") -> EventPriority:
    """Return the effective dispatch priority for a live event.

    Resolution order:

    1. An emergency event name always resolves to ``EMERGENCY`` regardless of
       any explicit value (safety events must never be down-prioritized).
    2. An explicitly-set ``event.priority`` is honored.
    3. Otherwise the catalog default for the event name is used.
    """
    if event.name in EMERGENCY_EVENTS:
        return EventPriority.EMERGENCY
    if event.priority is not None:
        return event.priority
    return default_priority(event.name)


# Monotonic tiebreaker so events of equal priority keep insertion (FIFO) order
# and heap comparison never falls through to the (unorderable) Event payload.
_sequence = itertools.count()


@dataclass(order=True)
class PrioritizedEvent:
    """A heap-ready wrapper pairing an event with its ordering key.

    Python's ``heapq`` and ``queue.PriorityQueue`` are min-heaps, so the sort
    key is negated priority: higher :class:`EventPriority` yields a smaller key
    and is therefore popped first. A monotonically increasing sequence number
    breaks ties in FIFO order and prevents the heap from ever comparing the
    underlying :class:`Event` objects directly.
    """

    # Ordering fields (compared in declaration order by the dataclass).
    sort_key: Tuple[int, int] = field(init=False)
    event: "Event" = field(compare=False)

    def __init__(self, event: "Event") -> None:
        self.event = event
        priority = resolve_priority(event)
        # Negate priority for min-heap semantics; append a FIFO sequence tiebreak.
        object.__setattr__(self, "sort_key", (-int(priority), next(_sequence)))

    @property
    def priority(self) -> EventPriority:
        return EventPriority(-self.sort_key[0])

    @property
    def priority_class(self) -> PriorityClass:
        return PriorityClass.from_priority(self.priority)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<PrioritizedEvent name={self.event.name!r} "
            f"priority={self.priority.name} seq={self.sort_key[1]}>"
        )


@dataclass
class PriorityAgingPolicy:
    """Anti-starvation policy that promotes long-waiting events.

    Under sustained high-priority load, low-priority events can wait
    indefinitely. This policy bumps an event's effective priority by one level
    for every ``step_seconds`` it has spent waiting, capped at ``max_priority``.
    Emergency events are exempt (already maximal) and never de-prioritized.

    The dispatcher calls :meth:`effective_priority` when re-evaluating queued
    events, so aging is opt-in and has zero cost when disabled.
    """

    enabled: bool = True
    step_seconds: float = 5.0
    max_priority: EventPriority = EventPriority.HIGH

    def effective_priority(self, event: "Event") -> EventPriority:
        """Compute the aged priority for ``event`` given its current wait time."""
        base = resolve_priority(event)
        if not self.enabled or base >= EventPriority.EMERGENCY:
            return base
        if self.step_seconds <= 0:
            return base

        bumps = int(event.age_seconds // self.step_seconds)
        if bumps <= 0:
            return base

        aged_value = min(int(base) + (bumps * 10), int(self.max_priority))
        # Snap to the nearest defined level at or below the computed value.
        candidates = [p for p in EventPriority if int(p) <= aged_value]
        return max(candidates) if candidates else base
