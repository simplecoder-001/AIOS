# app/core/exceptions/event.py
"""
Event-bus exceptions.

Raised by ``app/core/event_bus`` (bus, publisher, subscriber, dispatcher,
middleware, registry, store, serializer, filter). The Event Bus is the
system-wide nervous system: subsystems communicate through published events
instead of direct calls. Failures here are usually degraded-mode conditions
(a single handler failing should not crash the bus) but can escalate when the
dispatcher or store itself is unhealthy.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "EventError",
    "EventPublishError",
    "EventSubscriptionError",
    "EventHandlerError",
    "EventSerializationError",
    "UnknownEventTypeError",
    "EventDispatchError",
]


class EventError(AIOSError):
    """Base class for all event-bus failures."""

    default_category = ErrorCategory.EVENT
    default_severity = ErrorSeverity.ERROR


class EventPublishError(EventError):
    """An event could not be published to the bus (e.g. queue full/closed)."""

    def __init__(self, event_type: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Failed to publish event '{event_type}'",
            code="EVENT_PUBLISH_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(event_type=event_type)


class EventSubscriptionError(EventError):
    """A subscription could not be registered or removed."""

    def __init__(self, event_type: str, reason: Optional[str] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Event subscription failed for '{event_type}'{suffix}",
            code="EVENT_SUBSCRIPTION_ERROR",
            **kwargs,
        )
        self.with_context(event_type=event_type, reason=reason)


class EventHandlerError(EventError):
    """A subscriber's handler raised while processing an event.

    Recoverable and intentionally low-to-normal severity: the dispatcher
    isolates handler failures so one bad subscriber cannot bring down the bus
    or starve other handlers. The original error is preserved via ``cause``.
    """

    def __init__(
        self,
        event_type: str,
        handler: Any,
        cause: Optional[BaseException] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            f"Handler {handler!r} failed while processing event '{event_type}'",
            code="EVENT_HANDLER_ERROR",
            severity=ErrorSeverity.WARNING,
            cause=cause,
            **kwargs,
        )
        self.with_context(event_type=event_type, handler=repr(handler))


class EventSerializationError(EventError):
    """An event could not be serialized/deserialized for the event store."""

    def __init__(self, event_type: str, operation: str = "serialize", cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Failed to {operation} event '{event_type}'",
            code="EVENT_SERIALIZATION_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(event_type=event_type, operation=operation)


class UnknownEventTypeError(EventError):
    """An event referenced a type that is not in the event registry."""

    def __init__(self, event_type: str, **kwargs: Any) -> None:
        super().__init__(
            f"Unknown event type: '{event_type}'",
            code="EVENT_UNKNOWN_TYPE",
            **kwargs,
        )
        self.with_context(event_type=event_type)


class EventDispatchError(EventError):
    """The dispatcher itself failed (thread pool, middleware, routing).

    Elevated to CRITICAL: unlike a single handler failing, a broken dispatcher
    means events stop flowing and the whole event-driven architecture stalls.
    """

    def __init__(self, reason: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Event dispatch failure{suffix}",
            code="EVENT_DISPATCH_ERROR",
            severity=ErrorSeverity.CRITICAL,
            cause=cause,
            **kwargs,
        )
        self.with_context(reason=reason)
