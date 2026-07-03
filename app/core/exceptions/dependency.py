# app/core/exceptions/dependency.py
"""
Dependency-injection exceptions.

Raised by ``app/dependency_injection`` (container, providers, factories,
scopes) and the service/module registries when a service cannot be resolved,
registered, or constructed. The DI container is the backbone of Phase 0 wiring,
so these failures are typically startup-critical: if a required service cannot
be built, the subsystem depending on it must not run in a half-initialized
state.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "DependencyError",
    "DependencyNotFoundError",
    "DependencyResolutionError",
    "CircularDependencyError",
    "DuplicateRegistrationError",
    "ProviderError",
    "ScopeError",
]


class DependencyError(AIOSError):
    """Base class for all dependency-injection failures."""

    default_category = ErrorCategory.DEPENDENCY
    default_severity = ErrorSeverity.CRITICAL


class DependencyNotFoundError(DependencyError):
    """A requested service/token is not registered in the container."""

    def __init__(self, token: Any, **kwargs: Any) -> None:
        super().__init__(
            f"No registered dependency for token: {token!r}",
            code="DI_NOT_FOUND",
            **kwargs,
        )
        self.with_context(token=repr(token))


class DependencyResolutionError(DependencyError):
    """A dependency is registered but failed to construct.

    Usually wraps the exception raised inside a provider/factory during
    instantiation, preserved via ``cause`` for forensics.
    """

    def __init__(self, token: Any, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Failed to resolve dependency: {token!r}",
            code="DI_RESOLUTION_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(token=repr(token))


class CircularDependencyError(DependencyError):
    """A dependency cycle was detected during resolution.

    Non-recoverable: a cycle is a static wiring defect that retrying cannot
    fix. The offending resolution chain is captured for debugging.
    """

    def __init__(self, chain: Iterable[Any], **kwargs: Any) -> None:
        chain_list = [repr(item) for item in chain]
        path = " -> ".join(chain_list) if chain_list else "<unknown>"
        super().__init__(
            f"Circular dependency detected: {path}",
            code="DI_CIRCULAR_DEPENDENCY",
            recoverable=False,
            **kwargs,
        )
        self.with_context(chain=chain_list)


class DuplicateRegistrationError(DependencyError):
    """An attempt was made to register a token that already exists.

    Non-recoverable by default: silent overwrites of service bindings are a
    common source of subtle bugs, so the container should reject them unless
    the caller explicitly opts into replacement.
    """

    def __init__(self, token: Any, **kwargs: Any) -> None:
        super().__init__(
            f"Dependency already registered for token: {token!r}",
            code="DI_DUPLICATE_REGISTRATION",
            recoverable=False,
            **kwargs,
        )
        self.with_context(token=repr(token))


class ProviderError(DependencyError):
    """A provider/factory is misconfigured or returned an invalid instance."""

    def __init__(self, token: Any, reason: Optional[str] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Provider error for token {token!r}{suffix}",
            code="DI_PROVIDER_ERROR",
            **kwargs,
        )
        self.with_context(token=repr(token), reason=reason)


class ScopeError(DependencyError):
    """A service was requested from an invalid or inactive scope.

    Example: resolving a request-scoped service outside an active request
    scope, or mixing singleton and transient lifetimes incorrectly.
    """

    def __init__(self, token: Any, scope: str, reason: Optional[str] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Scope error resolving {token!r} in scope '{scope}'{suffix}",
            code="DI_SCOPE_ERROR",
            **kwargs,
        )
        self.with_context(token=repr(token), scope=scope, reason=reason)
