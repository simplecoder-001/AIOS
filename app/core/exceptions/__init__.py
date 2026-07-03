# app/core/exceptions/__init__.py
"""
AIOS core exception package — public API.

This package defines the entire structured exception hierarchy for the system.
Every custom exception derives from :class:`AIOSError` (in ``base``) and carries
a stable ``code``, an :class:`ErrorSeverity`, an :class:`ErrorCategory`, and a
serializable context payload, so errors flow uniformly into the Event Bus,
audit logs, telemetry, and the Recovery Manager.

Usage
-----
Import directly from the package root, not the submodules:

    from app.core.exceptions import (
        AIOSError,
        ConfigValidationError,
        PermissionDeniedError,
        ToolExecutionError,
    )

Design
------
* ``base`` has zero project dependencies and is import-safe at the very first
  bootstrap step.
* Every domain module depends only on ``base``, so importing this package can
  never create a circular import with the subsystems that raise these errors.
* ``get_exception_for_category`` maps an :class:`ErrorCategory` back to its
  base class, which the Event Bus / Recovery Manager can use for generic
  handling and routing.
"""

from __future__ import annotations

from typing import Type

# --- Foundation -----------------------------------------------------------
from app.core.exceptions.base import (
    AIOSError,
    ErrorCategory,
    ErrorSeverity,
)

# --- Configuration --------------------------------------------------------
from app.core.exceptions.configuration import (
    ConfigFileNotFoundError,
    ConfigMergeError,
    ConfigParseError,
    ConfigValidationError,
    ConfigurationError,
    EnvironmentVariableError,
    InvalidConfigValueError,
    MissingConfigKeyError,
)

# --- Dependency injection -------------------------------------------------
from app.core.exceptions.dependency import (
    CircularDependencyError,
    DependencyError,
    DependencyNotFoundError,
    DependencyResolutionError,
    DuplicateRegistrationError,
    ProviderError,
    ScopeError,
)

# --- Database -------------------------------------------------------------
from app.core.exceptions.database import (
    BackupError,
    ConnectionError,
    DatabaseError,
    EncryptionKeyError,
    IntegrityError,
    KnowledgeGraphError,
    MigrationError,
    QueryError,
    TransactionError,
    VectorStoreError,
)

# --- State ----------------------------------------------------------------
from app.core.exceptions.state import (
    InvalidStateError,
    InvalidStateTransitionError,
    StateError,
    StateLockError,
    StatePersistenceError,
    StateSnapshotError,
    StateValidationError,
)

# --- Event bus ------------------------------------------------------------
from app.core.exceptions.event import (
    EventDispatchError,
    EventError,
    EventHandlerError,
    EventPublishError,
    EventSerializationError,
    EventSubscriptionError,
    UnknownEventTypeError,
)

# --- Queues ---------------------------------------------------------------
from app.core.exceptions.queue import (
    InvalidPriorityError,
    QueueClosedError,
    QueueEmptyError,
    QueueError,
    QueueFullError,
    QueueInterruptedError,
    QueueOverflowError,
    QueueTimeoutError,
)

# --- Security -------------------------------------------------------------
from app.core.exceptions.security import (
    AuditIntegrityError,
    AuthenticationError,
    AuthorizationError,
    EncryptionError,
    FirewallBlockedError,
    PermissionDeniedError,
    PromptInjectionError,
    RiskThresholdExceededError,
    SandboxViolationError,
    SecurityError,
    SpeakerVerificationError,
)

# --- Validation -----------------------------------------------------------
from app.core.exceptions.validation import (
    ConstraintViolationError,
    MissingFieldError,
    ParameterValidationError,
    SchemaValidationError,
    ToolValidationError,
    TypeCoercionError,
    ValidationError,
)

# --- Startup / lifecycle --------------------------------------------------
from app.core.exceptions.startup import (
    BootstrapError,
    FeatureGroupInitError,
    InitializationError,
    PhaseInitializationError,
    ServiceStartupError,
    ShutdownError,
    StartupError,
    StartupTimeoutError,
)

# --- Runtime --------------------------------------------------------------
from app.core.exceptions.runtime import (
    ExternalServiceError,
    ModelInferenceError,
    NotSupportedError,
    OperationError,
    RecoveryError,
    ResourceExhaustedError,
    RetryExhaustedError,
    RuntimeError_,
    TimeoutError_,
    ToolExecutionError,
)


