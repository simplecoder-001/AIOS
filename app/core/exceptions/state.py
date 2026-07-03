# app/core/exceptions/state.py
"""
State-management exceptions.

Raised by ``app/core/state_manager`` and every subsystem state machine (voice,
brain, security, GUI). The AIOS design is heavily state-driven: subsystems
subscribe to a central State Manager and react to transitions rather than
calling each other directly. That makes illegal transitions and corrupted
state snapshots serious integrity problems, handled here.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "StateError",
    "InvalidStateTransitionError",
    "InvalidStateError",
    "StatePersistenceError",
    "StateSnapshotError",
    "StateValidationError",
    "StateLockError",
]


class StateError(AIOSError):
    """Base class for all state-management failures."""

    default_category = ErrorCategory.STATE
    default_severity = ErrorSeverity.ERROR


class InvalidStateTransitionError(StateError):
    """An attempt was made to move between two states that is not permitted.

    Non-recoverable at the call site: the transition table is static, so an
    illegal transition is a logic defect, not a transient condition. The
    subsystem should reject it and stay in its current state.
    """

    def __init__(
        self,
        from_state: Any,
        to_state: Any,
        *,
        allowed: Optional[Iterable[Any]] = None,
        **kwargs: Any,
    ) -> None:
        allowed_list = [str(s) for s in allowed] if allowed is not None else None
        super().__init__(
            f"Illegal state transition: {from_state} -> {to_state}",
            code="STATE_INVALID_TRANSITION",
            recoverable=False,
            **kwargs,
        )
        self.with_context(
            from_state=str(from_state),
            to_state=str(to_state),
            allowed=allowed_list,
        )


class InvalidStateError(StateError):
    """An operation was requested that is not valid in the current state.

    Example: starting STT while the voice subsystem is DEAUTHORIZED, or
    executing a tool while the brain is in ERROR.
    """

    def __init__(self, current_state: Any, operation: str, **kwargs: Any) -> None:
        super().__init__(
            f"Operation '{operation}' is not allowed in state '{current_state}'",
            code="STATE_INVALID_OPERATION",
            **kwargs,
        )
        self.with_context(current_state=str(current_state), operation=operation)


class StatePersistenceError(StateError):
    """Failed to persist or reload state to/from durable storage.

    Recoverable: the in-memory state is still authoritative; persistence can
    be retried. Elevated to CRITICAL because losing persistence undermines
    crash recovery.
    """

    def __init__(self, operation: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"State {operation} failed",
            code="STATE_PERSISTENCE_ERROR",
            severity=ErrorSeverity.CRITICAL,
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation)


class StateSnapshotError(StateError):
    """A state snapshot could not be created or restored.

    Directly impacts the Recovery Manager, which rebuilds subsystem state and
    the action queue from snapshots after a crash.
    """

    def __init__(self, operation: str = "snapshot", cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"State {operation} operation failed",
            code="STATE_SNAPSHOT_ERROR",
            severity=ErrorSeverity.CRITICAL,
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation)


class StateValidationError(StateError):
    """A state object failed integrity validation (e.g. corrupted on load)."""

    def __init__(self, reason: str, **kwargs: Any) -> None:
        super().__init__(
            f"State validation failed: {reason}",
            code="STATE_VALIDATION_ERROR",
            **kwargs,
        )
        self.with_context(reason=reason)


class StateLockError(StateError):
    """Failed to acquire the state lock within the allotted time.

    The State Manager is read/written by many worker threads (audio, wake,
    STT, TTS, planner). A lock timeout usually signals a deadlock or a stuck
    holder and is surfaced rather than silently blocking forever.
    """

    def __init__(self, timeout_seconds: Optional[float] = None, **kwargs: Any) -> None:
        detail = f" after {timeout_seconds}s" if timeout_seconds is not None else ""
        super().__init__(
            f"Failed to acquire state lock{detail}",
            code="STATE_LOCK_ERROR",
            severity=ErrorSeverity.CRITICAL,
            **kwargs,
        )
        self.with_context(timeout_seconds=timeout_seconds)
