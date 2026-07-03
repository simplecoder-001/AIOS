# app/core/exceptions/base.py
"""
Base exception hierarchy for the entire AIOS project.

Every custom exception in the system derives from :class:`AIOSError`. The base
carries structured metadata (error code, severity, category, context, cause)
so exceptions can flow uniformly into the Event Bus, audit logs, telemetry, and
the Recovery Manager instead of being opaque strings.

Dependency order
----------------
This is the ROOT of the exceptions package. It imports nothing from other
project modules (only the standard library), so it is safe to import during the
earliest Phase 0 bootstrap, before logging or the event bus exist.
"""

from __future__ import annotations

import enum
import traceback
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

__all__ = [
    "ErrorSeverity",
    "ErrorCategory",
    "AIOSError",
]


class ErrorSeverity(str, enum.Enum):
    """How serious an error is, used for routing and escalation.

    Inherits ``str`` so it serializes cleanly into JSON audit records.
    """

    DEBUG = "debug"            # diagnostic only, non-actionable
    INFO = "info"             # noteworthy but harmless
    WARNING = "warning"       # degraded, system continues
    ERROR = "error"           # operation failed, recoverable
    CRITICAL = "critical"     # subsystem failure, may need recovery
    FATAL = "fatal"           # unrecoverable, triggers safe shutdown


class ErrorCategory(str, enum.Enum):
    """Broad classification aligning with the core subsystem that raised it."""

    CONFIGURATION = "configuration"
    DEPENDENCY = "dependency"
    DATABASE = "database"
    STATE = "state"
    EVENT = "event"
    QUEUE = "queue"
    SECURITY = "security"
    VALIDATION = "validation"
    STARTUP = "startup"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"


class AIOSError(Exception):
    """Root exception for all AIOS-specific errors.

    Parameters
    ----------
    message:
        Human-readable description of what went wrong.
    code:
        Stable, machine-readable identifier (e.g. ``"CONFIG_MISSING_KEY"``).
        Defaults to the class name upper-cased if not supplied.
    severity:
        A :class:`ErrorSeverity`. Defaults to ``ERROR``.
    category:
        A :class:`ErrorCategory`. Subclasses set a sensible default.
    context:
        Arbitrary structured detail (never include secrets/PII here; the FG6
        firewall does not run over exception context).
    cause:
        The originating exception, preserved for chaining and forensics.
    recoverable:
        Hint to the Recovery Manager whether a retry/rollback may help.
    """

    # Subclasses override these class-level defaults.
    default_severity: ErrorSeverity = ErrorSeverity.ERROR
    default_category: ErrorCategory = ErrorCategory.UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        severity: Optional[ErrorSeverity] = None,
        category: Optional[ErrorCategory] = None,
        context: Optional[Mapping[str, Any]] = None,
        cause: Optional[BaseException] = None,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__.upper()
        self.severity = severity or self.default_severity
        self.category = category or self.default_category
        self.context: dict[str, Any] = dict(context) if context else {}
        self.cause = cause
        self.recoverable = recoverable
        self.timestamp = datetime.now(timezone.utc)

        # Preserve chaining for standard traceback machinery.
        if cause is not None:
            self.__cause__ = cause

    # ------------------------------------------------------------------ API

    def with_context(self, **kwargs: Any) -> "AIOSError":
        """Attach additional context and return self (fluent, chainable)."""
        self.context.update(kwargs)
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for audit logs, telemetry, and events."""
        payload: dict[str, Any] = {
            "code": self.code,
            "type": self.__class__.__name__,
            "message": self.message,
            "severity": self.severity.value,
            "category": self.category.value,
            "recoverable": self.recoverable,
            "timestamp": self.timestamp.isoformat(),
            "context": self.context,
        }
        if self.cause is not None:
            payload["cause"] = {
                "type": type(self.cause).__name__,
                "message": str(self.cause),
            }
        return payload

    def format_traceback(self) -> str:
        """Return the full formatted traceback string for crash logs."""
        return "".join(
            traceback.format_exception(type(self), self, self.__traceback__)
        )

    def is_fatal(self) -> bool:
        """True if this error should trigger a safe shutdown."""
        return self.severity is ErrorSeverity.FATAL

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"{self.__class__.__name__}(code={self.code!r}, "
            f"severity={self.severity.value!r}, category={self.category.value!r}, "
            f"message={self.message!r})"
        )
