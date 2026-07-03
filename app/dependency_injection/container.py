# app/dependency_injection/container.py
"""
Dependency Injection Container
==============================
The central registry and resolution engine for AIOS. The container is the
backbone of Phase 0 wiring: every subsystem is registered here and resolved
through a single, consistent, thread-safe entry point.

Responsibilities
----------------
* Register providers against tokens (types or arbitrary hashable keys).
* Resolve dependencies with lifetime semantics (via ScopeManager + providers).
* Detect circular dependencies during resolution (per-thread chain tracking).
* Expose scope creation/entry for SCOPED lifetimes.
* Dispose singletons on shutdown.

Import safety
-------------
Depends only on the DI interfaces/providers/scopes plus the core exceptions
and logging packages, all of which are Phase 0 import-safe.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Type, TypeVar

from app.core.exceptions import (
    CircularDependencyError,
    DependencyNotFoundError,
    DuplicateRegistrationError,
)
from app.dependency_injection.interfaces import IContainer, IProvider
from app.dependency_injection.providers import (
    ClassProvider,
    FactoryProvider,
    InstanceProvider,
)
from app.dependency_injection.scopes import Lifetime, Scope, ScopeManager
from app.logging import Logger

__all__ = ["Container"]

T = TypeVar("T")


class Container(IContainer):
    """Thread-safe DI container with lifetime management and cycle detection.

    Usage
    -----
        container = Container()
        container.register_instance(Config, config)
        container.register_class(EventBus, EventBus, lifetime=Lifetime.SINGLETON)
        container.register_factory(
            Logger, lambda c: c.resolve(LoggerFactory).create_console_logger("app")
        )

        bus = container.resolve(EventBus)

        with container.scope("voice-session") as scope:
            session = container.resolve(VoiceSession)  # SCOPED
    """

    def __init__(self, logger: Optional[Logger] = None) -> None:
        self._logger = logger
        self._providers: Dict[Any, IProvider] = {}
        self._lock = threading.RLock()
        self._scopes = ScopeManager(logger=logger)
        # Per-thread resolution chain used to detect cycles.
        self._local = threading.local()

    # ----------------------------------------------------------- properties
    @property
    def scopes(self) -> ScopeManager:
        """Expose the ScopeManager for providers' lifetime bookkeeping."""
        return self._scopes

    @property
    def _chain(self) -> List[Any]:
        chain = getattr(self._local, "chain", None)
        if chain is None:
            chain = []
            self._local.chain = chain
        return chain

    # ------------------------------------------------------------- register
    def register(self, token: Any, provider: IProvider, *, replace: bool = False) -> None:
        """Register a provider for ``token``.

        Raises:
            DuplicateRegistrationError: If the token exists and ``replace`` is
                False (silent overwrites of bindings are rejected by default).
        """
        with self._lock:
            if token in self._providers and not replace:
                raise DuplicateRegistrationError(token)
            self._providers[token] = provider
        if self._logger:
            self._logger.debug(
                "Provider registered",
                extra={"token": repr(token), "provider": repr(provider)},
            )

    def register_instance(self, token: Any, instance: T, *, replace: bool = False) -> None:
        """Register a pre-built instance (eager singleton)."""
        self.register(token, InstanceProvider(instance), replace=replace)

    def register_factory(
        self,
        token: Any,
        factory: Callable[..., T],
        *,
        lifetime: Lifetime = Lifetime.TRANSIENT,
        disposer: Optional[Callable[[T], None]] = None,
        replace: bool = False,
    ) -> None:
        """Register a factory callable with the given lifetime."""
        self.register(
            token,
            FactoryProvider(token, factory, lifetime=lifetime, disposer=disposer),
            replace=replace,
        )

    def register_class(
        self,
        token: Any,
        cls: Optional[Type[T]] = None,
        *,
        lifetime: Lifetime = Lifetime.SINGLETON,
        disposer: Optional[Callable[[T], None]] = None,
        replace: bool = False,
    ) -> None:
        """Register a class for auto-wired construction.

        When ``cls`` is omitted, ``token`` is assumed to be the concrete class
        and is used as both key and implementation (self-binding).
        """
        implementation = cls or token
        self.register(
            token,
            ClassProvider(token, implementation, lifetime=lifetime, disposer=disposer),
            replace=replace,
        )

    # -------------------------------------------------------------- resolve
    def resolve(self, token: Type[T] | Any) -> T:
        """Resolve an instance for ``token``.

        Raises:
            DependencyNotFoundError: If no provider is registered.
            CircularDependencyError: If a resolution cycle is detected.
            DependencyResolutionError: If provider construction fails.
        """
        with self._lock:
            provider = self._providers.get(token)
        if provider is None:
            raise DependencyNotFoundError(token)

        chain = self._chain
        if token in chain:
            cycle = chain[chain.index(token):] + [token]
            raise CircularDependencyError(cycle)

        chain.append(token)
        try:
            return provider.resolve(self)
        finally:
            chain.pop()

    def try_resolve(self, token: Any, default: Any = None) -> Any:
        """Resolve ``token`` or return ``default`` if it is not registered."""
        if not self.has(token):
            return default
        return self.resolve(token)

    def has(self, token: Any) -> bool:
        """True if a provider is registered for ``token``."""
        with self._lock:
            return token in self._providers

    def unregister(self, token: Any) -> None:
        """Remove a provider registration (does not dispose live instances)."""
        with self._lock:
            self._providers.pop(token, None)

    @property
    def registered_tokens(self) -> List[Any]:
        with self._lock:
            return list(self._providers.keys())

    # ----------------------------------------------------------- scope API
    def create_scope(self, name: str) -> Scope:
        """Create (but do not enter) a new scope for SCOPED resolutions."""
        return self._scopes.create_scope(name)

    @contextmanager
    def scope(self, name: str) -> Iterator[Scope]:
        """Context manager that opens, activates, and disposes a scope.

            with container.scope("request-42") as s:
                svc = container.resolve(RequestService)
        """
        scope = self._scopes.create_scope(name)
        self._scopes.push_scope(scope)
        try:
            with scope:
                yield scope
        finally:
            self._scopes.pop_scope()

    # ------------------------------------------------------------- shutdown
    def dispose(self) -> None:
        """Dispose all singletons and clear registrations.

        Call during application shutdown after every feature group has stopped.
        """
        if self._logger:
            self._logger.info("Disposing DI container")
        self._scopes.dispose_singletons()
        with self._lock:
            self._providers.clear()

    # --------------------------------------------------------------- dunder
    def __contains__(self, token: Any) -> bool:
        return self.has(token)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Container providers={len(self._providers)}>"
