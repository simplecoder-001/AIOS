# app/core/database/session_manager.py
"""
Per-request / per-task database sessions on top of the connection pool.

A :class:`SessionManager` produces short-lived :class:`Session` objects that
combine three concerns a repository layer needs in one place:

* A *single* connection leased from the :class:`ConnectionManager` for the
  duration of a logical unit of work (one user turn, one tool call, one
  scheduled task).
* A *nested-savepoint stack* so callers can open sub-transactions
  (``BEGIN ... SAVEPOINT s1 ... SAVEPOINT s2``) and roll back to a marker
  without aborting the outer transaction. SQLite implements ``SAVEPOINT``
  directly when the driver is in manual-commit mode (isolation_level=None,
  which the engine sets when a transaction manager is active).
* A *propagation flag* — ``Propagation.REQUIRED`` joins an ambient session in
  the current thread, ``Propagation.REQUIRES_NEW`` always opens a fresh one.
  Mirrors Spring's ``@Transactional`` semantics so the AI brain orchestrator
  can compose sub-flows without passing an explicit connection through every
  signature.

Why sessions instead of letting repositories use the pool directly?
--------------------------------------------------------------------
* **Bounded scope** — a session closes (returns its connection) at a single
  well-defined boundary, preventing the silent leak when a repository forgets
  to release.
* **Savepoint correctness** — SQLite's savepoint semantics depend on the
  driver being in manual-commit mode; a session hides that detail so the
  repository code never accidentally opens a duplicate ``BEGIN``.
* **Correlation** — each session carries a ``trace_id`` so the audit logger
  can correlate every executed statement with the originating feature-group
  event.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → connection manager
→ here.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Optional

from app.core.database.connection_manager import ConnectionManager
from app.core.database.sqlite.connection import SQLiteConnection
from app.core.exceptions.database import DatabaseError, TransactionError
from app.logging import Logger

__all__ = [
    "Propagation",
    "SessionState",
    "SessionStats",
    "Session",
    "SessionManager",
    "SessionContext",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Propagation(str, Enum):
    """How a new session relates to an ambient session on the same thread.

    Mirrors Spring's transaction propagation contract in the subset of modes
    the AIOS codebase actually uses; ``NESTED``/``SUPPORTS``/``MANDATORY`` are
    intentionally absent because they would obscure the contract for callers
    who do not need them.
    """

    REQUIRED = "required"          # Join ambient, or open if none.
    REQUIRES_NEW = "requires_new"  # Always open a fresh independent session.


class SessionState(str, Enum):
    """Lifecycle states observed by the session manager."""

    IDLE = "idle"               # opened, no active transaction
    ACTIVE = "active"           # outermost BEGIN issued
    SAVEPOINT = "savepoint"     # inside ≥1 nested savepoint
    COMMITTING = "committing"
    ROLLING_BACK = "rolling_back"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionStats:
    """Cumulative counters used by the health manager."""

    sessions_opened: int = 0
    sessions_closed: int = 0
    commits: int = 0
    rollbacks: int = 0
    rollbacks_on_exception: int = 0
    in_flight: int = 0

    def as_dict(self) -> dict:
        return {
            "sessions_opened": self.sessions_opened,
            "sessions_closed": self.sessions_closed,
            "commits": self.commits,
            "rollbacks": self.rollbacks,
            "rollbacks_on_exception": self.rollbacks_on_exception,
            "in_flight": self.in_flight,
        }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class Session:
    """A single leased connection plus its savepoint stack.

    Created only by :class:`SessionManager` — never instantiated directly by
    application code. Callers obtain one via :meth:`SessionManager.session` or
    :meth:`SessionManager.open`.
    """

    __slots__ = (
        "_conn",
        "_owner",
        "_trace_id",
        "_caller",
        "_state",
        "_savepoints",
        "_has_outer_transaction",
        "_logger",
        "_release_on_close",
        "_closed",
    )

    def __init__(
        self,
        connection: SQLiteConnection,
        *,
        owner: "SessionManager",
        trace_id: str,
        caller: Optional[str],
        logger: Optional[Logger],
        release_on_close: bool,
    ) -> None:
        self._conn = connection
        self._owner = owner
        self._trace_id = trace_id
        self._caller = caller
        self._logger = logger
        self._state = SessionState.IDLE
        self._savepoints: list[str] = []
        self._has_outer_transaction = False
        self._release_on_close = release_on_close
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def connection(self) -> SQLiteConnection:
        if self._closed:
            raise DatabaseError("Session is closed").with_context(trace_id=self._trace_id)
        return self._conn

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def caller(self) -> Optional[str]:
        return self._caller

    @property
    def in_transaction(self) -> bool:
        return self._has_outer_transaction or bool(self._savepoints)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def savepoint_depth(self) -> int:
        return len(self._savepoints)

    # ----------------------------------------------------------- transactional API
    def begin(self) -> None:
        """Start the outermost transaction.

        Idempotent: subsequent calls become no-ops once a transaction is
        active. Use :meth:`savepoint` for nested scope.
        """
        self._ensure_open()
        if self._has_outer_transaction:
            return
        self._conn.begin()
        self._has_outer_transaction = True
        self._state = SessionState.ACTIVE

    def savepoint(self, name: Optional[str] = None) -> str:
        """Open a nested savepoint and return its name.

        Auto-generates a unique name when ``name`` is None. The name is the
        handle the caller passes to :meth:`release_savepoint` /
        :meth:`rollback_to_savepoint`.
        """
        self._ensure_open()
        sp_name = name or f"sp_{len(self._savepoints) + 1}_{uuid.uuid4().hex[:8]}"
        self._conn.execute(f"SAVEPOINT {sp_name}")
        self._savepoints.append(sp_name)
        self._state = SessionState.SAVEPOINT
        return sp_name

    def release_savepoint(self, name: str) -> None:
        """Release (commit) a savepoint once its scope is healthy.

        After release the savepoint can no longer be rolled back to. If it was
        the outermost savepoint, the session returns to the ACTIVE state; the
        outer transaction remains open until :meth:`commit` / :meth:`rollback`.
        """
        self._ensure_open()
        if name not in self._savepoints:
            raise TransactionError(
                operation=f"release_savepoint({name})",
            ).with_context(
                trace_id=self._trace_id,
                active_savepoints=list(self._savepoints),
            )
        # Release nested savepoints opened after this one first; SQLite
        # forbids releasing out-of-order savepoints.
        idx = self._savepoints.index(name)
        nested = self._savepoints[idx + 1:]
        for inner in reversed(nested):
            self._conn.execute(f"RELEASE SAVEPOINT {inner}")
            self._savepoints.pop()
        self._conn.execute(f"RELEASE SAVEPOINT {name}")
        self._savepoints.pop()
        if not self._savepoints and self._has_outer_transaction:
            self._state = SessionState.ACTIVE
        elif not self._savepoints and not self._has_outer_transaction:
            self._state = SessionState.IDLE

    def rollback_to_savepoint(self, name: str) -> None:
        """Roll back to a savepoint, discarding later nested work."""
        self._ensure_open()
        if name not in self._savepoints:
            raise TransactionError(
                operation=f"rollback_to_savepoint({name})",
            ).with_context(
                trace_id=self._trace_id,
                active_savepoints=list(self._savepoints),
            )
        idx = self._savepoints.index(name)
        nested = self._savepoints[idx + 1:]
        # Roll back to the savepoint itself — SQLite automatically releases
        # nested savepoints opened after the target.
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        for inner in nested:
            self._savepoints.remove(inner)
        # The named savepoint remains active for further work in the same scope.
        self._state = SessionState.SAVEPOINT

    def commit(self) -> None:
        """Commit the outermost transaction and reset session state.

        No-op when no transaction was started — callers may always call
        ``commit()`` at the boundary without tracking whether anything
        wrote.
        """
        self._ensure_open()
        if self._savepoints:
            # Leak prevention: release outstanding savepoints first. Their
            # work is included in the commit.
            for sp in list(self._savepoints):
                try:
                    self._conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    # Savepoint may already have been rolled back; ignore.
                    pass
            self._savepoints.clear()
        if not self._has_outer_transaction:
            return
        self._state = SessionState.COMMITTING
        try:
            self._conn.commit()
        except Exception as exc:
            self._state = SessionState.ROLLING_BACK
            self._conn.rollback()
            self._state = SessionState.IDLE
            self._has_outer_transaction = False
            raise TransactionError(operation="commit", cause=exc) from exc
        self._has_outer_transaction = False
        self._state = SessionState.IDLE

    def rollback(self) -> None:
        """Roll back the outermost transaction (and any nested savepoints)."""
        self._ensure_open()
        self._state = SessionState.ROLLING_BACK
        try:
            self._conn.execute("ROLLBACK")
        except Exception:
            # An ROLLBACK failure is unrecoverable; mark the connection broken
            # so the pool evicts it on release.
            self._owner._mark_session_broken(self)
            raise
        finally:
            self._savepoints.clear()
            self._has_outer_transaction = False
            self._state = SessionState.IDLE

    # ----------------------------------------------------------- close
    def close(self, *, broken: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        # Defensive cleanup of an unfinished transaction. Rollback is safer
        # than commit at an unknown close point.
        if self._has_outer_transaction:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                self._owner._mark_session_broken(self)
        self._savepoints.clear()
        self._has_outer_transaction = False
        self._state = SessionState.CLOSED
        if self._release_on_close:
            self._owner._release_session_connection(self, broken=broken)

    # ----------------------------------------------------------- internal
    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError("Session is closed").with_context(trace_id=self._trace_id)

    # ----------------------------------------------------------- context mgmt
    def __enter__(self) -> "Session":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # On exception: roll back the outermost transaction so partial writes
        # do not persist. A clean exit expects the caller to have committed
        # (or to have left the session in IDLE state with no transaction).
        broken = exc is not None
        if broken and self._has_outer_transaction:
            self.rollback()
        self.close(broken=broken)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<Session trace_id={self._trace_id!r} "
            f"state={self._state} depth={len(self._savepoints)}>"
        )


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Factory and ambient-thread registry for :class:`Session` objects.

    One :class:`SessionManager` is constructed per :class:`ConnectionManager`
    by the :class:`DatabaseManager`. The manager also provides ambient
    propagation: callers submitting a session via :meth:`session` with
    ``Propagation.REQUIRED`` will, when an outer session is already active on
    the current thread, simply reuse it instead of opening a nested lease.
    """

    __slots__ = (
        "_pool",
        "_logger",
        "_ambient",
        "_lock",
        "_stats",
        "_closed",
    )

    def __init__(
        self,
        pool: ConnectionManager,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self._pool = pool
        self._logger = logger
        self._ambient: dict[int, Session] = {}  # thread_id -> ambient session
        self._lock = threading.RLock()
        self._stats = SessionStats()
        self._closed = False

    # ------------------------------------------------------------- properties
    @property
    def pool(self) -> ConnectionManager:
        return self._pool

    @property
    def stats(self) -> SessionStats:
        with self._lock:
            self._stats.in_flight = len(self._ambient)
            return self._stats

    @property
    def is_closed(self) -> bool:
        return self._closed

    def current(self) -> Optional[Session]:
        """Return the ambient session for the calling thread, if any."""
        return self._ambient.get(threading.get_ident())

    # ------------------------------------------------------------- open
    def open(
        self,
        *,
        caller: Optional[str] = None,
        propagation: Propagation = Propagation.REQUIRED,
        begin: bool = False,
        trace_id: Optional[str] = None,
    ) -> Session:
        """Open a (possibly ambient) session.

        Parameters
        ----------
        caller:
            Free-form label recorded for diagnostics.
        propagation:
            REQUIRED joins the ambient thread session; REQUIRES_NEW always
            starts a fresh independent session.
        begin:
            When True the outermost transaction is started immediately.
        trace_id:
            Optional correlation id; auto-generated when None so the audit
            logger always has a value.
        """
        if self._closed:
            raise DatabaseError("SessionManager is closed")

        tid = threading.get_ident()
        if propagation is Propagation.REQUIRED:
            ambient = self._ambient.get(tid)
            if ambient is not None and not ambient.is_closed:
                if begin:
                    ambient.begin()
                return ambient

        conn = self._pool.acquire(caller=caller)
        session = Session(
            connection=conn,
            owner=self,
            trace_id=trace_id or uuid.uuid4().hex,
            caller=caller,
            logger=self._logger,
            release_on_close=True,
        )
        with self._lock:
            self._stats.sessions_opened += 1
            self._stats.in_flight += 1
            # Only register as ambient when there isn't already one; nested
            # sessions (REQUIRES_NEW) never overwrite the ambient.
            if tid not in self._ambient or self._ambient[tid].is_closed:
                self._ambient[tid] = session
        if begin:
            session.begin()
        return session

    @contextmanager
    def session(
        self,
        *,
        caller: Optional[str] = None,
        propagation: Propagation = Propagation.REQUIRED,
        begin: bool = False,
        trace_id: Optional[str] = None,
    ) -> Iterator[Session]:
        """Context-managed session — preferred entry point for repositories.

        Automatically commits on clean exit when ``begin=True`` was requested
        and the body did not commit itself; rolls back on exception; always
        releases the connection back to the pool on exit.
        """
        session = self.open(
            caller=caller,
            propagation=propagation,
            begin=begin,
            trace_id=trace_id,
        )
        # Track whether we created the connection (REQUIRES_NEW or first
        # ambient). If we joined the ambient, we must NOT release it.
        joined = session is not self._ambient.get(threading.get_ident())
        try:
            yield session
        except Exception:
            if session.in_transaction:
                session.rollback()
            raise
        else:
            if begin and session.in_transaction:
                session.commit()
        finally:
            # Only close sessions we actually own: the ambient session belongs
            # to the outer scope that opened it.
            if not joined:
                session.close()

    # ------------------------------------------------------------- internal callbacks
    def _release_session_connection(self, session: Session, *, broken: bool) -> None:
        self._pool.release(session._conn, broken=broken)
        with self._lock:
            self._stats.sessions_closed += 1
            self._stats.in_flight = max(0, self._stats.in_flight - 1)
            if self._stats.in_flight == 0 and session._caller:
                self._ambient.pop(threading.get_ident(), None)
        # The pool's release() does the actual logging.

    def _mark_session_broken(self, session: Session) -> None:
        # Tag the underlying connection so the next commit/rollback/close
        # releases it as broken; the pool will evict it.
        session._release_on_close = True
        if self._logger:
            self._logger.warning(
                "Session marked broken; connection will be evicted on release",
                extra={"trace_id": session._trace_id},
            )

    # ------------------------------------------------------------- maintenance
    def close_all(self) -> None:
        """Force-close every ambient session (shutdown)."""
        with self._lock:
            sessions = list(self._ambient.values())
            self._ambient.clear()
        for s in sessions:
            if not s.is_closed:
                try:
                    s.close(broken=True)
                except Exception:
                    if self._logger:
                        self._logger.warning("Error closing ambient session at shutdown")

    def close(self) -> None:
        """Shut the manager down. Subsequent ``open`` calls raise."""
        self._closed = True
        self.close_all()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<SessionManager in_flight={self._stats.in_flight} closed={self._closed}>"


# ---------------------------------------------------------------------------
# Convenience alias
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionContext:
    """Return type capturing enough metadata for downstream audit logging.

    The AI brain orchestrator frequently needs to know which session it ran a
    request in without retaining a reference to the :class:`Session` itself
    (which would defeat scoped release). This lightweight record is the
    public handle.
    """

    trace_id: str
    caller: Optional[str] = None
    state: SessionState = SessionState.IDLE


def current_trace_id(manager: SessionManager) -> Optional[str]:
    """Return the trace_id of the calling thread's ambient session, or None."""
    s = manager.current()
    return s.trace_id if s is not None else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += ["SessionStats", "SessionContext", "current_trace_id"]
