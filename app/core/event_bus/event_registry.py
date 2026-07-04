# app/core/event_bus/event_registry.py
"""
Runtime event-name registry for the AIOS Event Bus.
====================================================
``constants/events.py`` is the *static* catalog of first-party event names.
This module turns that catalog into a queryable *runtime* registry and layers
dynamic registration on top of it, so:

* every canonical enum member is indexed once at construction;
* plugins (FG7) and feature groups can register additional event names at
  runtime under a controlled, validated path;
* publishers/middleware can validate that an event name is known and look up
  its category, default priority, and originating enum member.

Why a registry on top of constants?
-----------------------------------
The catalog is a closed set of enums — perfect for first-party code, but the
plugin system needs to introduce new event names without editing core. The
registry provides that extension seam while keeping first-party names immutable
and detecting collisions.

Thread-safe and import-safe (depends only on constants + priority + exceptions).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional

from app.core.constants.events import (
    EVENT_ENUM_BY_CATEGORY,
    EventCategory,
    EventPriority,
    default_priority,
)
from app.core.exceptions import UnknownEventTypeError

__all__ = ["EventDescriptor", "EventRegistry"]


@dataclass(frozen=True)
class EventDescriptor:
    """Immutable metadata describing a single registered event name.

    Attributes
    ----------
    name : the canonical event string (e.g. ``"voice.wakeword.triggered"``).
    category : the :class:`EventCategory` the event belongs to.
    default_priority : recommended dispatch priority for the name.
    member : the source enum member for first-party events, else ``None``
             (dynamic/plugin events have no backing enum).
    dynamic : ``True`` when registered at runtime rather than from the catalog.
    """

    name: str
    category: EventCategory
    default_priority: EventPriority
    member: Optional[Enum] = None
    dynamic: bool = False


class EventRegistry:
    """Authoritative, queryable set of known event names + metadata.

    On construction the registry ingests every enum in
    ``EVENT_ENUM_BY_CATEGORY`` so all first-party names are immediately known.
    Additional names may be registered later via :meth:`register` (used by the
    plugin loader and feature groups exposing custom events).
    """

    def __init__(self, *, load_catalog: bool = True) -> None:
        self._descriptors: Dict[str, EventDescriptor] = {}
        self._lock = threading.RLock()
        if load_catalog:
            self._load_catalog()

    # ------------------------------------------------------------- loading
    def _load_catalog(self) -> None:
        """Index every canonical enum member from the constants catalog."""
        for category, enum_cls in EVENT_ENUM_BY_CATEGORY.items():
            for member in enum_cls:
                descriptor = EventDescriptor(
                    name=member.value,
                    category=category,
                    default_priority=default_priority(member.value),
                    member=member,
                    dynamic=False,
                )
                self._descriptors[member.value] = descriptor

    # ---------------------------------------------------------- registration
    def register(
        self,
        name: str,
        category: EventCategory,
        *,
        priority: Optional[EventPriority] = None,
        replace: bool = False,
    ) -> EventDescriptor:
        """Register a dynamic (e.g. plugin) event name.

        Parameters
        ----------
        name : the new event string; must be non-empty.
        category : the category the event should route under.
        priority : explicit default priority; falls back to the catalog policy.
        replace : allow overwriting an existing *dynamic* registration.

        Raises
        ------
        ValueError : if the name is empty.
        UnknownEventTypeError : if the name collides with a first-party
            (non-dynamic) event, which must never be redefined at runtime.
        """
        if not name:
            raise ValueError("Event name must be a non-empty string")

        with self._lock:
            existing = self._descriptors.get(name)
            if existing is not None:
                if not existing.dynamic:
                    raise UnknownEventTypeError(
                        f"Cannot override first-party event name {name!r}"
                    )
                if not replace:
                    return existing  # idempotent for dynamic re-registration

            descriptor = EventDescriptor(
                name=name,
                category=category,
                default_priority=priority or default_priority(name),
                member=None,
                dynamic=True,
            )
            self._descriptors[name] = descriptor
            return descriptor

    def register_many(
        self, names: Iterable[str], category: EventCategory
    ) -> List[EventDescriptor]:
        """Bulk-register dynamic event names under one category."""
        return [self.register(name, category) for name in names]

    def unregister(self, name: str) -> None:
        """Remove a *dynamic* event name.

        First-party catalog names cannot be unregistered.
        """
        with self._lock:
            existing = self._descriptors.get(name)
            if existing is None:
                return
            if not existing.dynamic:
                raise UnknownEventTypeError(
                    f"Cannot unregister first-party event name {name!r}"
                )
            self._descriptors.pop(name, None)

    # -------------------------------------------------------------- queries
    def is_known(self, name: str) -> bool:
        """True if ``name`` is registered (catalog or dynamic)."""
        with self._lock:
            return name in self._descriptors

    def validate(self, name: str) -> None:
        """Raise :class:`UnknownEventTypeError` if ``name`` is not registered.

        Called by the bus in strict mode before accepting a publish.
        """
        if not self.is_known(name):
            raise UnknownEventTypeError(f"Unknown event name: {name!r}")

    def get(self, name: str) -> Optional[EventDescriptor]:
        """Return the descriptor for ``name``, or ``None`` if unknown."""
        with self._lock:
            return self._descriptors.get(name)

    def require(self, name: str) -> EventDescriptor:
        """Return the descriptor for ``name`` or raise if unknown."""
        descriptor = self.get(name)
        if descriptor is None:
            raise UnknownEventTypeError(f"Unknown event name: {name!r}")
        return descriptor

    def category_of(self, name: str) -> EventCategory:
        """Return the category for a known event name."""
        return self.require(name).category

    def default_priority_of(self, name: str) -> EventPriority:
        """Return the default dispatch priority for a known event name."""
        return self.require(name).default_priority

    def names(self, category: Optional[EventCategory] = None) -> List[str]:
        """List known event names, optionally filtered by category."""
        with self._lock:
            if category is None:
                return sorted(self._descriptors.keys())
            return sorted(
                d.name for d in self._descriptors.values() if d.category is category
            )

    def descriptors(self) -> List[EventDescriptor]:
        """Return a snapshot of all registered descriptors."""
        with self._lock:
            return list(self._descriptors.values())

    def __contains__(self, name: str) -> bool:
        return self.is_known(name)

    def __len__(self) -> int:
        with self._lock:
            return len(self._descriptors)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        with self._lock:
            dynamic = sum(1 for d in self._descriptors.values() if d.dynamic)
        return f"<EventRegistry total={len(self)} dynamic={dynamic}>"
