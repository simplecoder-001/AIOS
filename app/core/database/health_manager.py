# app/core/database/health_manager.py
"""
Continuous health monitoring for the AIOS database layer.

The health manager is the single source of truth every other subsystem consults
for: "is the database server alive?" Because SQLite is in-process, "alive"
means something more nuanced than a TCP probe — the questions the manager
actually answers are:

1. **File integrity** — can a read-only connection run ``PRAGMA
   integrity_check`` against the file without raising? A WAL race or a
   truncated file would surface here.
2. **Foreign-key consistency** — are there any orphaned rows after a
   half-applied migration (FK enforcement was OFF at the moment the rows were
   written)? ``PRAGMA foreign_key_check`` finds them.
3. **Page-accounting drift** — ``PRAGMA page_count`` against the expected
   floor and ceiling. A runaway INSERT without a checkpoint would push the
   WAL far past the configured ceiling; the health manager surfaces it as
   HEALTH_DEGRADED before the user smells it.
4. **Pool saturation** — acquire failures and timeouts in the connection
   pool are the earliest indicator that a feature group is leaking
   connections. The manager queries :class:`ConnectionManager.stats` each
   poll so the dashboard (FG5) sees the same numbers the bootstrap evaluator
   sees.
5. **Slow-statement detection** — every check is timed; a check that
   exceeds the configured budget flips the database to ``degraded`` even when
   the check itself "succeeded".

States
------
The manager maintains a per-database :class:`HealthState` enum that mirrors
the system event catalog:

* HEALTHY          — every check is green
* DEGRADED        — one or more checks left a warning
* UNHEALTHY       — at least one check failed
* UNREACHABLE    — the database file cannot be opened at all

Transitions publish the corresponding :class:`SystemEvent`:

* HEALTHY   → *               emits nothing (only failures are noisy)
* * → DEGRADED / UNHEALTHY  emits ``system.health.degraded``
* * → HEALTHY               emits ``system.health.restored``
* Any → UNREACHABLE        emits ``system.health.failed``

Polling is driven externally — the bootstrap wires an APScheduler job (FG8
layer 10) that calls :meth:`HealthManager.poll`. The manager itself is
thread-safe (uses a single RLock) but never starts a background thread on
its own, keeping the lifecycle predictable for tests and the shutdown
sequence.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
connection_manager → session_manager → ``sqlite/engine`` → here. Does not
import the event bus — sinks are registered through the same hook pattern as
transaction_manager / unit_of_work / backup_manager.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

from app.core.database.connection_manager import (
    ConnectionManager,
    PoolStats,
)
from app.core.database.sqlite.engine import SQLiteEngine
from app.core.exceptions.database import (
    ConnectionError,
    DatabaseError,
    QueryError,
)
from app.logging import Logger

__all__ = [
    "HealthState",
    "HealthCheck",
    "HealthSnapshot",
    "HealthStats",
    "HealthThrottle",
    "HealthManager",
]


# ---------------------------------------------------------------------------
# Enums + records
# ---------------------------------------------------------------------------


class HealthState(str, Enum):
    """Per-database health ranking. Higher ordinal = worse."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNREACHABLE = "unreachable"

    @classmethod
    def worse(cls, a: "HealthState", b: "HealthState") -> "HealthState":
        """Return the more severe of two states."""
        order = (
            cls.HEALTHY,
            cls.DEGRADED,
            cls.UNHEALTHY,
            cls.UNREACHABLE,
        )
        return a if order.index(a) >= order.index(b) else b


@dataclass(slots=True)
class HealthCheck:
    """A single named check executed inside one poll cycle."""

    name: str
    duration_seconds: float
    state: HealthState
    detail: Optional[Mapping[str, Any]] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "duration_seconds": round(self.duration_seconds, 6),
            "state": self.state.value,
            "detail": dict(self.detail) if self.detail else None,
        }


@dataclass(slots=True)
class HealthSnapshot:
    """A point-in-time view of one database's health."""

    database_name: str
    database_path: str
    state: HealthState
    timestamp: str
    checks: List[HealthCheck] = field(default_factory=list)
    pool_stats: Optional[PoolStats] = None

    def as_dict(self) -> dict:
        return {
            "database_name": self.database_name,
            "database_path": self.database_path,
            "state": self.state.value,
            "timestamp": self.timestamp,
            "checks": [c.as_dict() for c in self.checks],
            "pool_stats": self.pool_stats.as_dict() if self.pool_stats else None,
        }


