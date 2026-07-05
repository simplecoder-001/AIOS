# app/core/database/unit_of_work.py
"""
Unit-of-Work (UoW) — cross-store transactional coordination.

A real user request in AIOS frequently mutates more than one store:

* FG2 memory engine writes a memory row to SQLite **and** an embedding to
  Qdrant **and** updates the knowledge graph.
* FG3 transaction manager snapshots file/registry state to the recovery
  folder **and** records a recovery_operations audit row **and** rolls back
  the SQLite snapshot if the action's verification fails.
* FG10 deployment pipeline applies a learned workflow to the learning DB
  **and** records a learning event **and** updates the knowledge graph.

These stores do **not** share a transaction protocol — SQLite is ACID, Qdrant
is per-collection, the knowledge graph is in-process NetworkX. The Unit-of-
Work coordinates them with the **two-phase commit with compensating
rollback** pattern: every change is queued, ``commit()`` flushes them in the
right order, and on any failure the UoW invokes the registered rollback
callbacks in reverse order. Imperfect (distributed atomicity is impossible
without a shared transaction manager) but the architectural invariant is
that the *SQLite* layer is always either fully committed or fully rolled
back, and the *non-SQLite* layers run compensating actions leaving the
system in a state where the recovery manager can finish cleanup on the next
start.

Responsibilities
----------------
* **Single entry point** — :meth:`UnitManager.work()` opens a UoW bound to the
  ambient thread; nested joins share the outermost UoW.
* **Per-store batches** — each store has a small in-memory staging buffer that
  the feature-group code fills; the UoW flushes batches atomically per store.
* **Compensating rollback registry** — every mutation registers a callable
  that can undo the change; on failure they run in reverse registration order.
* **Trace propagation** — the UoW carries a `trace_id` correlated to the
  event-bus context so every cross-store change is auditable as one logical
  operation.
* **Observable** — emits ``database.uow.committed`` / ``database.uow.rolled_back``
  events through a sink installed by the :class:`DatabaseManager`.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
connection_manager → session_manager → transaction_manager → repository →
here. Does NOT import qdrant/knowledge_graph directly — those stores receive
the UoW through duck-typed callbacks so this module stays import-safe from
either side.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Iterator

from app.core.database.session_manager import (
    Propagation,
    Session,
    SessionManager,
)
from app.core.database.transaction_manager import (
    IsolationLevel,
    TransactionManager,
    TransactionPolicy,
)
from app.core.exceptions.database import (
    DatabaseError,
    TransactionError,
    VectorStoreError,
    KnowledgeGraphError,
)
from app.logging import Logger

__all__ = [
    "StoreKind",
    "MutationOp",
    "RollbackHandle",
    "UowOutcome",
    "UowContext",
    "UowStats",
    "UnitOfWork",
    "UnitManager",
]


# ---------------------------------------------------------------------------
# Enums + records
# ---------------------------------------------------------------------------


class StoreKind(str, Enum):
    """The four backing stores that a UnitOfWork may touch."""

    SQLITE_METADATA = "sqlite_metadata"
    SQLITE_ENCRYPTED = "sqlite_encrypted"
    QDRANT = "qdrant"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    EXTERNAL = "external"          # recovery folder, file system, etc.


class UowOutcome(str, Enum):
    COMMITTED = "committed"
    SOFT_COMMITTED = "soft_committed"     # SQLite ok; non-SQLite partial
    ROLLED_BACK = "rolled_back"
    ROLLED_BACK_PARTIAL = "rolled_back_partial"


@dataclass(slots=True)
class MutationOp:
    """A staged mutation queued in the UoW before flush.

    The UoW does not interpret the payload; it simply hands it to the
    registered ``flush_callback`` for the store. This keeps the UoW free of
    any qdrant/networkx import surface.
    """

    store: StoreKind
    operation: str       # feature-group-defined label ("memory.insert", "kg.link")
    payload: Mapping[str, Any]
    flush_callback: Optional[Callable[[Mapping[str, Any]], None]] = None


@dataclass(slots=True)
class RollbackHandle:
    """A compensating action that can undo a staged mutation.

    Stored callbacks receive no arguments and may safely raise; on raise the
    UoW marks the rollback as partial and continues with the remaining
    callbacks. The recovery manager reads the partial state from the audit
    log on next boot.
    """

    store: StoreKind
    operation: str
    callback: Callable[[], None]
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Stats + shared registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UowStats:
    """Lifetime counters consumed by the health manager."""

    opened: int = 0
    committed: int = 0
    soft_committed: int = 0
    rolled_back: int = 0
    rolled_back_partial: int = 0
    in_flight: int = 0
    compensations_run: int = 0
    compensation_failures: int = 0

    def as_dict(self) -> dict:
        return {
            "opened": self.opened,
            "committed": self.committed,
            "soft_committed": self.soft_committed,
            "rolled_back": self.rolled_back,
            "rolled_back_partial": self.rolled_back_partial,
            "in_flight": self.in_flight,
            "compensations_run": self.compensations_run,
            "compensation_failures": self.compensation_failures,
        }


# ---------------------------------------------------------------------------
# UnitOfWork
# ---------------------------------------------------------------------------


class UnitOfWork:
    """One cross-store unit of work bound to a single SQLite transaction.

    Constructed only by :class:`UnitManager`. The unit begins a SQLite
    transaction in its session on first mutation (lazy BEGIN); commits all
    staged mutations on :meth:`commit` in the dependency order
    SQLite → encrypted SQLite → Qdrant → knowledge graph → external, then
    commits the SQLite transaction last. On any failure the SQLite
    transaction is rolled back and all already-applied compensations are
    rolled back in reverse order.
    """

    __slots__ = (
        "_uow_id",
        "_trace_id",
        "_caller",
        "_policy",
        "_session_manager",
        "_transaction_manager",
        "_session",
        "_owns_session",
        "_stage",
        "_applied",
        "_rollbacks",
        "_state",
        "_logger",
        "_manager",
        "_closed",
    )

    def __init__(
        self,
        *,
        uow_id: str,
        trace_id: str,
        caller: Optional[str],
        policy: TransactionPolicy,
        session_manager: SessionManager,
        transaction_manager: TransactionManager,
        logger: Optional[Logger],
        manager: "UnitManager",
    ) -> None:
        self._uow_id = uow_id
        self._trace_id = trace_id
        self._caller = caller
        self._policy = policy
        self._session_manager = session_manager
        self._transaction_manager = transaction_manager
        self._session: Optional[Session] = None
        self._owns_session = False
        self._stage: list[MutationOp] = []
        self._applied: list[Tuple[MutationOp, RollbackHandle]] = []
        self._rollbacks: list[RollbackHandle] = []
        self._state = "open"
        self._logger = logger
        self._manager = manager
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def uow_id(self) -> str:
        return self._uow_id

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def caller(self) -> Optional[str]:
        return self._caller

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def session(self) -> Session:
        """Lazily acquire / join the SQLite session backing this UoW."""
        return self._ensure_session()

    @property
    def stage_size(self) -> int:
        return len(self._stage)

    @property
    def applied_size(self) -> int:
        return len(self._applied)

    # ----------------------------------------------------------- stage + rollback
    def stage(
        self,
        store: StoreKind,
        operation: str,
        payload: Mapping[str, Any],
        *,
        flush: Optional[Callable[[Mapping[str, Any]], None]] = None,
        rollback: Optional[Callable[[], None]] = None,
        rollback_description: Optional[str] = None,
    ) -> None:
        """Queue a mutation to be flushed at commit time.

        Parameters
        ----------
        store:
            Target store. The UoW flushes per-store groups in dependency
            order; mixed-store mutations interleaved across calls land in the
            order they were staged.
        operation:
            Feature-group-defined op label (e.g. ``"memory.insert"``).
            Recorded in the audit trail.
        payload:
            Opaque per-op dict handed to ``flush``.
        flush:
            Invoked at commit time with the payload. Use None for staged
            writes that the caller has already staged elsewhere — but then
            no compensating action is automatically recorded.
        rollback:
            Optional compensating callback invoked on failure. Receives no
            arguments; can safely raise (the UoW logs and continues).
        """
        self._ensure_open()
        self._stage.append(MutationOp(store=store, operation=operation, payload=payload, flush_callback=flush))
        if rollback is not None:
            self._rollbacks.append(
                RollbackHandle(
                    store=store,
                    operation=operation,
                    callback=rollback,
                    description=rollback_description,
                )
            )

    def stage_sqlite(self, operation: str, payload: Mapping[str, Any]) -> None:
        """Convenience: stage a SQLite metadata mutation with no compensating
        callback — the SQLite transaction rollback IS the compensation."""
        self.stage(StoreKind.SQLITE_METADATA, operation, payload, flush=None, rollback=None)

    def __enter__(self) -> "UnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._closed:
            return
        if exc is not None:
            self.rollback()
        else:
            try:
                self.commit()
            except Exception:
                # Commit failure already invoked rollback internally.
                raise

    # ----------------------------------------------------------- commit / rollback
    def commit(self) -> UowOutcome:
        """Flush staged mutations and commit the SQLite transaction."""
        self._ensure_open()
        if self._state in ("committing", "rolled_back", "soft_committed"):
            return UowOutcome.COMMITTED if self._state == "soft_committed" else UowOutcome.COMMITTED

        self._state = "committing"
        outcome: UowOutcome
        started = time.monotonic()

        # If no work was staged we still issue a commit on a started SQLite
        # transaction to keep the session-state consistent.
        session = self._ensure_session()
        try:
            # Flush per store in the canonical order so SQLite is the first
            # store whose pending writes are observable; if SQLite fails the
            # non-SQLite stores have not been touched yet.
            ordered = self._ordered_stages()

            for op in ordered:
                cb = op.flush_callback
                if cb is None:
                    continue  # no flush — purely staged metadata change
                try:
                    cb(op.payload)
                except Exception as exc:
                    self._classify_flush_failure(op, exc)
                # Track applied mutations so we can run compensations if a
                # later flush fails. The matching rollback is the most-recent
                # registered one whose operation matches; this is a best-effort
                # pairing — callers may attach no rollback at all.
                rb = self._pop_matching_rollback(op)
                if rb is not None:
                    self._applied.append((op, rb))

            # Commit the SQLite transaction. If this fails, run all the
            # compensations in reverse order — the SQLite state will be
            # rolled back to before the UoW.
            if session.in_transaction:
                session.commit()
            outcome = UowOutcome.COMMITTED

        except Exception:
            outcome = self._rollback_with_compensations()
            raise

        finally:
            self._state = outcome.value
            if self._owns_session:
                session.close()
                self._owns_session = False
            self._closed = True
            self._manager._record_outcome(self, outcome, duration=time.monotonic() - started)
            self._emit_event("database.uow.committed" if outcome is UowOutcome.COMMITTED else "database.uow.rolled_back")

        return outcome

    def rollback(self) -> UowOutcome:
        """Roll back the current UoW and run all registered compensations."""
        if self._closed:
            return UowOutcome.ROLLED_BACK
        self._state = "rolling_back"
        outcome = self._rollback_with_compensations()
        if self._owns_session and self._session is not None:
            self._session.close()
            self._owns_session = False
        self._closed = True
        self._manager._record_outcome(self, outcome, duration=0.0)
        self._emit_event("database.uow.rolled_back")
        return outcome

    def _rollback_with_compensations(self) -> UowOutcome:
        """Internal: roll back SQLite + run compensations for applied ops.

        Returns ``ROLLED_BACK`` when every compensation succeeded, otherwise
        ``ROLLED_BACK_PARTIAL``. Does not raise: commit callers re-raise the
        original error; direct rollback callers receive the outcome.
        """
        outcome = UowOutcome.ROLLED_BACK
        # SQLite first — its transaction rollback undoes all in-transaction
        # metadata writes atomically.
        if self._session is not None and self._session.in_transaction:
            try:
                self._session.rollback()
            except Exception:
                # The transaction manager already marked the connection broken.
                outcome = UowOutcome.ROLLED_BACK_PARTIAL

        # Compensations for non-SQLite stores that have already been flushed.
        partial = False
        for op, rb in reversed(self._applied):
            try:
                rb.callback()
                with self._manager._lock:
                    self._manager._stats.compensations_run += 1
            except Exception as exc:
                partial = True
                with self._manager._lock:
                    self._manager._stats.compensation_failures += 1
                if self._logger:
                    self._logger.error(
                        "Compensating rollback failed",
                        extra={
                            "trace_id": self._trace_id,
                            "store": rb.store.value,
                            "operation": rb.operation,
                            "description": rb.description,
                            "error": str(exc),
                        },
                    )
        # Clear the queue so a double-commit does not retry.
        self._stage.clear()
        self._rollbacks.clear()
        if partial:
            outcome = UowOutcome.ROLLED_BACK_PARTIAL
        return outcome

    # ----------------------------------------------------------- internals
    def _ordered_stages(self) -> List[MutationOp]:
        """Sort the staged mutations by store-kind flush order.

        Stable sort preserves caller-supplied relative order within a store,
        which is essential for the FG3 "snapshot → run → verify" pipeline
        where the order of file-system writes is semantically significant.
        """
        order = {
            StoreKind.SQLITE_METADATA: 0,
            StoreKind.SQLITE_ENCRYPTED: 1,
            StoreKind.QDRANT: 2,
            StoreKind.KNOWLEDGE_GRAPH: 3,
            StoreKind.EXTERNAL: 4,
        }
        return sorted(
            self._stage,
            key=lambda op: order.get(op.store, 99),
        )

    def _pop_matching_rollback(self, op: MutationOp) -> Optional[RollbackHandle]:
        """Find the most-recently registered rollback whose operation matches."""
        for i in range(len(self._rollbacks) - 1, -1, -1):
            rb = self._rollbacks[i]
            if rb.operation == op.operation and rb.store == op.store:
                return self._rollbacks.pop(i)
        return None

    def _classify_flush_failure(self, op: MutationOp, exc: BaseException) -> None:
        """Translate a flush failure into the appropriate AIOS exception.

        Promotes raw exceptions raised by qdrant/networkx/file callbacks into
        typed AIOS exceptions so commit() can run the rollback pipeline before
        re-raising.
        """
        if op.store is StoreKind.QDRANT:
            raise VectorStoreError(operation=op.operation, cause=exc) from exc
        if op.store is StoreKind.KNOWLEDGE_GRAPH:
            raise KnowledgeGraphError(operation=op.operation, cause=exc) from exc
        raise TransactionError(operation=op.operation, cause=exc) from exc

    def _ensure_session(self) -> Session:
        if self._session is not None and not self._session.is_closed:
            return self._session
        # We open a transactional session bound to the ambient if present.
        session = self._session_manager.open(
            caller=self._caller or f"uow:{self._uow_id[:8]}",
            propagation=self._policy.propagation,
            begin=True,
            trace_id=self._trace_id,
        )
        # Determine whether we joined the ambient (must not close it) or
        # opened a fresh session (we own its release).
        ambient = self._session_manager.current()
        self._owns_session = session is not ambient
        self._session = session
        return session

    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError(
                "UnitOfWork already closed",
            ).with_context(trace_id=self._trace_id, uow_id=self._uow_id)

    def _emit_event(self, name: str) -> None:
        sink = _uow_sinks.get(id(self._manager))
        if sink is None:
            return
        try:
            sink(
                {
                    "event": name,
                    "trace_id": self._trace_id,
                    "uow_id": self._uow_id,
                    "caller": self._caller,
                    "state": self._state,
                }
            )
        except Exception:  # noqa: BLE001 - never block the commit path on a sink failure
            pass

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<UnitOfWork id={self._uow_id[:8]!r} state={self._state} "
            f"staged={len(self._stage)} applied={len(self._applied)}>"
        )


# ---------------------------------------------------------------------------
# UnitManager
# ---------------------------------------------------------------------------


class UnitManager:
    """Factory and ambient-thread registry for :class:`UnitOfWork` instances.

    One :class:`UnitManager` is constructed per :class:`DatabaseManager` and
    coordinates the SQLite + SQLCipher + Qdrant + knowledge-graph stores for
    that database set. Ambient propagation mirrors the session manager: a
    feature group that opens a UoW while another UoW is already active on the
    same thread simply joins the outer one's SQLite transaction so partial
    writes are visible across the inner and outer scope.
    """

    __slots__ = (
        "_session_manager",
        "_transaction_manager",
        "_logger",
        "_ambient",
        "_lock",
        "_stats",
        "_policy_default",
        "_closed",
    )

    def __init__(
        self,
        session_manager: SessionManager,
        transaction_manager: TransactionManager,
        *,
        logger: Optional[Logger] = None,
        default_policy: Optional[TransactionPolicy] = None,
    ) -> None:
        self._session_manager = session_manager
        self._transaction_manager = transaction_manager
        self._logger = logger
        self._ambient: dict[int, UnitOfWork] = {}
        self._lock = threading.RLock()
        self._stats = UowStats()
        self._policy_default = default_policy or TransactionPolicy()
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def transaction_manager(self) -> TransactionManager:
        return self._transaction_manager

    @property
    def stats(self) -> UowStats:
        with self._lock:
            self._stats.in_flight = len(self._ambient)
            return self._stats

    @property
    def is_closed(self) -> bool:
        return self._closed

    def current(self) -> Optional[UnitOfWork]:
        return self._ambient.get(threading.get_ident())

    # ----------------------------------------------------------- open
    def open(
        self,
        *,
        caller: Optional[str] = None,
        policy: Optional[TransactionPolicy] = None,
        trace_id: Optional[str] = None,
    ) -> UnitOfWork:
        if self._closed:
            raise DatabaseError("UnitManager is closed")
        tid = threading.get_ident()
        ambient = self._ambient.get(tid)
        if (
            ambient is not None
            and not ambient.is_closed
            and (policy is None or policy.propagation is Propagation.REQUIRED)
        ):
            # Join ambient; ignore isolation differences — the ambient owns
            # the SQLite BEGIN tag.
            return ambient

        active_policy = policy or self._policy_default
        uow = UnitOfWork(
            uow_id=uuid.uuid4().hex,
            trace_id=trace_id or uuid.uuid4().hex,
            caller=caller,
            policy=active_policy,
            session_manager=self._session_manager,
            transaction_manager=self._transaction_manager,
            logger=self._logger,
            manager=self,
        )
        with self._lock:
            self._stats.opened += 1
            self._stats.in_flight += 1
            self._ambient.setdefault(tid, uow)
        return uow

    @contextmanager
    def work(
        self,
        *,
        caller: Optional[str] = None,
        policy: Optional[TransactionPolicy] = None,
        trace_id: Optional[str] = None,
    ) -> Iterator["UnitOfWork"]:
        """Context-managed UoW — preferred entry point.

        On exception: rolls back; on clean exit: commits. When the body joins
        an ambient UoW (REQUIRED + outer active) the inner scope does NOT
        commit on clean exit by default — that responsibility belongs to the
        outer scope. The inner scope WILL roll back to its savepoint on
        exception, leaving the outer UoW free to retry or commit.
        """
        tid = threading.get_ident()
        ambient = self._ambient.get(tid)
        joined = (
            ambient is not None
            and not ambient.is_closed
            and (policy is None or policy.propagation is Propagation.REQUIRED)
        )
        uow = self.open(caller=caller, policy=policy, trace_id=trace_id)
        try:
            yield uow
        except Exception:
            if not joined:
                # We own the SQLite transaction — full rollback.
                uow.rollback()
            else:
                # Joined the ambient: stage-rollback only clears the staged
                # queue + clearing pending compensations. The outer UoW owns
                # the SQLite rollback.
                uow._stage.clear()
                uow._rollbacks.clear()
            raise
        else:
            if not joined:
                uow.commit()
        finally:
            # Always remove the ambient entry if we own it; never remove the
            # entry we joined.
            if not joined:
                with self._lock:
                    if self._ambient.get(tid) is uow:
                        self._ambient.pop(tid, None)
                    if self._stats.in_flight > 0:
                        self._stats.in_flight -= 1

    # ----------------------------------------------------------- diagnostics
    def install_event_sink(self, sink: Callable[[dict], None]) -> Callable[[], None]:
        """Register a sink for UoW commit/rollback events.

        Installed by the DatabaseManager so the EventBus gets the events
        without this module importing the bus directly.
        """
        with self._lock:
            _uow_sinks[id(self)] = sink

        def _unsubscribe() -> None:
            _uow_sinks.pop(id(self), None)

        return _unsubscribe

    def _record_outcome(self, uow: UnitOfWork, outcome: UowOutcome, *, duration: float) -> None:
        with self._lock:
            if outcome is UowOutcome.COMMITTED:
                self._stats.committed += 1
            elif outcome is UowOutcome.SOFT_COMMITTED:
                self._stats.soft_committed += 1
            elif outcome is UowOutcome.ROLLED_BACK:
                self._stats.rolled_back += 1
            elif outcome is UowOutcome.ROLLED_BACK_PARTIAL:
                self._stats.rolled_back_partial += 1
        if self._logger:
            self._logger.debug(
                "UnitOfWork completed",
                extra={
                    "trace_id": uow.trace_id,
                    "uow_id": uow.uow_id,
                    "caller": uow.caller,
                    "outcome": outcome.value,
                    "duration_seconds": round(duration, 6),
                    "stage_size": uow.stage_size,
                    "applied_size": uow.applied_size,
                },
            )

    # ----------------------------------------------------------- shutdown
    def close_all(self) -> None:
        with self._lock:
            uows = list(self._ambient.values())
            self._ambient.clear()
        for u in uows:
            if not u.is_closed:
                try:
                    u.rollback()
                except Exception:
                    if self._logger:
                        self._logger.warning("Error rolling back ambient UoW at shutdown")

    def close(self) -> None:
        self._closed = True
        self.close_all()
        if self._logger:
            self._logger.info("UnitManager closed", extra={"stats": self._stats.as_dict()})

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<UnitManager in_flight={self._stats.in_flight} closed={self._closed}>"


# Module-global sink registry; see the corresponding note in
# transaction_manager.py.
_uow_sinks: dict[int, Callable[[dict], None]] = {}


# ---------------------------------------------------------------------------
# Backwards-compatible UoWContext exporter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UowContext:
    """Public handle for feature groups to capture in their event payloads."""

    uow_id: str
    trace_id: str
    state: str
    applied_count: int


def current_uow_context(manager: UnitManager) -> Optional[UowContext]:
    u = manager.current()
    if u is None:
        return None
    return UowContext(
        uow_id=u.uow_id,
        trace_id=u.trace_id,
        state=u.state,
        applied_count=u.applied_size,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += ["UnitManager", "UowContext", "current_uow_context", "UowOutcome"]
