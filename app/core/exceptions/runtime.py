# app/core/exceptions/runtime.py
"""
Runtime exceptions.

General-purpose operational errors that occur after boot, during normal
operation, and do not belong to a more specific category (config, dependency,
database, state, event, queue, security, validation, startup). Covers tool
execution, model inference, external providers, resource limits, timeouts,
retries, and recovery.

Unlike startup errors, most runtime errors are recoverable: the Recovery
Manager, retry policies, and fallback selectors are designed to catch these
and retry, roll back, or degrade gracefully.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
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
]


class RuntimeError_(AIOSError):
    """Base class for all post-boot runtime failures.

    Named with a trailing underscore to avoid shadowing the builtin
    ``RuntimeError``; exported and imported explicitly by callers.
    """

    default_category = ErrorCategory.RUNTIME
    default_severity = ErrorSeverity.ERROR


class OperationError(RuntimeError_):
    """A named operation failed for a general reason."""

    def __init__(self, operation: str, reason: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Operation '{operation}' failed{suffix}",
            code="RUNTIME_OPERATION_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation, reason=reason)


class TimeoutError_(RuntimeError_):
    """A runtime operation exceeded its allotted time.

    Trailing underscore avoids shadowing the builtin ``TimeoutError``.
    """

    def __init__(self, operation: str, timeout_seconds: Optional[float] = None, **kwargs: Any) -> None:
        detail = f" after {timeout_seconds}s" if timeout_seconds is not None else ""
        super().__init__(
            f"Operation '{operation}' timed out{detail}",
            code="RUNTIME_TIMEOUT",
            severity=ErrorSeverity.WARNING,
            **kwargs,
        )
        self.with_context(operation=operation, timeout_seconds=timeout_seconds)


class RetryExhaustedError(RuntimeError_):
    """All retry attempts for an operation were exhausted.

    Non-recoverable at this layer: retries already happened. The last
    underlying failure is preserved via ``cause``.
    """

    def __init__(self, operation: str, attempts: int, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Operation '{operation}' failed after {attempts} attempt(s)",
            code="RUNTIME_RETRY_EXHAUSTED",
            recoverable=False,
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation, attempts=attempts)


class ResourceExhaustedError(RuntimeError_):
    """A resource limit was hit (RAM, VRAM, disk, handles).

    Ties into FG telemetry monitors (psutil / NVML). Elevated to CRITICAL
    because resource exhaustion can cascade across subsystems.
    """

    def __init__(self, resource: str, detail: Optional[str] = None, **kwargs: Any) -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"Resource exhausted: {resource}{suffix}",
            code="RUNTIME_RESOURCE_EXHAUSTED",
            severity=ErrorSeverity.CRITICAL,
            **kwargs,
        )
        self.with_context(resource=resource, detail=detail)


class ToolExecutionError(RuntimeError_):
    """A tool failed during execution (FG2 tool manager / FG3 execution).

    Feeds the execution-verification / rollback flow.
    """

    def __init__(self, tool: str, reason: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Tool '{tool}' execution failed{suffix}",
            code="RUNTIME_TOOL_EXECUTION_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(tool=tool, reason=reason)


class ModelInferenceError(RuntimeError_):
    """An LLM/model inference call failed (local Gemma or cloud Groq).

    Recoverable: the router/fallback manager may switch models (e.g. cloud ->
    local on network loss), so this defaults to recoverable.
    """

    def __init__(self, model: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Model inference failed for '{model}'",
            code="RUNTIME_MODEL_INFERENCE_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(model=model)


class ExternalServiceError(RuntimeError_):
    """An external provider call failed (search providers, cloud APIs).

    Supports the FG2 search fallback chain (Tavily -> Brave -> DuckDuckGo ->
    Gemini grounding); a single provider failing should trigger fallback.
    """

    def __init__(self, service: str, status_code: Optional[int] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        detail = f" (status={status_code})" if status_code is not None else ""
        super().__init__(
            f"External service '{service}' call failed{detail}",
            code="RUNTIME_EXTERNAL_SERVICE_ERROR",
            severity=ErrorSeverity.WARNING,
            cause=cause,
            **kwargs,
        )
        self.with_context(service=service, status_code=status_code)


class NotSupportedError(RuntimeError_):
    """A requested capability/feature is not supported or not enabled.

    Non-recoverable: retrying an unsupported operation cannot succeed.
    """

    def __init__(self, feature: str, reason: Optional[str] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Not supported: {feature}{suffix}",
            code="RUNTIME_NOT_SUPPORTED",
            recoverable=False,
            **kwargs,
        )
        self.with_context(feature=feature, reason=reason)


class RecoveryError(RuntimeError_):
    """The Recovery Manager itself failed to restore a subsystem.

    FATAL: if recovery fails, the system cannot self-heal and must fall back
    to a safe shutdown / user notification.
    """

    def __init__(self, subsystem: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Recovery failed for subsystem '{subsystem}'",
            code="RUNTIME_RECOVERY_ERROR",
            severity=ErrorSeverity.FATAL,
            recoverable=False,
            cause=cause,
            **kwargs,
        )
        self.with_context(subsystem=subsystem)