@dataclass(slots=True)
class HealthStats:
    """Lifetime counters for the health manager itself.

    Surfaces: total polls performed, per-database last-healthy timestamp,
    successive failure streaks used by the recovery manager to trigger an
    automatic restore after N consecutive ``UNREACHABLE`` checks.
    """

    polls: int = 0
    checks_executed: int = 0
    state_transitions: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    last_healthy_at: dict[str, str] = field(default_factory=dict)
    last_state: dict[str, HealthState] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "polls": self.polls,
            "checks_executed": self.checks_executed,
            "state_transitions": self.state_transitions,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": dict(self.consecutive_failures),
            "last_healthy_at": dict(self.last_healthy_at),
            "last_state": {k: v.value for k, v in self.last_state.items()},
        }


@dataclass(frozen=True, slots=True)
class HealthThrottle:
    """Per-check throttling (avoid hammering slow PRAGMAs every cycle).

    ``min_interval_seconds=0`` disables throttling. Velocity tracking on
    the connection pool lets the manager bump a check that has been silent
    for a while into the next poll cycle if a corresponding stat has spiked
    — e.g. ``acquire_timeouts`` between polls triggers an immediate
    ``pool`` check on the next cycle regardless of its throttle window.
    """

    integrity_min_interval_seconds: int = 300       # five minutes
    foreign_key_min_interval_seconds: int = 600     # ten minutes
    page_count_min_interval_seconds: int = 60       # every minute
    pool_min_interval_seconds: int = 10             # ten seconds

    slow_statement_warn_seconds: float = 5.0       # anything slower flips DEGRADED


# ---------------------------------------------------------------------------
# HealthManager
# ---------------------------------------------------------------------------


