# app/core/database/transaction_manager.py
"""
Transactional coordination for the AIOS database layer.

While :class:`SessionManager` provides the *connection + savepoint stack*
primitives, this :class:`TransactionManager` owns the higher-level
transactional **policy**:

* Which :class:`IsolationLevel` a transaction runs at.
* Whether failed statements roll back the whole transaction or only to a
  savepoint (per FG3's "execute → verify → retry → rollback" pipeline).
* Cross-feature-group transactional notifications — ``database.transaction.*``
  events that the audit logger (FG6) and the GUI recovery view (FG5) observe.
* A declarative :meth:`transaction` decorator that the AI Brain orchestrator
  applies to composable operations without passing a session through every
  signature.

The transaction manager does NOT coordinate cross-database transactions
(SQLite + Qdrant + knowledge graph). The Unit-of-Work class handles
multi-store coordination; this module is strictly SQLite + session scoped
and exists to keep the UoW focused on cross-store semantics.

Isolation levels
----------------
SQLite supports a limited, frozen set of isolation levels because the database
file itself is single-writer per database. The driver maps ``IsolationLevel``
values to ``BEGIN`` statement variants:

* ``READ_UNCOMMITTED``    → ``PRAGMA read_uncommitted=ON`` + ``BEGIN``
* ``READ_COMMITTED``      → ``BEGIN`` (default; each statement sees only
                            committed rows thanks to WAL snapshot isolation)
* ``REPEATABLE_READ``     → ``BEGIN`` (SQLite row snapshots per transaction)
* ``SERIALIZABLE``        → ``BEGIN IMMEDIATE`` (acquires writer lock up front;
                            blocks concurrent writers until commit)
* ``AUTO``                → no explicit BEGIN; the driver auto-commits

These mappings are applied in :meth:`_prepare_isolation`; mismatches are
translated rather than rejected so config values stay portable across future
Postgres deployments of selected feature-group tables (FG10 analytics schemas).

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
connection_manager → session_manager → here.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Optional, TypeVar

from app.core.constants.events import EventCategory, SystemEvent
from app.core.database.connection_manager import ConnectionManager
from app.core.database.session_manager import (
    Propagation,
    Session,
    SessionManager,
    SessionState,
)
from app.core.database.sqlite.connection import SQLiteConnection
from app.core.exceptions.database import DatabaseError, TransactionError
from app.logging import Logger

__all__ = [
    "IsolationLevel",
    "TransactionOutcome",
    "TransactionPolicy",
    "TransactionContext",
    "TransactionStats",
    "TransactionManager",
    "transactional",
    "Transactional",
]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class IsolationLevel(str, Enum):
    """SQLite-supported isolation levels (see module docstring)."""

    AUTO = "auto"
    READ_UNCOMMITTED = "read_uncommitted"
    READ_COMMITTED = "read_committed"
    REPEATABLE_READ = "repeatable_read"
    SERIALIZABLE = "serializable"


_BEGIN_BY_ISOLATION: dict[IsolationLevel, str] = {
    IsolationLevel.AUTO: "",
    IsolationLevel.READ_UNCOMMITTED: "BEGIN",
    IsolationLevel.READ_COMMITTED: "BEGIN",
    IsolationLevel.REPEATABLE_READ: "BEGIN",
    IsolationLevel.SERIALIZABLE: "BEGIN IMMEDIATE",
}


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


class TransactionOutcome(str, Enum):
    """What happened to a transaction when its scope ended."""

    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    ROLLED_BACK_TO_SAVEPOINT = "rolled_back_to_savepoint"
    NO_TRANSACTION = "no_transaction"  # body did not start one


# ---------------------------------------------------------------------------
# Policy + context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransactionPolicy:
    """Per-call tuning for a transactional scope.

    Mirrors Spring's ``@Transactional`` semantics in the subset AIOS needs;
    intentionally not exhaustive (no MAINTATORY/NESTED/NEVER) so the contract
    stays legible for callers who are not transaction experts.
    """

    isolation: IsolationLevel = IsolationLevel.READ_COMMITTED
    propagation: Propagation = Propagation.REQUIRED
    timeout_seconds: Optional[int] = None  # not enforced at SQLite level; observability hook
    rollback_on: tuple[type[BaseException], ...] = (Exception,)
    no_rollback_on: tuple[type[BaseException], ...] = ()
    name: Optional[str] = None  # logical name — recorded by the audit logger

    def should_rollback_on(self, exc: BaseException) -> bool:
        for nr_type in self.no_rollback_on:
            if isinstance(exc, nr_type):
                return False
        for r_type in self.rollback_on:
            if isinstance(exc, r_type):
                return True
        return False


@dataclass(slots=True)
class TransactionContext:
    """Public record of one transaction's execution.

    Returned to the caller so feature groups can attach a (trace_id, outcome)
    pair to their own audit/telemetry records without coupling themselves to
    the transaction manager's internals.
    """

    transaction_id: str
    trace_id: str
    name: Optional[str]
    isolation: IsolationLevel
    outcome: TransactionOutcome
    savepoint: Optional[str] = None
    duration_seconds: float = 0.0
    error: Optional[BaseException] = None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TransactionStats:
    """Lifetime counters exposed for the health manager."""

    transactions: int = 0
    commits: int = 0
    rollbacks: int = 0
    savepoint_rollbacks: int = 0
    failures: int = 0
    in_flight: int = 0

    def as_dict(self) -> dict:
        return {
            "transactions": self.transactions,
            "commits": self.commits,
            "rollbacks": self.rollbacks,
            "savepoint_rollbacks": self.savepoint_rollbacks,
            "failures": self.failures,
            "in_flight": self.in_flight,
        }


# ---------------------------------------------------------------------------
# TransactionManager
# ---------------------------------------------------------------------------


class TransactionManager:
    """Coordinates SQLite transactions over a :class:`SessionManager`.

    Bound to a single :class:`SessionManager` (and therefore a single
    :class:`ConnectionManager` / :class:`SQLiteEngine`). Two integration
    surfaces are exposed:

    * :meth:`transaction` — context-managed declarative scope. The body runs
      inside a transaction or nested savepoint; an exception matching
      ``policy.rollback_on`` rolls back, otherwise it propagates except for
      a clean commit.
    * :meth:`transactional` — decorator factory that the AI brain orchestrator
      applies to composable units of work. The decorated method receives the
      session via ``self._session`` only if it owns one; the decorator
      itself is the canonical entry point for "this is a transactional op".
    """

    __slots__ = (
        "_session_manager",
        "_logger",
        "_lock",
        "_stats",
        "_closed",
    )

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self._session_manager = session_manager
        self._logger = logger
        self._lock = threading.RLock()
        self._stats = TransactionStats()
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def stats(self) -> TransactionStats:
        with self._lock:
            return self._stats

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ----------------------------------------------------------- transactional scope
    @contextmanager
    def transaction(
        self,
        *,
        policy: Optional[TransactionPolicy] = None,
        caller: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Iterator[Session]:
        """Run a body inside a transaction or savepoint.

        Yields the :class:`Session`. On clean exit the transaction (or
        savepoint) commits; on a matching exception the scope rolls back.
        """
        if self._closed:
            raise DatabaseError("TransactionManager is closed")

        active_policy = policy or TransactionPolicy()
        outcome: TransactionOutcome = TransactionOutcome.NO_TRANSACTION
        error: Optional[BaseException] = None
        savepoint_name: Optional[str] = None
        joined_ambient = False

        # Determine whether to nest via SAVEPOINT or open a fresh transaction.
        ambient = self._session_manager.current()
        if (
            active_policy.propagation is Propagation.REQUIRED
            and ambient is not None
            and not ambient.is_closed
            and ambient.in_transaction
        ):
            # Join ambient transaction as a savepoint so a rollback here does
            # not abort work the outer scope may still want to commit.
            joined_ambient = True

        tx_id = uuid.uuid4().hex
        tid = trace_id or (ambient.trace_id if ambient is not None else tx_id)
        import time as _time
        started_at = _time.monotonic()

        # Open (or join) the session. We never begin a transaction here for
        # SERIALIZABLE — SQLite's BEGIN IMMEDIATE acquires the writer lock
        # immediately, and our session manager uses isolation_level=None
        # (manual-commit) which is the only mode where BEGIN IMMEDIATE
        # works. The session manager's begin() issues plain BEGIN; we override
        # here for SERIALIZABLE.
        with self._session_manager.session(
            caller=caller or active_policy.name,
            propagation=active_policy.propagation,
            begin=False,
            trace_id=tid,
        ) as session:
            try:
                self._prepare_isolation(session.connection, active_policy.isolation)
                if joined_ambient:
                    savepoint_name = session.savepoint(name=f"tx_{tx_id[:8]}")
                else:
                    self._issue_begin(session.connection, active_policy.isolation)
                    # Reflect that the outermost transaction is now active so
                    # session.in_transaction stays consistent.
                    session._has_outer_transaction = True
                    session._state = SessionState.ACTIVE

                with self._lock:
                    self._stats.transactions += 1
                    self._stats.in_flight += 1

                yield session

                # Clean exit — commit the scope.
                if savepoint_name is not None:
                    session.release_savepoint(savepoint_name)
                    outcome = TransactionOutcome.COMMITTED
                else:
                    session.commit()
                    outcome = TransactionOutcome.COMMITTED

            except BaseException as exc:
                error = exc
                if active_policy.should_rollback_on(exc):
                    with self._lock:
                        self._stats.failures += 1
                    try:
                        if savepoint_name is not None:
                            session.rollback_to_savepoint(savepoint_name)
                            outcome = TransactionOutcome.ROLLED_BACK_TO_SAVEPOINT
                            self._emit_rollback_event(
                                tid, active_policy, savepoint=savepoint_name, exc=exc
                            )
                        else:
                            session.rollback()
                            outcome = TransactionOutcome.ROLLED_BACK
                            self._emit_rollback_event(
                                tid, active_policy, savepoint=None, exc=exc
                            )
                    except Exception as rollback_exc:
                        if self._logger:
                            self._logger.error(
                                "Rollback itself failed; connection will be evicted",
                                extra={
                                    "trace_id": tid,
                                    "original_error": str(exc),
                                    "rollback_error": str(rollback_exc),
                                },
                            )
                        # Surface the rollback failure; the original exception
                        # takes precedence per Python __context__ semantics but
                        # the rollback error is the actionable one.
                        raise TransactionError(
                            operation="rollback",
                            cause=rollback_exc,
                        ) from exc
                else:
                    # Exception did not match rollback_on — caller wants the
                    # transaction committed (rare; e.g. IgnoreException classes).
                    if savepoint_name is not None:
                        try:
                            session.release_savepoint(savepoint_name)
                        except Exception:
                            pass
                    else:
                        try:
                            session.commit()
                        except Exception:
                            pass
                    outcome = TransactionOutcome.COMMITTED
                raise
            else:
                with self._lock:
                    if outcome is TransactionOutcome.COMMITTED:
                        self._stats.commits += 1
            finally:
                # Reconcile per-outcome counters.
                with self._lock:
                    if outcome is TransactionOutcome.ROLLED_BACK_TO_SAVEPOINT:
                        self._stats.savepoint_rollbacks += 1
                    if outcome is TransactionOutcome.ROLLED_BACK:
                        self._stats.rollbacks += 1
                    if self._stats.in_flight > 0:
                        self._stats.in_flight -= 1
                duration = _time.monotonic() - started_at
                if self._logger:
                    self._logger.debug(
                        "Transaction completed",
                        extra={
                            "transaction_id": tx_id,
                            "trace_id": tid,
                            "name": active_policy.name,
                            "isolation": active_policy.isolation.value,
                            "outcome": outcome.value,
                            "duration_seconds": round(duration, 6),
                            "savepoint": savepoint_name,
                            "joined_ambient": joined_ambient,
                        },
                    )

    # ----------------------------------------------------------- isolation helpers
    @staticmethod
    def _prepare_isolation(conn: SQLiteConnection, isolation: IsolationLevel) -> None:
        """Apply per-isolation-level PRAGMAs before issuing BEGIN."""
        if isolation is IsolationLevel.READ_UNCOMMITTED:
            conn.execute("PRAGMA read_uncommitted = ON")
        else:
            # Re-arm the default for subsequent transactions even if the engine
            # preset didn't choose read-uncommitted at connect time.
            conn.execute("PRAGMA read_uncommitted = OFF")
        if isolation is IsolationLevel.SERIALIZABLE:
            # Acquire the writer lock up front so concurrent writers block at
            # BEGIN-time rather than mid-statement.
            return
        return

    @staticmethod
    def _issue_begin(conn: SQLiteConnection, isolation: IsolationLevel) -> None:
        stmt = _BEGIN_BY_ISOLATION[isolation]
        if stmt:
            conn.execute(stmt)

    # ----------------------------------------------------------- event emission
    def _emit_rollback_event(
        self,
        trace_id: str,
        policy: TransactionPolicy,
        *,
        savepoint: Optional[str],
        exc: BaseException,
    ) -> None:
        """Emit a rollback event for the audit logger / FG5 dashboard.

        The transaction manager deliberately does not import the event bus
        directly — see ``database_manager.py`` for the centralized wiring.
        Instead it publishes through a registered sink that the DatabaseManager
        installs during bootstrap. This keeps the transaction manager
        import-safe (no circular dependency on event_bus).
        """
        sink = _rollback_sinks.get(id(self))
        if sink is None:
            return
        try:
            sink(
                {
                    "event": "database.transaction.rollback",
                    "trace_id": trace_id,
                    "name": policy.name,
                    "isolation": policy.isolation.value,
                    "savepoint": savepoint,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                }
            )
        except Exception:  # noqa: BLE001 - never block the rollback path on a sink failure
            pass

    def install_rollback_sink(self, sink: Callable[[dict], None]) -> Callable[[], None]:
        """Register a callable to receive ``database.transaction.rollback`` events.

        Used by the DatabaseManager to bridge into the EventBus without pulling
        the bus into this module's import graph. Returns an unsubscribe hook.
        """
        with self._lock:
            _rollback_sinks[id(self)] = sink

        def _unsubscribe() -> None:
            _rollback_sinks.pop(id(self), None)

        return _unsubscribe

    # ----------------------------------------------------------- decorator factory
    def transactional(
        self,
        *,
        policy: Optional[TransactionPolicy] = None,
        caller: Optional[str] = None,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator factory binding a :class:`TransactionPolicy` to a method.

        The decorated callable may accept an optional first positional
        parameter (after ``self``) which receives the active :class:`Session` —
        if the method's signature does not name ``session``, the decorator
        silently skips the injection.
        """
        active_policy = policy or TransactionPolicy()

        def _decorator(func: Callable[..., T]) -> Callable[..., T]:
            import functools
            import inspect

            sig = inspect.signature(func)
            # Detect whether the callable accepts a 'session' parameter we can
            # inject. Bind by parameter name so users can name it anything.
            params = list(sig.parameters.values())
            accepts_session = any(p.name == "session" for p in params[1:])

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> T:
                label = caller or func.__qualname__
                with self.transaction(
                    policy=active_policy,
                    caller=label,
                ) as session:
                    if accepts_session and "session" not in kwargs:
                        kwargs["session"] = session
                    return func(*args, **kwargs)

            wrapper.__transaction_policy__ = active_policy  # type: ignore[attr-defined]
            return wrapper

        return _decorator

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        self._closed = True
        if self._logger:
            self._logger.info(
                "TransactionManager closed",
                extra={"stats": self._stats.as_dict()},
            )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<TransactionManager in_flight={self._stats.in_flight} "
            f"commits={self._stats.commits} rollbacks={self._stats.rollbacks}>"
        )


