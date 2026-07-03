# app/core/exceptions/queue.py
"""
Queue-system exceptions.

Raised by ``app/core/queues`` (queue manager, priority/broadcast/task/event/
message/interrupt/retry queues). Queues are central to AIOS: FG1 uses a
broadcast audio model fanning into wake/verification/STT/interrupt queues, and
FG3 uses a priority-aware action queue (Critical/High/Normal/Low). Overflow,
timeouts, and interrupt handling are first-class failure modes here.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "QueueError",
    "QueueFullError",
    "QueueEmptyError",
    "QueueTimeoutError",
    "QueueClosedError",
    "InvalidPriorityError",
    "QueueInterruptedError",
]


class QueueError(AIOSError):
    """Base class for all queue failures."""

    default_category = ErrorCategory.QUEUE
    default_severity = ErrorSeverity.ERROR


class QueueFullError(QueueError):
    """An item could not be enqueued because the queue is at capacity.

    Recoverable: producers may apply back-pressure, drop, or retry. Elevated
    to WARNING because sustained fullness signals a consumer bottleneck worth
    surfacing (e.g. audio broadcast outpacing STT).
    """

    def __init__(self, queue_name: str, maxsize: Optional[int] = None, **kwargs: Any) -> None:
        detail = f" (maxsize={maxsize})" if maxsize is not None else ""
        super().__init__(
            f"Queue '{queue_name}' is full{detail}",
            code="QUEUE_FULL",
            severity=ErrorSeverity.WARNING,
            **kwargs,
        )
        self.with_context(queue_name=queue_name, maxsize=maxsize)


class QueueEmptyError(QueueError):
    """A non-blocking get found the queue empty.

    Typically an expected control-flow signal rather than a true error, so it
    defaults to DEBUG severity.
    """

    def __init__(self, queue_name: str, **kwargs: Any) -> None:
        super().__init__(
            f"Queue '{queue_name}' is empty",
            code="QUEUE_EMPTY",
            severity=ErrorSeverity.DEBUG,
            **kwargs,
        )
        self.with_context(queue_name=queue_name)


class QueueTimeoutError(QueueError):
    """A blocking put/get exceeded its timeout."""

    def __init__(self, queue_name: str, operation: str, timeout_seconds: Optional[float] = None, **kwargs: Any) -> None:
        detail = f" after {timeout_seconds}s" if timeout_seconds is not None else ""
        super().__init__(
            f"Queue '{queue_name}' {operation} timed out{detail}",
            code="QUEUE_TIMEOUT",
            severity=ErrorSeverity.WARNING,
            **kwargs,
        )
        self.with_context(queue_name=queue_name, operation=operation, timeout_seconds=timeout_seconds)


class QueueClosedError(QueueError):
    """An operation was attempted on a closed/shut-down queue.

    Common during shutdown races; non-recoverable for the caller since the
    queue will not reopen, but not fatal to the system.
    """

    def __init__(self, queue_name: str, operation: str = "operation", **kwargs: Any) -> None:
        super().__init__(
            f"Queue '{queue_name}' is closed; cannot perform {operation}",
            code="QUEUE_CLOSED",
            recoverable=False,
            **kwargs,
        )
        self.with_context(queue_name=queue_name, operation=operation)


class InvalidPriorityError(QueueError):
    """A priority value outside the supported levels was supplied.

    Aligns with FG3's four priority levels (Critical/High/Normal/Low).
    """

    def __init__(self, priority: Any, queue_name: Optional[str] = None, **kwargs: Any) -> None:
        where = f" for queue '{queue_name}'" if queue_name else ""
        super().__init__(
            f"Invalid priority {priority!r}{where}",
            code="QUEUE_INVALID_PRIORITY",
            **kwargs,
        )
        self.with_context(priority=repr(priority), queue_name=queue_name)


class QueueInterruptedError(QueueError):
    """A blocked queue operation was aborted by an interrupt/emergency stop.

    Directly supports FG1's interrupt pipeline (Stop/Cancel/Pause), which
    clears queues and returns subsystems to idle. This is an intentional
    control signal, not a defect.
    """

    def __init__(self, queue_name: str, **kwargs: Any) -> None:
        super().__init__(
            f"Queue '{queue_name}' operation interrupted",
            code="QUEUE_INTERRUPTED",
            severity=ErrorSeverity.INFO,
            **kwargs,
        )
        self.with_context(queue_name=queue_name)


class QueueOverflowError(QueueFullError):
    """A bounded queue overflowed and had to drop items.

    Distinct from :class:`QueueFullError`: full means "cannot accept now";
    overflow means "capacity exceeded and data was lost", which is more
    serious and surfaces at ERROR severity for the queue-overflow stress path.
    """

    def __init__(self, queue_name: str, dropped: int = 1, maxsize: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(queue_name=queue_name, maxsize=maxsize, **kwargs)
        self.code = "QUEUE_OVERFLOW"
        self.severity = ErrorSeverity.ERROR
        self.message = f"Queue '{queue_name}' overflowed; dropped {dropped} item(s)"
        self.with_context(dropped=dropped)


__all__.append("QueueOverflowError")