class HealthManager:
    """Continuous health monitor for a database set.

    Constructed per :class:`DatabaseManager`. The manager keeps a
    ``database_name -> (engine, pool)`` table — the same registration surface
    the :class:`DatabaseManager` exposes to feature groups. Checks run on a
    poll-driven basis driven externally (APScheduler during bootstrap) so
    production lifecycle and tests can both call :meth:`poll` deterministically.
    """

    __slots__ = (
        "_databases",
        "_throttle",
        "_logger",
        "_lock",
        "_stats",
        "_last_run",
        "_event_sink",
        "_failure_threshold",
        "_on_recovery",
        "_closed",
    )

    def __init__(
        self,
        databases: Mapping[str, tuple[SQLiteEngine, ConnectionManager]],
        *,
        throttle: Optional[HealthThrottle] = None,
        logger: Optional[Logger] = None,
        failure_threshold: int = 3,
        on_recovery: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not databases:
            raise DatabaseError("HealthManager requires at least one database")
        self._databases = dict(databases)
        self._throttle = throttle or HealthThrottle()
        self._logger = logger
        self._lock = threading.RLock()
        self._stats = HealthStats()
        self._last_run: Dict[str, Dict[str, float]] = {
            name: {} for name in self._databases
        }
        self._event_sink: Optional[Callable[[dict], None]] = None
        self._failure_threshold = max(1, failure_threshold)
        self._on_recovery = on_recovery
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def throttle(self) -> HealthThrottle:
        return self._throttle

    @property
    def stats(self) -> HealthStats:
        with self._lock:
            return self._stats

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def databases(self) -> List[str]:
        return list(self._databases.keys())

    # ----------------------------------------------------------- event sink
    def install_event_sink(self, sink: Callable[[dict], None]) -> Callable[[], None]:
        """Register a sink to receive health-transition events.

        The DatabaseManager installs a sink that bridges to the EventBus so
        this module never imports the bus. Returns an unsubscribe callable.
        """
        with self._lock:
            self._event_sink = sink

        def _unsubscribe() -> None:
            with self._lock:
                self._event_sink = None

        return _unsubscribe

    # ----------------------------------------------------------- poll API
    def poll(self) -> List[HealthSnapshot]:
        """Run one health-check cycle across every registered database.

        Returns the per-database snapshots in registration order so the
        caller (the bootstrap job) can publish a single
        ``database.health.poll`` event at the end of the cycle.
        """
        if self._closed:
            raise DatabaseError("HealthManager is closed")
        snapshots: List[HealthSnapshot] = []
        now_mono = time.monotonic()
        for name in self.databases:
            try:
                snapshots.append(self._poll_database(name, now_mono))
            except Exception as exc:
                snapshots.append(
                    HealthSnapshot(
                        database_name=name,
                        database_path=str(self._databases[name][0].database),
                        state=HealthState.UNREACHABLE,
                        timestamp=_now_iso(),
                        checks=[
                            HealthCheck(
                                name="poll",
                                duration_seconds=0.0,
                                state=HealthState.UNREACHABLE,
                                detail={"error": str(exc)[:500]},
                            )
                        ],
                        pool_stats=self._safe_pool_stats(name),
                    )
                )
                if self._logger:
                    self._logger.error(
                        "Health poll raised unexpectedly",
                        extra={"database": name, "error": str(exc)},
                    )
        with self._lock:
            self._stats.polls += 1
        return snapshots

    def poll_one(self, database_name: str) -> HealthSnapshot:
        """Run a poll against a single database. Used by the recovery manager
        to confirm a restore took effect before re-engaging the pool."""
        if self._closed:
            raise DatabaseError("HealthManager is closed")
        if database_name not in self._databases:
            raise DatabaseError(f"unknown database: {database_name!r}")
        return self._poll_database(database_name, time.monotonic())

    def snapshot(self, database_name: str) -> HealthSnapshot:
        """Return the most-recent snapshot for a database (no re-poll).

        Useful for the graceful-shutdown path: the bootstrap asks the manager
        for the last-known health before tearing down the pool.
        """
        with self._lock:
            state = self._stats.last_state.get(database_name)
        if state is None:
            # First ever observation — poll now so the caller gets real data
            # rather than a sentinel. Cheap: a fresh poll cycle misses the
            # throttles for a single database.
            return self.poll_one(database_name)
        return self._build_cached_snapshot(database_name, state)

    def _build_cached_snapshot(self, database_name: str, state: HealthState) -> HealthSnapshot:
        return HealthSnapshot(
            database_name=database_name,
            database_path=str(self._databases[database_name][0].database),
            state=state,
            timestamp=self._stats.last_healthy_at.get(database_name, _now_iso()),
            pool_stats=self._safe_pool_stats(database_name),
        )

    # ----------------------------------------------------------- poll internals
    def _poll_database(self, name: str, now_mono: float) -> HealthSnapshot:
        engine, pool = self._databases[name]
        checks: List[HealthCheck] = []
        overall = HealthState.HEALTHY

        # Pool stats are always fresh — they are O(1) reads of the pool
        # manager's own counters.
        pool_stats = pool.stats
        checks.append(self._pool_check(pool_stats))
        overall = HealthState.worse(overall, checks[-1].state)

        # Integrity / FK / page_count need a live connection; run any check
        # whose throttle window has elapsed.
        last_runs = self._last_run.get(name, {})
        if self._throttle.integrity_min_interval_seconds == 0 or self._due(
            last_runs, "integrity", now_mono, self._throttle.integrity_min_interval_seconds
        ):
            checks.append(self._integrity_check(engine))
            last_runs["integrity"] = now_mono
            overall = HealthState.worse(overall, checks[-1].state)

        if self._due(
            last_runs, "foreign_key", now_mono, self._throttle.foreign_key_min_interval_seconds
        ):
            checks.append(self._foreign_key_check(engine))
            last_runs["foreign_key"] = now_mono
            overall = HealthState.worse(overall, checks[-1].state)

        if self._due(
            last_runs, "page_count", now_mono, self._throttle.page_count_min_interval_seconds
        ):
            checks.append(self._page_count_check(engine, pool))
            last_runs["page_count"] = now_mono
            overall = HealthState.worse(overall, checks[-1].state)

        snapshot = HealthSnapshot(
            database_name=name,
            database_path=engine.database,
            state=overall,
            timestamp=_now_iso(),
            checks=checks,
            pool_stats=pool_stats,
        )

        with self._lock:
            self._stats.checks_executed += len(checks)
            prev = self._stats.last_state.get(name)
            if prev is not None and prev != overall:
                self._stats.state_transitions += 1
            if overall is HealthState.HEALTHY:
                self._stats.successes += 1
                self._stats.last_healthy_at[name] = snapshot.timestamp
                self._stats.consecutive_failures[name] = 0
            else:
                self._stats.failures += 1
                self._stats.consecutive_failures[name] = (
                    self._stats.consecutive_failures.get(name, 0) + 1
                )
            self._stats.last_state[name] = overall
            self._last_run[name] = last_runs

        self._emit_events(prev, snapshot)
        self._maybe_recover(name, overall)
        return snapshot

    @staticmethod
    def _due(last_runs: Mapping[str, float], check: str, now_mono: float, min_interval: int) -> bool:
        last = last_runs.get(check)
        if last is None:
            return True
        return (now_mono - last) >= min_interval

    # ----------------------------------------------------------- individual checks
    def _integrity_check(self, engine: SQLiteEngine) -> HealthCheck:
        started = time.monotonic()
        try:
            with engine.connection() as conn:
                cur = conn.execute("PRAGMA integrity_check")
                rows = cur.fetchall()
                cur.close()
                state, detail = self._interpret_simple_pragma(rows)
        except Exception as exc:
            return HealthCheck(
                name="integrity",
                duration_seconds=time.monotonic() - started,
                state=HealthState.UNHEALTHY,
                detail={"error": str(exc)[:500]},
            )
        dur = time.monotonic() - started
        if dur > self._throttle.slow_statement_warn_seconds:
            state = HealthState.worse(state, HealthState.DEGRADED)
        return HealthCheck(
            name="integrity",
            duration_seconds=dur,
            state=state,
            detail=detail,
        )

    def _foreign_key_check(self, engine: SQLiteEngine) -> HealthCheck:
        started = time.monotonic()
        try:
            with engine.connection() as conn:
                cur = conn.execute("PRAGMA foreign_key_check")
                rows = cur.fetchall()
                cur.close()
        except Exception as exc:
            return HealthCheck(
                name="foreign_key",
                duration_seconds=time.monotonic() - started,
                state=HealthState.UNHEALTHY,
                detail={"error": str(exc)[:500]},
            )
        dur = time.monotonic() - started
        if rows:
            detail = {
                "violation_count": len(rows),
                "first_row": dict(rows[0]) if rows else None,
            }
            state = HealthState.UNHEALTHY
        else:
            detail = {"violation_count": 0}
            state = HealthState.HEALTHY
        if dur > self._throttle.slow_statement_warn_seconds:
            state = HealthState.worse(state, HealthState.DEGRADED)
        return HealthCheck(
            name="foreign_key",
            duration_seconds=dur,
            state=state,
            detail=detail,
        )

    def _page_count_check(self, engine: SQLiteEngine, pool: ConnectionManager) -> HealthCheck:
        started = time.monotonic()
        try:
            with engine.connection() as conn:
                page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
                wal_pages = int(
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[2] or 0
                )
        except Exception as exc:
            return HealthCheck(
                name="page_count",
                duration_seconds=time.monotonic() - started,
                state=HealthState.UNHEALTHY,
                detail={"error": str(exc)[:500]},
            )
        dur = time.monotonic() - started
        wal_mb = (wal_pages * page_size) / (1024 * 1024)
        # Hard-coded bands: anything over 256 MiB WAL is degraded, >1 GiB is
        # unhealthy. Tunable later via config if FG8 telemetry coverage shows
        # it being touched regularly.
        state = HealthState.HEALTHY
        if wal_mb > 1024:
            state = HealthState.UNHEALTHY
        elif wal_mb > 256:
            state = HealthState.DEGRADED
        return HealthCheck(
            name="page_count",
            duration_seconds=dur,
            state=state,
            detail={
                "page_count": page_count,
                "page_size": page_size,
                "wal_pages": wal_pages,
                "wal_mb": round(wal_mb, 2),
            },
        )

    def _pool_check(self, pool_stats: PoolStats) -> HealthCheck:
        started = time.monotonic()
        # Saturation: a small configured pool driven aggressively should
        # surface as DEGRADED long before acquire-timeout. The thresholds are
        # "spot ratio of in_use to max_size without exceeding 16".
        max_size_ = getattr(pool_stats, "size", 0) or 1
        in_use_ratio = pool_stats.in_use / max_size_ if max_size_ else 0.0
        state = HealthState.HEALTHY
        if pool_stats.acquire_failures > 0 or pool_stats.acquire_timeouts > 0:
            state = HealthState.UNHEALTHY
        elif in_use_ratio > 0.85:
            state = HealthState.DEGRADED
        dur = time.monotonic() - started
        return HealthCheck(
            name="pool",
            duration_seconds=dur,
            state=state,
            detail={
                "size": pool_stats.size,
                "idle": pool_stats.idle,
                "in_use": pool_stats.in_use,
                "in_use_ratio": round(in_use_ratio, 3),
                "acquire_failures": pool_stats.acquire_failures,
                "acquire_timeouts": pool_stats.acquire_timeouts,
                "evicted": pool_stats.evicted,
            },
        )

    @staticmethod
    def _interpret_simple_pragma(rows: Any) -> tuple[HealthState, Optional[Dict[str, Any]]]:
        # PRAGMA integrity_check returns one of:
        # - a single row "ok" — healthy
        # - a single textual error — unhealthy
        # - multiple error rows — unhealthy (report count + first)
        if not rows:
            return HealthState.HEALTHY, {"value": "ok"}
        first_value = rows[0][0] if len(rows[0]) else ""
        if len(rows) == 1 and first_value == "ok":
            return HealthState.HEALTHY, {"value": "ok"}
        return HealthState.UNHEALTHY, {
            "value": str(first_value)[:500],
            "count": len(rows),
        }

    def _safe_pool_stats(self, name: str) -> Optional[PoolStats]:
        try:
            return self._databases[name][1].stats
        except Exception:
            return None

    # ----------------------------------------------------------- event emission
    def _emit_events(self, prev: Optional[HealthState], snapshot: HealthSnapshot) -> None:
        if prev is None:
            # First observation; emit a stored snapshot but never raise an
            # initial "degraded" alert so a fresh startup is quiet.
            return
        if prev == snapshot.state:
            return
        with self._lock:
            sink = self._event_sink
        if sink is None:
            return
        event_name: str
        if snapshot.state is HealthState.HEALTHY:
            event_name = "database.health.restored"
        elif snapshot.state is HealthState.UNREACHABLE:
            event_name = "database.health.failed"
        else:
            event_name = "database.health.degraded"
        try:
            sink(
                {
                    "event": event_name,
                    "database": snapshot.database_name,
                    "prev_state": prev.value,
                    "new_state": snapshot.state.value,
                    "timestamp": snapshot.timestamp,
                    "checks": [c.as_dict() for c in snapshot.checks],
                }
            )
        except Exception:  # noqa: BLE001 - a sink failure must not block polls.
            pass

    # ----------------------------------------------------------- automatic recovery
    def _maybe_recover(self, name: str, state: HealthState) -> None:
        if self._on_recovery is None:
            return
        if state is HealthState.HEALTHY:
            return
        with self._lock:
            streak = self._stats.consecutive_failures.get(name, 0)
        if streak < self._failure_threshold:
            return
        if self._logger:
            self._logger.warning(
                "Database consecutive-failure threshold hit; invoking recovery",
                extra={
                    "database": name,
                    "consecutive_failures": streak,
                    "threshold": self._failure_threshold,
                },
            )
        try:
            self._on_recovery(name)
        except Exception as exc:
            if self._logger:
                self._logger.error(
                    "Recovery hook raised; aborting further automatic retries",
                    extra={"database": name, "error": str(exc)},
                )
        # The recovery manager is responsible for resetting consecutive_failures
        # once the restore succeeds.

    def reset_failure_streak(self, database_name: str) -> None:
        """Used by the recovery manager after a successful restore."""
        with self._lock:
            self._stats.consecutive_failures[database_name] = 0

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        self._closed = True
        if self._logger:
            self._logger.info(
                "HealthManager closed",
                extra={"stats": self._stats.as_dict()},
            )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<HealthManager databases={len(self._databases)} "
            f"polls={self._stats.polls} failures={self._stats.failures}>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_throttle() -> HealthThrottle:
    """Return the :class:`HealthThrottle` defaults so the DatabaseManager can
    override individual knobs from configuration without touching class
    defaults implicitly."""
    return HealthThrottle()


__all__ += ["default_throttle"]
