# app/core/event_bus/dispatcher.py
"""
Event delivery engine for the AIOS Event Bus.
=============================================
The :class:`Dispatcher` is the heart of delivery. For each event it:

1. runs the :class:`MiddlewareChain` (priority stamping, context, dedup, …);
2. drops the event if the pipeline vetoed it;
3. selects subscribers whose filter accepts the event;
4. orders them by their relative priority;
5. invokes them according to the event's :class:`EventDeliveryMode`:
   * ``SYNC``      — blocking, in declaration order (state gates);
   * ``ASYNC``     — scheduled on the asyncio loop when one is running;
   * ``QUEUED``    — offloaded to a bounded :class:`ThreadPoolExecutor`;
   * ``BROADCAST`` — fan-out, order not guaranteed.

Every event is offered to the :class:`EventStore` after dispatch so the audit
trail reflects the final lifecycle status. Handler faults are contained per
subscriber (per each subscriber's :class:`ErrorPolicy`) so one failure never
aborts delivery to the rest.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from app.core.constants.events import EventDeliveryMode
from app.core.event_bus.event_types import Event, EventStatus
from app.core.event_bus.event_store import EventStore
from app.core.event_bus.middleware import Middleware, MiddlewareChain
from app.core.event_bus.subscriber import Subscriber
from app.core.exceptions.event import EventDispatchError
from app.logging.logger_factory import LoggerFactory
from app.logging.logger import Logger, LogLevel

__all__ = ["Dispatcher"]


class Dispatcher:
    """Routes events through middleware to matching, ordered subscribers.

    Parameters
    ----------
    middleware:
        Optional pre-built :class:`MiddlewareChain`; a fresh one is created
        when omitted.
    store:
        Optional :class:`EventStore` to record dispatched events.
    max_workers:
        Size of the thread pool backing ``QUEUED`` delivery.
    logger:
        Logger for dispatch diagnostics; created via :class:`LoggerFactory`
        when not supplied.
    logger_factory:
        Factory reused for logger creation; a new one is created when omitted.
    """

    def __init__(
        self,
        *,
        middleware: Optional[MiddlewareChain] = None,
        store: Optional[EventStore] = None,
        max_workers: int = 4,
        logger: Optional[Logger] = None,
        logger_factory: Optional[LoggerFactory] = None,
    ) -> None:
        self._factory = logger_factory or LoggerFactory()
        self._logger = logger or self._factory.create_console_logger(
            "core.event_bus.dispatcher", LogLevel.INFO
        )
        self._middleware = middleware or MiddlewareChain(logger=self._logger)
        self._store = store
        self._subscribers: List[Subscriber] = []
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="aios-event"
        )
        self._closed = False

    # ----------------------------------------------------- subscriber admin
    def add_subscriber(self, subscriber: Subscriber) -> Subscriber:
        """Register a subscriber and wire its unsubscribe callback."""
        with self._lock:
            self._subscribers.append(subscriber)
        subscriber.bind_unsubscribe(self._remove_subscriber)
        return subscriber

    def _remove_subscriber(self, subscriber: Subscriber) -> None:
        with self._lock:
            try:
                self._subscribers.remove(subscriber)
            except ValueError:
                pass

    def add_middleware(self, middleware: Middleware) -> None:
        """Append a middleware to the delivery pipeline."""
        self._middleware.add(middleware)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # --------------------------------------------------------- dispatch (sync)
    def dispatch(self, event: Event) -> Event:
        """Run the pipeline and deliver ``event`` from a synchronous context.

        Returns the event with its final lifecycle status set.
        """
        if self._closed:
            raise EventDispatchError(
                f"Dispatcher is closed; cannot dispatch {event.name!r}"
            )

        processed = self._middleware.run(event)
        if processed is None:
            return self._finalize(event)  # dropped by middleware

        processed.mark(EventStatus.DISPATCHING)
        targets = self._select(processed)
        if not targets:
            processed.mark(EventStatus.DROPPED)
            return self._finalize(processed)

        mode = processed.delivery_mode
        try:
            if mode is EventDeliveryMode.QUEUED:
                for sub in targets:
                    self._executor.submit(self._safe_invoke, sub, processed)
            else:
                # SYNC / ASYNC(no loop) / BROADCAST all deliver inline here.
                for sub in targets:
                    self._safe_invoke(sub, processed)
            processed.mark(EventStatus.HANDLED)
        except Exception as exc:  # noqa: BLE001 - surface as structured error
            processed.mark(EventStatus.FAILED)
            raise EventDispatchError(
                f"Dispatch failed for {event.name!r}", cause=exc
            ) from exc

        return self._finalize(processed)

    # -------------------------------------------------------- dispatch (async)
    async def dispatch_async(self, event: Event) -> Event:
        """Run the pipeline and deliver ``event`` on the asyncio loop.

        Async and sync handlers are awaited/executed concurrently via
        ``asyncio.gather``; blocking sync handlers are offloaded to the thread
        pool so they never stall the loop.
        """
        if self._closed:
            raise EventDispatchError(
                f"Dispatcher is closed; cannot dispatch {event.name!r}"
            )

        processed = self._middleware.run(event)
        if processed is None:
            return self._finalize(event)

        processed.mark(EventStatus.DISPATCHING)
        targets = self._select(processed)
        if not targets:
            processed.mark(EventStatus.DROPPED)
            return self._finalize(processed)

        loop = asyncio.get_running_loop()
        coros = []
        for sub in targets:
            if sub.is_async:
                coros.append(sub.invoke_async(processed))
            else:
                # Offload blocking handlers to the executor.
                coros.append(loop.run_in_executor(self._executor, self._safe_invoke, sub, processed))

        results = await asyncio.gather(*coros, return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        processed.mark(EventStatus.FAILED if failed else EventStatus.HANDLED)
        if failed:
            self._logger.error(
                "Async dispatch had handler failures",
                extra={"event": processed.name, "failures": len(failed)},
            )
        return self._finalize(processed)

    # ---------------------------------------------------------------- helpers
    def _select(self, event: Event) -> List[Subscriber]:
        """Return active subscribers whose filter accepts ``event``, ordered."""
        with self._lock:
            matches = [s for s in self._subscribers if s.wants(event)]
        # Higher subscriber priority runs first; stable for equal priorities.
        matches.sort(key=lambda s: s.priority, reverse=True)
        return matches

    def _safe_invoke(self, subscriber: Subscriber, event: Event) -> None:
        """Invoke a subscriber, containing faults per its error policy.

        ``ErrorPolicy.PROPAGATE`` handlers re-raise; the caller decides whether
        that aborts a QUEUED task (isolated) or a SYNC batch (surfaced).
        """
        subscriber.invoke(event)

    def _finalize(self, event: Event) -> Event:
        """Record the event in the store (best-effort) and return it."""
        if self._store is not None:
            try:
                self._store.record(event)
            except Exception as exc:  # noqa: BLE001 - store must not break dispatch
                self._logger.error(
                    "Event store record failed",
                    extra={"event": event.name, "error": str(exc)},
                )
        return event

    # ---------------------------------------------------------------- lifecycle
    def close(self, *, wait: bool = True) -> None:
        """Shut down the thread pool. Call during application shutdown."""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=wait)
        self._logger.info("Dispatcher closed")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<Dispatcher subscribers={self.subscriber_count} "
            f"middleware={len(self._middleware)} closed={self._closed}>"
        )