# Module-global sink registry — keyed by ``id(self)`` so multiple managers
# (one per database) can each have their own sink without needing a shared
# global event bus. Lives at module scope because transaction *events* are
# side-effects, not state — keeping them on the instance would still require a
# module-level registry, so we keep it explicit.
_rollback_sinks: dict[int, Callable[[dict], None]] = {}


# ---------------------------------------------------------------------------
# Public-API shims
# ---------------------------------------------------------------------------


def transactional(
    manager: TransactionManager,
    *,
    policy: Optional[TransactionPolicy] = None,
    caller: Optional[str] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Free-function decorator binding a manager/policy pair to a callable.

    Equivalent to ``manager.transactional(policy=policy, caller=caller)``;
    exposed at module scope so calling sites can import the decorator without
    referencing a manager instance inline.
    """
    return manager.transactional(policy=policy, caller=caller)


class Transactional:
    """Mixin helper that pre-arms a TransactionManager for a feature group.

    Feature groups typically hold a single :class:`TransactionManager` and
    decorate their service methods. This base removes the boilerplate of
    storing it on ``self._tx`` and provides the same :meth:`transactional`
    decorator as a bound method.
    """

    __slots__ = ("_tx",)

    def __init__(self, transaction_manager: TransactionManager) -> None:
        self._tx = transaction_manager

    @property
    def transaction_manager(self) -> TransactionManager:
        return self._tx

    def transactional(
        self,
        *,
        policy: Optional[TransactionPolicy] = None,
        caller: Optional[str] = None,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        return self._tx.transactional(policy=policy, caller=caller)
