# app/core/event_bus/event_filter.py
"""
Subscriber-side event filtering for the AIOS Event Bus.
=======================================================
A subscriber rarely wants *every* event. An :class:`EventFilter` is a
composable predicate the dispatcher evaluates per (event, subscriber) pair to
decide delivery. Filters are cheap, side-effect free, and combinable so complex
subscriptions ("all SECURITY events at HIGH+ priority, except sandbox timeouts
from plugins") stay declarative instead of buried in handler bodies.

Design
------
* :class:`EventFilter` is the abstract contract with an overridable
  :meth:`matches`. All concrete filters implement only that method.
* Filters compose via ``&`` (AND), ``|`` (OR), and ``~`` (NOT), plus the
  :meth:`EventFilter.all_of` / :meth:`EventFilter.any_of` constructors.
* Matching is defensive: an exception inside a custom predicate is treated as a
  non-match rather than propagating into the dispatch loop, so one bad filter
  can never take down delivery for other subscribers.

Depends only on the event envelope, the priority enum, and the constants
catalog — import-safe, no cycles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable, Optional, Pattern
import re

from app.core.constants.events import EventCategory
from app.core.event_bus.event_priority import EventPriority, resolve_priority
from app.core.event_bus.event_types import Event

__all__ = [
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
]


class EventFilter(ABC):
    """Composable predicate deciding whether an event should be delivered."""

    @abstractmethod
    def matches(self, event: Event) -> bool:
        """Return True if ``event`` passes this filter."""
        raise NotImplementedError

    # -- safe evaluation used by the dispatcher ---------------------------
    def accepts(self, event: Event) -> bool:
        """Evaluate :meth:`matches` defensively.

        A raising predicate is treated as a non-match so a faulty filter never
        disrupts delivery to other subscribers.
        """
        try:
            return self.matches(event)
        except Exception:  # noqa: BLE001 - a bad filter must not break dispatch
            return False

    # -- combinators ------------------------------------------------------
    def __and__(self, other: "EventFilter") -> "EventFilter":
        return AndFilter(self, other)

    def __or__(self, other: "EventFilter") -> "EventFilter":
        return OrFilter(self, other)

    def __invert__(self) -> "EventFilter":
        return NotFilter(self)

    @staticmethod
    def all_of(*filters: "EventFilter") -> "EventFilter":
        """Match only when every supplied filter matches (AND)."""
        return AndFilter(*filters)

    @staticmethod
    def any_of(*filters: "EventFilter") -> "EventFilter":
        """Match when at least one supplied filter matches (OR)."""
        return OrFilter(*filters)


class AcceptAllFilter(EventFilter):
    """Passes every event. The default when a subscriber sets no filter."""

    def matches(self, event: Event) -> bool:
        return True

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<AcceptAll>"


class NameFilter(EventFilter):
    """Match events whose name is in an explicit allow-set."""

    def __init__(self, *names: str) -> None:
        self._names = frozenset(names)

    def matches(self, event: Event) -> bool:
        return event.name in self._names

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Name in {sorted(self._names)}>"


class NamePrefixFilter(EventFilter):
    """Match events whose name starts with any of the given prefixes.

    Enables hierarchical subscriptions like ``"security."`` or ``"voice.stt"``.
    """

    def __init__(self, *prefixes: str) -> None:
        self._prefixes = tuple(prefixes)

    def matches(self, event: Event) -> bool:
        return event.name.startswith(self._prefixes)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<NamePrefix {self._prefixes}>"


class NamePatternFilter(EventFilter):
    """Match events whose name matches a regular expression."""

    def __init__(self, pattern: str | Pattern[str]) -> None:
        self._pattern: Pattern[str] = (
            pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
        )

    def matches(self, event: Event) -> bool:
        return self._pattern.search(event.name) is not None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<NamePattern {self._pattern.pattern!r}>"


class CategoryFilter(EventFilter):
    """Match events belonging to any of the given categories."""

    def __init__(self, *categories: EventCategory) -> None:
        self._categories = frozenset(categories)

    def matches(self, event: Event) -> bool:
        return event.category in self._categories

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Category in {[c.value for c in self._categories]}>"


class PriorityFilter(EventFilter):
    """Match events whose effective priority is at least ``minimum``.

    Uses :func:`resolve_priority` so emergency escalation and catalog defaults
    are honored even when the publisher left ``priority`` unset.
    """

    def __init__(self, minimum: EventPriority) -> None:
        self._minimum = minimum

    def matches(self, event: Event) -> bool:
        return resolve_priority(event) >= self._minimum

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Priority >= {self._minimum.name}>"


class SourceFilter(EventFilter):
    """Match events emitted by any of the given logical sources."""

    def __init__(self, *sources: str) -> None:
        self._sources = frozenset(sources)

    def matches(self, event: Event) -> bool:
        return event.source in self._sources

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Source in {sorted(self._sources)}>"


class PayloadFilter(EventFilter):
    """Match events whose payload contains a key equal to an expected value.

    When ``expected`` is omitted, matches on mere key presence.
    """

    _MISSING = object()

    def __init__(self, key: str, expected: Any = _MISSING) -> None:
        self._key = key
        self._expected = expected

    def matches(self, event: Event) -> bool:
        if self._key not in event.payload:
            return False
        if self._expected is PayloadFilter._MISSING:
            return True
        return event.payload[self._key] == self._expected

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Payload {self._key!r}>"


class PredicateFilter(EventFilter):
    """Wrap an arbitrary callable ``(Event) -> bool`` as a filter."""

    def __init__(self, predicate: Callable[[Event], bool]) -> None:
        if not callable(predicate):
            raise TypeError("PredicateFilter requires a callable")
        self._predicate = predicate

    def matches(self, event: Event) -> bool:
        return bool(self._predicate(event))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        name = getattr(self._predicate, "__name__", repr(self._predicate))
        return f"<Predicate {name}>"


class AndFilter(EventFilter):
    """Logical AND over a set of filters (matches when all match)."""

    def __init__(self, *filters: EventFilter) -> None:
        self._filters = tuple(filters)

    def matches(self, event: Event) -> bool:
        return all(f.accepts(event) for f in self._filters)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"({' AND '.join(repr(f) for f in self._filters)})"


class OrFilter(EventFilter):
    """Logical OR over a set of filters (matches when any matches)."""

    def __init__(self, *filters: EventFilter) -> None:
        self._filters = tuple(filters)

    def matches(self, event: Event) -> bool:
        return any(f.accepts(event) for f in self._filters)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"({' OR '.join(repr(f) for f in self._filters)})"


class NotFilter(EventFilter):
    """Logical negation of a single filter."""

    def __init__(self, inner: EventFilter) -> None:
        self._inner = inner

    def matches(self, event: Event) -> bool:
        return not self._inner.accepts(event)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"(NOT {self._inner!r})"