# --------------------------------------------------------------------------- #
# Category -> base-class registry (for generic handling / routing)
# --------------------------------------------------------------------------- #
_CATEGORY_BASE: dict[ErrorCategory, Type[AIOSError]] = {
    ErrorCategory.CONFIGURATION: ConfigurationError,
    ErrorCategory.DEPENDENCY: DependencyError,
    ErrorCategory.DATABASE: DatabaseError,
    ErrorCategory.STATE: StateError,
    ErrorCategory.EVENT: EventError,
    ErrorCategory.QUEUE: QueueError,
    ErrorCategory.SECURITY: SecurityError,
    ErrorCategory.VALIDATION: ValidationError,
    ErrorCategory.STARTUP: StartupError,
    ErrorCategory.RUNTIME: RuntimeError_,
    ErrorCategory.UNKNOWN: AIOSError,
}


def get_exception_for_category(category: ErrorCategory) -> Type[AIOSError]:
    """Return the base exception class associated with a category.

    Used by the Event Bus and Recovery Manager to reason about errors
    generically (e.g. "is this a SECURITY error?") without importing every
    concrete subclass.
    """
    return _CATEGORY_BASE.get(category, AIOSError)


def is_fatal(exc: BaseException) -> bool:
    """Convenience predicate: True if ``exc`` is a fatal AIOS error.

    Non-AIOS exceptions are treated conservatively as non-fatal here; the
    global handler decides how to wrap them.
    """
    return isinstance(exc, AIOSError) and exc.is_fatal()


def wrap_exception(
    exc: BaseException,
    *,
    category: ErrorCategory = ErrorCategory.RUNTIME,
    message: str | None = None,
) -> AIOSError:
    """Wrap an arbitrary exception into an :class:`AIOSError` subclass.

    Already-AIOS exceptions are returned unchanged. Everything else is wrapped
    in the category's base class, preserving the original via ``cause`` so the
    global error handler always deals with a uniform, structured error type.
    """
    if isinstance(exc, AIOSError):
        return exc
    base_cls = get_exception_for_category(category)
    return base_cls(
        message or f"Unhandled {type(exc).__name__}: {exc}",
        cause=exc,
    )


__all__ = [
    # foundation
    "AIOSError",
    "ErrorCategory",
    "ErrorSeverity",
    # configuration
    "ConfigurationError",
    "ConfigFileNotFoundError",
    "ConfigParseError",
    "ConfigValidationError",
    "MissingConfigKeyError",
    "InvalidConfigValueError",
    "EnvironmentVariableError",
    "ConfigMergeError",
    # dependency
    "DependencyError",
    "DependencyNotFoundError",
    "DependencyResolutionError",
    "CircularDependencyError",
    "DuplicateRegistrationError",
    "ProviderError",
    "ScopeError",
    # database
    "DatabaseError",
    "ConnectionError",
    "TransactionError",
    "MigrationError",
    "QueryError",
    "IntegrityError",
    "BackupError",
    "EncryptionKeyError",
    "VectorStoreError",
    "KnowledgeGraphError",
    # state
    "StateError",
    "InvalidStateTransitionError",
    "InvalidStateError",
    "StatePersistenceError",
    "StateSnapshotError",
    "StateValidationError",
    "StateLockError",
    # event
    "EventError",
    "EventPublishError",
    "EventSubscriptionError",
    "EventHandlerError",
    "EventSerializationError",
    "UnknownEventTypeError",
    "EventDispatchError",
    # queue
    "QueueError",
    "QueueFullError",
    "QueueEmptyError",
    "QueueTimeoutError",
    "QueueClosedError",
    "InvalidPriorityError",
    "QueueInterruptedError",
    "QueueOverflowError",
    # security
    "SecurityError",
    "AuthenticationError",
    "AuthorizationError",
    "PermissionDeniedError",
    "SpeakerVerificationError",
    "RiskThresholdExceededError",
    "FirewallBlockedError",
    "PromptInjectionError",
    "SandboxViolationError",
    "EncryptionError",
    "AuditIntegrityError",
    # validation
    "ValidationError",
    "SchemaValidationError",
    "ParameterValidationError",
    "TypeCoercionError",
    "ConstraintViolationError",
    "MissingFieldError",
    "ToolValidationError",
    # startup
    "StartupError",
    "InitializationError",
    "BootstrapError",
    "ServiceStartupError",
    "FeatureGroupInitError",
    "PhaseInitializationError",
    "StartupTimeoutError",
    "ShutdownError",
    # runtime
    "RuntimeError_",
    "OperationError",
    "TimeoutError_",
    "RetryExhaustedError",
    "ResourceExhaustedError",
    "ToolExecutionError",
    "ModelInferenceError",
    "ExternalServiceError",
    "NotSupportedError",
    "RecoveryError",
    # helpers
    "get_exception_for_category",
    "is_fatal",
    "wrap_exception",
]
