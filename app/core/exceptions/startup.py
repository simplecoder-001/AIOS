# app/core/exceptions/startup.py
"""
Startup / bootstrap exceptions.

Raised by ``app/bootstrap`` (startup, initializer, lifecycle_manager) and the
``app/architecture`` layer during the Phase 0 boot sequence: architecture setup
-> config -> logging -> dependency injection -> event bus, followed by phased
feature-group initialization (Phase 1..5).

Boot failures are inherently serious: a subsystem that fails to initialize must
NOT be left half-wired. Most of these default to FATAL / non-recoverable so the
launcher can abort cleanly (or trigger the fail-safe path) instead of running a
partially constructed system.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "StartupError",
    "InitializationError",
    "BootstrapError",
    "ServiceStartupError",
    "FeatureGroupInitError",
    "PhaseInitializationError",
    "StartupTimeoutError",
    "ShutdownError",
]


class StartupError(AIOSError):
    """Base class for all startup/bootstrap failures. Fatal by default."""

    default_category = ErrorCategory.STARTUP
    default_severity = ErrorSeverity.FATAL

    def __init__(self, message: str, **kwargs: Any) -> None:
        # A failed boot must not silently continue.
        kwargs.setdefault("recoverable", False)
        super().__init__(message, **kwargs)


class InitializationError(StartupError):
    """A component failed to initialize during boot."""

    def __init__(self, component: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Failed to initialize component '{component}'",
            code="STARTUP_INIT_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(component=component)


class BootstrapError(StartupError):
    """The Phase 0 bootstrap sequence itself failed (before feature groups).

    Covers architecture/context/registry wiring that must exist before any
    other layer can start.
    """

    def __init__(self, stage: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Bootstrap failed at stage '{stage}'",
            code="STARTUP_BOOTSTRAP_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(stage=stage)


class ServiceStartupError(StartupError):
    """A registered service failed to start via its service manager."""

    def __init__(self, service: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Service '{service}' failed to start",
            code="STARTUP_SERVICE_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(service=service)


class FeatureGroupInitError(StartupError):
    """A feature group (FG1..FG10) failed to initialize.

    Whether this is fatal depends on the group: a core group (FG2 brain,
    FG6 security) failing is fatal; an optional group (FG7 plugins, FG9 agents)
    may be degradable. Callers can pass ``recoverable=True`` for optional
    groups to allow the system to boot without them.
    """

    def __init__(
        self,
        feature_group: str,
        cause: Optional[BaseException] = None,
        *,
        optional: bool = False,
        **kwargs: Any,
    ) -> None:
        if optional:
            kwargs.setdefault("severity", ErrorSeverity.WARNING)
            kwargs["recoverable"] = True
        super().__init__(
            f"Feature group '{feature_group}' failed to initialize"
            + (" (optional; continuing degraded)" if optional else ""),
            code="STARTUP_FEATURE_GROUP_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(feature_group=feature_group, optional=optional)


class PhaseInitializationError(StartupError):
    """A whole boot phase (Phase 0..5) failed to complete.

    A phase gates the next one, so a failed phase halts progression.
    """

    def __init__(self, phase: Any, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Initialization phase '{phase}' failed",
            code="STARTUP_PHASE_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(phase=str(phase))


class StartupTimeoutError(StartupError):
    """Startup did not complete within the configured timeout.

    Aligns with ``app.startup_timeout_seconds`` from the config defaults.
    """

    def __init__(self, component: str, timeout_seconds: Optional[float] = None, **kwargs: Any) -> None:
        detail = f" after {timeout_seconds}s" if timeout_seconds is not None else ""
        super().__init__(
            f"Startup of '{component}' timed out{detail}",
            code="STARTUP_TIMEOUT",
            cause=kwargs.pop("cause", None),
            **kwargs,
        )
        self.with_context(component=component, timeout_seconds=timeout_seconds)


class ShutdownError(StartupError):
    """A component failed to shut down cleanly.

    Not fatal (the process is ending anyway) but must be logged for audit and
    to flag resource leaks. Overrides the fatal/non-recoverable base defaults.
    """

    default_severity = ErrorSeverity.WARNING

    def __init__(self, component: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        kwargs.setdefault("severity", ErrorSeverity.WARNING)
        kwargs["recoverable"] = True
        super().__init__(
            f"Component '{component}' failed to shut down cleanly",
            code="STARTUP_SHUTDOWN_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(component=component)
