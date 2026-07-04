# app/core/event_bus/middleware.py
"""
Event Bus middleware pipeline for AIOS.
=======================================
Middleware are ordered, composable interceptors the bus runs around every
published event *before* it is handed to the dispatcher/subscribers. They own
the cross-cutting concerns that must not be duplicated in each handler:

* priority stamping (via the catalog policy),
* flow-context activation,
* structured logging and telemetry,
* deduplication and rate limiting,
* dropping events that should not propagate.

Contract
--------
Each middleware implements :meth:`process`, receiving the current
:class:`Event` and a ``next_call`` continuation. It may:

* inspect / mutate the event, then ``return next_call(event)`` to continue;
* return an event **without** calling ``next_call`` to short-circuit;
* return ``None`` to **drop** the event (marks it ``DROPPED``; the bus stops).

The :class:`MiddlewareChain` wires them into a single callable following the
classic onion model, so the first registered middleware is the outermost layer.

Import-safe: depends only on the event primitives, constants, and logging.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Callable, Deque, Dict, Optional

from app.core.constants.events import default_priority
from app.core.event_bus.event_context import use_context
from app.core.event_bus.event_priority import resolve_priority
from app.core.event_bus.event_types import Event, EventStatus
from app.logging import Logger

__all__ = [
    "Middleware",
    "MiddlewareChain",
    "LoggingMiddleware",
    "PriorityStampMiddleware",
    "ContextPropagationMiddleware",
    "DeduplicationMiddleware",
    "RateLimitMiddleware",
    "MetricsMiddleware",
]

# A continuation: hands the (possibly transformed) event to the next layer.
NextCall = Callable[[Event], Optional[Event]]


class Middleware(ABC):
    """Base class for a single interceptor in the event pipeline."""

    @abstractmethod
    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        """Process ``event`` and optionally delegate to ``next_call``.

        Return the resulting event to continue, or ``None`` to drop it.
        """
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.__class__.__name__


class MiddlewareChain:
    """Ordered collection of middleware composed into one callable.

    The chain is built in *onion* order: the first added middleware is the
    outermost wrapper and runs first on the way in. Execution is guarded so a
    raising middleware is isolated — it drops the event rather than corrupting
    the pipeline for subsequent publishes.
    """

    def __init__(self, logger: Optional[Logger] = None) -> None:
        self._middlewares: list[Middleware] = []
        self._logger = logger
        self._lock = threading.RLock()

    def add(self, middleware: Middleware) -> "MiddlewareChain":
        """Append a middleware to the end of the pipeline (fluent)."""
        with self._lock:
            self._middlewares.append(middleware)
        return self

    def run(self, event: Event) -> Optional[Event]:
        """Execute the full pipeline for ``event``.

        Returns the transformed event when it survives every layer, or ``None``
        if any layer dropped it.
        """
        with self._lock:
            chain = list(self._middlewares)

        def make_link(index: int) -> NextCall:
            def link(evt: Event) -> Optional[Event]:
                if index >= len(chain):
                    return evt  # end of chain: event passes through
                mw = chain[index]
                try:
                    return mw.process(evt, make_link(index + 1))
                except Exception as exc:  # noqa: BLE001 - isolate faulty middleware
                    if self._logger:
                        self._logger.error(
                            "Middleware raised; dropping event",
                            extra={"middleware": mw.name, "event": evt.name, "error": str(exc)},
                        )
                    return None
            return link

        result = make_link(0)(event)
        if result is None:
            event.mark(EventStatus.DROPPED)
        return result

    def __len__(self) -> int:
        return len(self._middlewares)


# --------------------------------------------------------------------------- #
# Concrete middleware
# --------------------------------------------------------------------------- #
class PriorityStampMiddleware(Middleware):
    """Guarantee every event carries a resolved priority before dispatch.

    Ensures emergency/security escalation is applied even for publishers that
    never set a priority, using the catalog policy.
    """

    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        if event.priority is None:
            event.priority = default_priority(event.name)
        # Emergency names always win, regardless of any explicit value.
        event.priority = resolve_priority(event)
        return next_call(event)


class ContextPropagationMiddleware(Middleware):
    """Activate the event's flow context for the remainder of the pipeline.

    Downstream middleware and (synchronously dispatched) handlers that publish
    nested events then inherit the same correlation/causation chain.
    """

    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        if event.context is None:
            return next_call(event)
        with use_context(event.context):
            return next_call(event)


class LoggingMiddleware(Middleware):
    """Emit a structured log line as each event enters the pipeline."""

    def __init__(self, logger: Logger) -> None:
        self._logger = logger

    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        self._logger.debug(
            "Event published",
            extra={
                "event": event.name,
                "event_id": event.event_id,
                "priority": event.priority.name if event.priority else None,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )
        result = next_call(event)
        if result is None:
            self._logger.debug(
                "Event dropped in pipeline",
                extra={"event": event.name, "event_id": event.event_id},
            )
        return result


class DeduplicationMiddleware(Middleware):
    """Drop duplicate events seen within a sliding time window.

    Deduplicates on ``event_id`` by default. Useful when retries or redundant
    producers can republish the same logical occurrence. Thread-safe.
    """

    def __init__(self, window_seconds: float = 5.0, max_tracked: int = 4096) -> None:
        self._window = window_seconds
        self._max = max_tracked
        self._seen: Dict[str, float] = {}
        self._order: Deque[str] = deque()
        self._lock = threading.Lock()

    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        now = time.time()
        key = event.event_id
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return None  # duplicate within window → drop
            self._seen[key] = now
            self._order.append(key)
            if len(self._order) > self._max:
                oldest = self._order.popleft()
                self._seen.pop(oldest, None)
        return next_call(event)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._order and self._seen.get(self._order[0], 0.0) < cutoff:
            stale = self._order.popleft()
            self._seen.pop(stale, None)


class RateLimitMiddleware(Middleware):
    """Throttle high-frequency event names to a maximum rate.

    Limits are per event *name* using a fixed-window counter. Emergency events
    are never throttled so safety signals always propagate. Thread-safe.
    """

    def __init__(self, max_per_second: float = 100.0) -> None:
        self._max = max_per_second
        self._counts: Dict[str, tuple[int, float]] = {}  # name -> (count, window_start)
        self._lock = threading.Lock()

    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        if event.is_emergency:
            return next_call(event)
        now = time.time()
        with self._lock:
            count, start = self._counts.get(event.name, (0, now))
            if now - start >= 1.0:
                count, start = 0, now  # new window
            if count >= self._max:
                return None  # rate exceeded → drop
            self._counts[event.name] = (count + 1, start)
        return next_call(event)


class MetricsMiddleware(Middleware):
    """Count processed and dropped events per name for telemetry.

    Exposes a snapshot via :meth:`snapshot` for the core telemetry subsystem.
    """

    def __init__(self) -> None:
        self._processed: Dict[str, int] = {}
        self._dropped: Dict[str, int] = {}
        self._lock = threading.Lock()

    def process(self, event: Event, next_call: NextCall) -> Optional[Event]:
        result = next_call(event)
        with self._lock:
            bucket = self._processed if result is not None else self._dropped
            bucket[event.name] = bucket.get(event.name, 0) + 1
        return result

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {
                "processed": dict(self._processed),
                "dropped": dict(self._dropped),
            }
