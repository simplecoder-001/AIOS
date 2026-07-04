# app/core/event_bus/subscriber.py
"""
Event Bus subscribers for AIOS.
===============================
A :class:`Subscriber` binds a *handler* to a *filter* plus delivery policy. The
dispatcher owns a collection of subscribers; for each event it selects those
whose filter accepts the event and invokes them according to their policy.

Responsibilities
----------------
* Encapsulate the handler callable (sync **or** async) behind one invocation
  surface so the dispatcher need not special-case coroutine functions.
* Hold per-subscriber delivery preferences: filter, delivery mode, one-shot,
  and error policy (isolate vs. propagate).
* Provide safe invocation that records outcome and never lets a single failing
  handler corrupt the dispatch loop (subject to error policy).
* Support unsubscription and weak-reference handlers so bound methods on
  short-lived objects do not leak.

Import-safe: depends only on the event primitives, filters, and logging.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import threading
import uuid
import weakref
from typing import Any, Awaitable, Callable, Optional, Union

from app.core.event_bus.event_filter import AcceptAllFilter, EventFilter
from app.core.event_bus.event_types import Event
from app.core.exceptions import EventHandlerError
from app.logging import Logger

__all__ = [
    "ErrorPolicy",
    "SubscriptionState",
    "Subscriber",
]

# A handler may be synchronous or return an awaitable.
SyncHandler = Callable[[Event], Any]
AsyncHandler = Callable[[Event], Awaitable[Any]]
EventHandler = Union[SyncHandler, AsyncHandler]


class ErrorPolicy(str, enum.Enum):
    """What the dispatcher should do when a handler raises."""

    ISOLATE = "isolate"      # log + swallow; other subscribers proceed (default)
    PROPAGATE = "propagate"  # re-raise as EventHandlerError to the dispatcher
    DISABLE = "disable"      # log, swallow, and auto-disable this subscriber


class SubscriptionState(str, enum.Enum):
    """Lifecycle state of a subscription."""

    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"     # auto-disabled after a fatal handler error
    UNSUBSCRIBED = "unsubscribed"


class Subscriber:
    """A handler bound to a filter and delivery policy.

    Parameters
    ----------
    handler:
        Callable invoked with the matching :class:`Event`. May be a plain
        function/method or a coroutine function.
    event_filter:
        The :class:`EventFilter` deciding which events reach this handler.
        Defaults to :class:`AcceptAllFilter`.
    priority:
        Relative ordering hint among subscribers of the same event (higher
        runs first). Distinct from event dispatch priority.
    once:
        When ``True`` the subscription auto-unsubscribes after its first
        successful delivery (one-shot).
    error_policy:
        How handler exceptions are treated (see :class:`ErrorPolicy`).
    weak:
        When ``True`` and the handler is a bound method, hold it weakly so the
        owning object can be garbage-collected; the subscription then disables
        itself automatically once the referent is gone.
    name:
        Optional human-readable label for logs/telemetry.
    """

    def __init__(
        self,
        handler: EventHandler,
        *,
        event_filter: Optional[EventFilter] = None,
        priority: int = 0,
        once: bool = False,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
        weak: bool = False,
        name: Optional[str] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("Subscriber handler must be callable")

        self.id = uuid.uuid4().hex
        self.filter = event_filter or AcceptAllFilter()
        self.priority = priority
        self.once = once
        self.error_policy = error_policy
        self.name = name or getattr(handler, "__name__", f"subscriber-{self.id[:8]}")
        self._logger = logger
        self._is_async = inspect.iscoroutinefunction(handler)
        self._state = SubscriptionState.ACTIVE
        self._lock = threading.Lock()
        self._call_count = 0
        # unsubscribe callback wired by the dispatcher/bus on registration.
        self._on_unsubscribe: Optional[Callable[["Subscriber"], None]] = None

        self._weak = weak
        self._handler_ref = self._make_ref(handler, weak)

    # ------------------------------------------------------------ properties
    @property
    def is_async(self) -> bool:
        """True if the handler is a coroutine function."""
        return self._is_async

    @property
    def state(self) -> SubscriptionState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state is SubscriptionState.ACTIVE

    @property
    def call_count(self) -> int:
        return self._call_count

    # -------------------------------------------------------------- matching
    def wants(self, event: Event) -> bool:
        """True if this active subscriber's filter accepts ``event``."""
        if self._state is not SubscriptionState.ACTIVE:
            return False
        return self.filter.accepts(event)

    # ------------------------------------------------------------- invocation
    def invoke(self, event: Event) -> Any:
        """Invoke a synchronous handler with ``event``.

        For async handlers this schedules/blocks appropriately: if a running
        loop is present the coroutine is returned for the caller to await;
        otherwise it is run to completion. Prefer :meth:`invoke_async` from
        async dispatch paths.
        """
        handler = self._resolve_handler()
        if handler is None:
            return None

        try:
            if self._is_async:
                coro = handler(event)
                try:
                    asyncio.get_running_loop()
                    # Caller is async; hand back the coroutine to be awaited.
                    return coro
                except RuntimeError:
                    return asyncio.run(coro)
            result = handler(event)
            self._after_success()
            return result
        except Exception as exc:  # noqa: BLE001 - governed by error policy
            return self._handle_error(event, exc)

    async def invoke_async(self, event: Event) -> Any:
        """Await-friendly invocation for both sync and async handlers."""
        handler = self._resolve_handler()
        if handler is None:
            return None

        try:
            if self._is_async:
                result = await handler(event)
            else:
                result = handler(event)
            self._after_success()
            return result
        except Exception as exc:  # noqa: BLE001 - governed by error policy
            return self._handle_error(event, exc)

    # ---------------------------------------------------------------- control
    def pause(self) -> None:
        with self._lock:
            if self._state is SubscriptionState.ACTIVE:
                self._state = SubscriptionState.PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._state is SubscriptionState.PAUSED:
                self._state = SubscriptionState.ACTIVE

    def unsubscribe(self) -> None:
        """Detach from the bus and mark this subscription terminal."""
        with self._lock:
            if self._state is SubscriptionState.UNSUBSCRIBED:
                return
            self._state = SubscriptionState.UNSUBSCRIBED
        if self._on_unsubscribe is not None:
            self._on_unsubscribe(self)

    def bind_unsubscribe(self, callback: Callable[["Subscriber"], None]) -> None:
        """Wire the callback the bus uses to remove this subscriber."""
        self._on_unsubscribe = callback

    # ---------------------------------------------------------------- helpers
    def _after_success(self) -> None:
        with self._lock:
            self._call_count += 1
            should_close = self.once
        if should_close:
            self.unsubscribe()

    def _handle_error(self, event: Event, exc: Exception) -> Any:
        if self._logger:
            self._logger.error(
                "Event handler raised",
                extra={
                    "subscriber": self.name,
                    "event": event.name,
                    "policy": self.error_policy.value,
                    "error": str(exc),
                },
            )
        if self.error_policy is ErrorPolicy.PROPAGATE:
            raise EventHandlerError(
                f"Handler {self.name!r} failed for event {event.name!r}",
                cause=exc,
            ) from exc
        if self.error_policy is ErrorPolicy.DISABLE:
            with self._lock:
                self._state = SubscriptionState.DISABLED
        return None

    def _make_ref(
        self, handler: EventHandler, weak: bool
    ) -> Callable[[], Optional[EventHandler]]:
        if not weak:
            return lambda: handler
        try:
            ref = weakref.WeakMethod(handler)  # type: ignore[arg-type]
        except TypeError:
            ref = weakref.ref(handler)
        return ref  # type: ignore[return-value]

    def _resolve_handler(self) -> Optional[EventHandler]:
        handler = self._handler_ref()
        if handler is None:
            # Weak referent collected → self-disable.
            with self._lock:
                self._state = SubscriptionState.DISABLED
            if self._logger:
                self._logger.debug(
                    "Subscriber handler was garbage-collected; disabling",
                    extra={"subscriber": self.name},
                )
        return handler

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<Subscriber name={self.name!r} state={self._state.value} "
            f"async={self._is_async} once={self.once} calls={self._call_count}>"
        )
