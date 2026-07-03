# app/dependency_injection/scopes.py
"""
Dependency Injection Scopes
===========================
Defines the lifetime semantics for resolved dependencies and the
runtime machinery that stores scoped instances.

Three lifetimes are supported:

* SINGLETON : one instance for the entire application lifetime. Created
              lazily on first resolve, then cached forever in the container.
* TRANSIENT : a brand-new instance on every resolve. Never cached.
* SCOPED    : one instance per active :class:`Scope` (e.g. per voice
              session, per request, per task). Cached for the duration of
              the scope, disposed when the scope exits.

Thread safety
-------------
The reference hardware (Ryzen 7 + RTX 5050) runs a heavily multi-threaded
architecture (audio capture, wake word, STT, TTS threads, etc.). Scope
instance stores are therefore guarded by re-entrant locks so a provider that
resolves a nested dependency from the same thread does not deadlock.
"""

from __future__ import annotations

import enum
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.exceptions import ScopeError
from app.logging import Logger

__all__ = [
    "Lifetime",
    "Scope",
    "ScopeManager",
]


class Lifetime(str, enum.Enum):
    """Lifetime policy attached to every registered provider.

    Inherits ``str`` so it serializes cleanly into audit/debug records.
    """

    SINGLETON = "singleton"
    TRANSIENT = "transient"
    SCOPED = "scoped"


class Scope:
    """A disposable container for SCOPED instances.

    A scope owns the instances created within it and the disposal callbacks
    registered against them. Exiting the scope (via context manager or
    :meth:`dispose`) tears every instance down in reverse creation order,
    mirroring the semantics of a stack unwind.

    Scopes are intended to be short-lived and bound to a logical unit of
    work such as a voice session, an AI-brain request, or a task execution.
    """

    def __init__(self, name: str, *, parent: Optional["Scope"] = None) -> None:
        self.name = name
        self.parent = parent
        self._instances: Dict[Any, Any] = {}
        # (token, instance, disposer) preserved in creation order.
        self._disposables: List[Tuple[Any, Any, Callable[[Any], None]]] = []
        self._lock = threading.RLock()
        self._active = True

    # ------------------------------------------------------------------ API
    @property
    def active(self) -> bool:
        """Whether this scope can still store or return instances."""
        return self._active

    def has(self, token: Any) -> bool:
        """True if an instance for ``token`` already exists in this scope."""
        with self._lock:
            return token in self._instances

    def get(self, token: Any) -> Any:
        """Return the cached instance for ``token``.

        Raises:
            ScopeError: If the scope is disposed or the token is absent.
        """
        with self._lock:
            self._ensure_active(token)
            if token not in self._instances:
                raise ScopeError(
                    token,
                    self.name,
                    reason="instance not present in scope",
                )
            return self._instances[token]

    def set(
        self,
        token: Any,
        instance: Any,
        disposer: Optional[Callable[[Any], None]] = None,
    ) -> None:
        """Store ``instance`` for ``token`` and optionally register a disposer.

        The disposer (if any) is invoked with the instance when the scope is
        torn down.
        """
        with self._lock:
            self._ensure_active(token)
            self._instances[token] = instance
            if disposer is not None:
                self._disposables.append((token, instance, disposer))

    def dispose(self) -> None:
        """Tear down every instance in reverse creation order.

        Disposal is best-effort: a failing disposer never prevents the
        remaining instances from being cleaned up. The scope is marked
        inactive and cannot be reused afterwards.
        """
        with self._lock:
            if not self._active:
                return
            self._active = False
            errors: List[str] = []
            for token, instance, disposer in reversed(self._disposables):
                try:
                    disposer(instance)
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    errors.append(f"{token!r}: {exc}")
            self._disposables.clear()
            self._instances.clear()
            if errors:
                # Surface disposal problems without aborting shutdown.
                raise ScopeError(
                    self.name,
                    self.name,
                    reason=f"disposer failures: {'; '.join(errors)}",
                )

    # -------------------------------------------------------------- helpers
    def _ensure_active(self, token: Any) -> None:
        if not self._active:
            raise ScopeError(
                token,
                self.name,
                reason="scope has been disposed",
            )

    # ------------------------------------------------------------- context
    def __enter__(self) -> "Scope":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.dispose()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        state = "active" if self._active else "disposed"
        return f"<Scope name={self.name!r} state={state} items={len(self._instances)}>"


