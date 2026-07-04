# app/core/event_bus/bus.py
"""
The AIOS Event Bus — public facade.
===================================
``EventBus`` is the single entry point every subsystem uses for event-driven
communication. It composes the package's building blocks into one coherent,
thread-safe service:

* :class:`EventRegistry`  — validates event names (catalog + dynamic plugins);
* :class:`MiddlewareChain`— cross-cutting pipeline (priority, context, dedup…);
* :class:`Dispatcher`     — selects, orders, and delivers to subscribers;
* :class:`EventStore`     — durable audit / replay record.

It hands out :class:`Publisher` objects (producer surface) and creates
:class:`Subscriber` objects (consumer surface), so feature groups never touch
the dispatcher or store directly. A helper registers the bus into the DI
:class:`Container` as a singleton, giving every feature group the same instance.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, List, Optional

from app.core.constants.events import EventCategory, EventDeliveryMode
from app.core.event_bus.dispatcher import Dispatcher
from app.core.event_bus.event_filter import AcceptAllFilter, EventFilter
from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_registry import EventRegistry
from app.core.event_bus.event_store import EventStore
from app.core.event_bus.event_types import Event
from app.core.event_bus.middleware import (
    LoggingMiddleware,
    MiddlewareChain,
    PriorityStampMiddleware,
    ContextPropagationMiddleware,
)
from app.core.event_bus.publisher import Publisher, ScopedPublisher
from app.core.event_bus.subscriber import ErrorPolicy, EventHandler, Subscriber

from app.logging.logger_factory import LoggerFactory
from app.logging.logger import Logger, LogLevel
from app.core.exceptions.event import EventDispatchError, EventPublishError, UnknownEventTypeError
from app.dependency_injection.container import Container

__all__ = ["EventBus", "register_event_bus"]


class EventBus:
    """Central publish/subscribe service for AIOS.

    Parameters
    ----------
    store:
        Optional :class:`EventStore`; a memory-only store is created when
        omitted so recent-event queries always work.
    registry:
        Optional :class:`EventRegistry`; a catalog-loaded one is created when
        omitted.
    strict:
        When ``True``, publishing an unregistered event name raises
        :class:`UnknownEventTypeError` (recommended for production).
    max_workers:
        Thread-pool size for QUEUED delivery.
    logger / logger_factory:
        Logging wiring; created via :class:`LoggerFactory` when not supplied.
    """

    def __init__(
        self,
        *,
        store: Optional[EventStore] = None,
        registry: Optional[EventRegistry] = None,
        strict: bool = False,
        max_workers: int = 4,
        logger: Optional[Logger] = None,
        logger_factory: Optional[LoggerFactory] = None,
    ) -> None:
        self._factory = logger_factory or LoggerFactory()
        self._logger = logger or self._factory.create_console_logger(
            "core.event_bus", LogLevel.INFO
        )
        self._registry = registry or EventRegistry()
        self._store = store or EventStore(logger=self._logger)
        self._strict = strict
        self._lock = threading.RLock()
        self._running = False

        # Default cross-cutting pipeline (order = onion, outermost first).
        chain = MiddlewareChain(logger=self._logger)
        chain.add(LoggingMiddleware(self._logger))
        chain.add(PriorityStampMiddleware())
        chain.add(ContextPropagationMiddleware())

        self._dispatcher = Dispatcher(
            middleware=chain,
            store=self._store,
            max_workers=max_workers,
            logger=self._logger,
            logger_factory=self._factory,
        )

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Mark the bus running. Publishing before start is rejected."""
        with self._lock:
            self._running = True
        self._logger.info("Event bus started")

    def stop(self) -> None:
        """Stop the bus, drain the dispatcher, and close the store."""
        with self._lock:
            self._running = False
        self._dispatcher.close(wait=True)
        self._store.close()
        self._logger.info("Event bus stopped")

    @property
    def registry(self) -> EventRegistry:
        return self._registry

    @property
    def store(self) -> EventStore:
        return self._store

    # ---------------------------------------------------------------- publish
    def publish(self, event: Event) -> Optional[Event]:
        """Validate and dispatch ``event`` synchronously.

        Returns the finalized event, or ``None`` if it was dropped.
        """
        self._guard_publish(event)
        try:
            return self._dispatcher.dispatch(event)
        except EventDispatchError:
            raise
        except Exception as exc:  # noqa: BLE001 - uniform routing
            raise EventPublishError(
                f"Publish failed for {event.name!r}", cause=exc
            ) from exc

    async def publish_async(self, event: Event) -> Optional[Event]:
        """Validate and dispatch ``event`` on the asyncio loop."""
        self._guard_publish(event)
        try:
            return await self._dispatcher.dispatch_async(event)
        except EventDispatchError:
            raise
        except Exception as exc:  # noqa: BLE001 - uniform routing
            raise EventPublishError(
                f"Async publish failed for {event.name!r}", cause=exc
            ) from exc

    def emit(
        self,
        name: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        category: EventCategory = EventCategory.SYSTEM,
        source: Optional[str] = None,
        priority: Optional[EventPriority] = None,
        delivery_mode: EventDeliveryMode = EventDeliveryMode.ASYNC,
        **payload_kwargs: Any,
    ) -> Optional[Event]:
        """Convenience: build an event and publish it synchronously."""
        event = Event(
            name=name,
            payload={**(payload or {}), **payload_kwargs},
            category=category,
            source=source,
            priority=priority,
            delivery_mode=delivery_mode,
        )
        return self.publish(event)

    def _guard_publish(self, event: Event) -> None:
        if not self._running:
            raise EventPublishError(
                f"Event bus is not running; cannot publish {event.name!r}"
            )
        if self._strict and not self._registry.is_known(event.name):
            raise UnknownEventTypeError(f"Unknown event name: {event.name!r}")

    # ------------------------------------------------------------ publishers
    def publisher(self, source: str) -> ScopedPublisher:
        """Return a source-bound :class:`ScopedPublisher` for a feature group."""
        return ScopedPublisher(self._sink, source=source, logger=self._logger)

    def create_publisher(self, source: Optional[str] = None) -> Publisher:
        """Return a general :class:`Publisher` with an optional default source."""
        return Publisher(self._sink, default_source=source, logger=self._logger)

    def _sink(self, event: Event) -> Optional[Event]:
        """Publisher → bus bridge (matches the Publisher ``EventSink`` type)."""
        return self.publish(event)

    # ----------------------------------------------------------- subscribers
    def subscribe(
        self,
        handler: EventHandler,
        *,
        event_filter: Optional[EventFilter] = None,
        priority: int = 0,
        once: bool = False,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
        weak: bool = False,
        name: Optional[str] = None,
    ) -> Subscriber:
        """Register ``handler`` and return the created :class:`Subscriber`."""
        subscriber = Subscriber(
            handler,
            event_filter=event_filter or AcceptAllFilter(),
            priority=priority,
            once=once,
            error_policy=error_policy,
            weak=weak,
            name=name,
            logger=self._logger,
        )
        return self._dispatcher.add_subscriber(subscriber)

    def on(self, *names: str, **kwargs: Any) -> Callable[[EventHandler], Subscriber]:
        """Decorator form: subscribe a handler to one or more event names.

            @bus.on(VoiceEvent.WAKE_TRIGGERED.value)
            def handle_wake(event): ...
        """
        from app.core.event_bus.event_filter import NameFilter

        def decorator(handler: EventHandler) -> Subscriber:
            return self.subscribe(handler, event_filter=NameFilter(*names), **kwargs)

        return decorator

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<EventBus running={self._running} strict={self._strict} "
            f"subscribers={self._dispatcher.subscriber_count}>"
        )


def register_event_bus(
    container: Container,
    *,
    store: Optional[EventStore] = None,
    strict: bool = False,
    start: bool = True,
) -> EventBus:
    """Create an :class:`EventBus`, register it as a singleton, and return it.

    Reuses the :class:`LoggerFactory` already registered in the container so the
    bus logs through the same cached registry as the rest of the system.
    """
    factory = container.try_resolve(LoggerFactory) or LoggerFactory()
    bus = EventBus(store=store, strict=strict, logger_factory=factory)
    if start:
        bus.start()
    container.register_instance(EventBus, bus, replace=True)
    return bus