class ScopeManager:
    """Owns the singleton store and the stack of active scopes.

    The container delegates all lifetime bookkeeping to this manager:

    * SINGLETON instances live in :attr:`_singletons` for the process lifetime.
    * SCOPED instances live in the currently active :class:`Scope`.
    * TRANSIENT instances are never stored here.

    A thread-local stack tracks the active scope per thread, so concurrent
    sessions on different worker threads never observe each other's scoped
    instances.
    """

    def __init__(self, logger: Optional[Logger] = None) -> None:
        self._logger = logger
        self._singletons: Dict[Any, Any] = {}
        self._singleton_disposers: List[Tuple[Any, Callable[[Any], None]]] = []
        self._lock = threading.RLock()
        self._local = threading.local()

    # ------------------------------------------------------------ singleton
    def has_singleton(self, token: Any) -> bool:
        with self._lock:
            return token in self._singletons

    def get_singleton(self, token: Any) -> Any:
        with self._lock:
            return self._singletons[token]

    def set_singleton(
        self,
        token: Any,
        instance: Any,
        disposer: Optional[Callable[[Any], None]] = None,
    ) -> None:
        with self._lock:
            self._singletons[token] = instance
            if disposer is not None:
                self._singleton_disposers.append((token, disposer))

    # -------------------------------------------------------- scoped access
    @property
    def _scope_stack(self) -> List[Scope]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    @property
    def current_scope(self) -> Optional[Scope]:
        """The innermost active scope on the current thread, if any."""
        stack = self._scope_stack
        return stack[-1] if stack else None

    def create_scope(self, name: str) -> Scope:
        """Create a new scope nested under the current thread's active scope.

        The scope must still be entered (``with`` or pushed) to become the
        active resolution scope.
        """
        return Scope(name, parent=self.current_scope)

    def push_scope(self, scope: Scope) -> None:
        """Make ``scope`` the active scope on the current thread."""
        self._scope_stack.append(scope)
        if self._logger:
            self._logger.debug("Scope pushed", extra={"scope": scope.name})

    def pop_scope(self) -> Scope:
        """Remove and return the active scope from the current thread.

        Raises:
            ScopeError: If no scope is active on this thread.
        """
        stack = self._scope_stack
        if not stack:
            raise ScopeError("<none>", "current", reason="no active scope to pop")
        scope = stack.pop()
        if self._logger:
            self._logger.debug("Scope popped", extra={"scope": scope.name})
        return scope

    def require_current_scope(self, token: Any) -> Scope:
        """Return the active scope or raise if resolution is out of scope.

        Used by SCOPED providers, which are invalid outside an active scope.
        """
        scope = self.current_scope
        if scope is None or not scope.active:
            raise ScopeError(
                token,
                "scoped",
                reason="no active scope; resolve a scoped service inside a Scope",
            )
        return scope

    # ---------------------------------------------------------------- reset
    def dispose_singletons(self) -> None:
        """Dispose every singleton in reverse registration order.

        Called during application shutdown after all feature groups stop.
        Best-effort: a failing disposer is logged but never aborts teardown.
        """
        with self._lock:
            for token, disposer in reversed(self._singleton_disposers):
                try:
                    disposer(self._singletons.get(token))
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    if self._logger:
                        self._logger.error(
                            "Singleton disposer failed",
                            extra={"token": repr(token), "error": str(exc)},
                        )
            self._singleton_disposers.clear()
            self._singletons.clear()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<ScopeManager singletons={len(self._singletons)} "
            f"active_scope={self.current_scope!r}>"
        )
