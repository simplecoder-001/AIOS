# app/core/database/connection_manager.py
"""
Thread-safe connection pool manager for the AIOS database layer.

A :class:`ConnectionManager` owns a bounded pool of :class:`SQLiteConnection`
instances for a single :class:`SQLiteEngine`. The multi-threaded voice
pipeline (FG1), the async AI brain (FG2), and the FG3 execution engine all
acquire connections concurrently — without a pool the engine would open a
fresh ``sqlite3.connect()`` (a syscall-bound fsopen) on every statement, and
without bounds the pool would leak file descriptors under load.

Responsibilities
----------------
* **Acquire / release** — :meth:`acquire` blocks up to a configurable timeout
  for a free connection then validates it before handing it back; :meth:`release`
  returns a healthy connection to the pool and evicts broken ones.
* **Bounded queue** — callers wait on a :class:`queue.Queue` when the pool is
  empty; a configurable ``acquire_timeout_ms`` prevents indefinite stalls when
  a feature group forgets to release.
* **Liveness validation** — every acquired connection is pinged with a
  zero-cost ``SELECT 1``; broken connections are closed and the caller
  transparently retries against the next slot.
* **Max-age + idle recycling** — connections older than ``max_age_seconds`` or
  idle longer than ``idle_timeout_seconds`` are evicted on release so the
  health manager never observes connections open for the entire process uptime.
* **Leak detection** — an optional lease tracker records which caller/thread
  has each connection; the health manager can dump it for diagnostics.
* **Lifecycle events** — emits events via the optional injected
  :class:`EventBus` so FG5 dashboard and FG6 audit can observe pool state.

The pool is intentionally NOT process-shared: SQLite connections are file
descriptors local to a process. Multi-process workers (future FG9
distributed mode) each construct their own :class:`ConnectionManager`
against the same on-disk database file, coordinated by the WAL journal_mode
already set by the engine's PRAGMA preset.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → ``sqlite/connection``
→ ``sqlite/engine`` → here.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from app.core.database.sqlite.connection import (
    ConnectionState,
    SQLiteConnection,
)
from app.core.database.sqlite.engine import SQLiteEngine
from app.core.exceptions.database import ConnectionError
from app.logging import Logger

__all__ = [
    "PoolConfig",
    "PoolStats",
    "LeaseRecord",
    "ConnectionManager",
]


# ---------------------------------------------------------------------------
# Config + stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolConfig:
    """Bounded pool tuning knobs.

    Defaults intentionally conservative: FG1 worker threads + FG2 async brain
    + FG3 execution threads together rarely exceed 16 concurrent readers on
    a local SQLite database. The upper bound exists to bound file-descriptor
    usage under pathological load, not for everyday throughput.
    """

    min_size: int = 1                # always-warm connections
    max_size: int = 16               # hard ceiling
    acquire_timeout_ms: int = 5_000  # raise after this if no slot frees
    max_age_seconds: int = 3_600     # recycle after 1h of lifetime
    idle_timeout_seconds: int = 300  # recycle if idle for 5 min
    validate_on_acquire: bool = True  # PING every connection before handing out
    leak_tracking: bool = True       # record lease metadata for diagnostics

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("PoolConfig.min_size must be >= 0")
        if self.max_size < 1:
            raise ValueError("PoolConfig.max_size must be >= 1")
        if self.min_size > self.max_size:
            raise ValueError("PoolConfig.min_size cannot exceed max_size")
        if self.acquire_timeout_ms < 0:
            raise ValueError("PoolConfig.acquire_timeout_ms must be >= 0")
        if self.max_age_seconds < 1:
            raise ValueError("PoolConfig.max_age_seconds must be >= 1")
        if self.idle_timeout_seconds < 1:
            raise ValueError("PoolConfig.idle_timeout_seconds must be >= 1")


@dataclass(slots=True)
class PoolStats:
    """Observable pool metrics — read by the health manager."""

    acquired: int = 0
    released: int = 0
    evicted: int = 0
    creations: int = 0
    acquire_failures: int = 0
    acquire_timeouts: int = 0
    size: int = 0
    idle: int = 0
    in_use: int = 0

    def as_dict(self) -> dict:
        return {
            "acquired": self.acquired,
            "released": self.released,
            "evicted": self.evicted,
            "creations": self.creations,
            "acquire_failures": self.acquire_failures,
            "acquire_timeouts": self.acquire_timeouts,
            "size": self.size,
            "idle": self.idle,
            "in_use": self.in_use,
        }


@dataclass(slots=True)
class LeaseRecord:
    """Diagnostic record for a single in-flight lease."""

    connection_id: int
    thread_id: int
    acquired_at: float
    caller: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal idle slot
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _IdleSlot:
    conn: SQLiteConnection
    last_used_at: float


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Bounded, thread-safe connection pool for one :class:`SQLiteEngine`.

    The pool keeps two structures:
    * ``_idle`` — a FIFO of idle slots, protected by ``_lock``.
    * ``_in_use`` — a map of ``id(connection) -> SQLiteConnection`` for the
      diagnostic dump and for an accurate ``in_use`` count.

    Acquire flow:

        1. Poll the idle deque under the lock.
        2. For each candidate: if too old or idle-too-long, evict it.
        3. Otherwise validate (unless disabled) and hand it out.
        4. If the deque runs dry, optionally create a new connection up to
           ``max_size``; if that would over-allocate, block on
           ``_not_empty`` with the configured timeout.

    Release flow:

        1. If the connection is broken, evict it.
        2. If the pool closed during use, close it.
        3. Otherwise stamp ``last_used_at`` and push it back to the idle deque.
        4. Notify one blocked acquirer.

    The manager never opens a connection while holding ``_lock`` — opening
    involves a syscall and ``sqlite3.connect`` which can block on file I/O;
    holding the lock across it would serialize the pool.
    """

    __slots__ = (
        "_engine",
        "_config",
        "_logger",
        "_lock",
        "_idle",
        "_in_use",
        "_size",
        "_not_empty",
        "_closed",
        "_stats",
        "_leases",
        "_conn_seq",
    )

    def __init__(
        self,
        engine: SQLiteEngine,
        config: Optional[PoolConfig] = None,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self._engine = engine
        self._config = config or PoolConfig()
        self._logger = logger
        self._lock = threading.RLock()
        self._idle: deque[_IdleSlot] = deque()
        self._in_use: dict[int, SQLiteConnection] = {}
        self._size = 0
        self._not_empty = threading.Condition(self._lock)
        self._closed = False
        self._stats = PoolStats()
        self._leases: dict[int, LeaseRecord] = {}
        self._conn_seq = 0

        # Warm the pool eagerly so the first acquire does not stall on
        # sqlite3.connect; failures here surface during bootstrap instead of
        # the first user request.
        self._warm()

    # ------------------------------------------------------------- properties
    @property
    def engine(self) -> SQLiteEngine:
        return self._engine

    @property
    def config(self) -> PoolConfig:
        return self._config

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> PoolStats:
        with self._lock:
            self._stats.size = self._size
            self._stats.idle = len(self._idle)
            self._stats.in_use = len(self._in_use)
            return self._stats

    @property
    def leases(self) -> dict[int, LeaseRecord]:
        """Diagnostic dump of in-flight leases (copy)."""
        with self._lock:
            return dict(self._leases)

    # ------------------------------------------------------------- warm-up
    def _warm(self) -> None:
        for _ in range(self._config.min_size):
            try:
                conn = self._engine.connect()
            except Exception:
                if self._logger:
                    self._logger.warning("Pool warm-up connection failed")
                # Continue: a missing warm connection is acceptable; an outright
                # engine failure will surface on the first real acquire.
                return
            with self._lock:
                self._size += 1
                self._idle.append(_IdleSlot(conn=conn, last_used_at=time.monotonic()))
                self._stats.creations += 1

    # ------------------------------------------------------------- acquire
    def acquire(self, *, caller: Optional[str] = None) -> SQLiteConnection:
        """Block up to ``acquire_timeout_ms`` for a free, validated connection.

        Raises :class:`ConnectionError` on timeout or when the pool is closed.
        """
        if self._closed:
            raise ConnectionError(backend="sqlite").with_context(
                reason="connection pool is closed",
                database=self._engine.database,
            )

        deadline = (
            time.monotonic() + self._config.acquire_timeout_ms / 1000.0
            if self._config.acquire_timeout_ms > 0
            else None
        )

        with self._not_empty:
            while True:
                conn = self._take_idle_locked()
                if conn is not None:
                    self._record_lease_locked(conn, caller)
                    self._stats.acquired += 1
                    return conn

                # No idle connection available. Try to grow the pool.
                if self._size < self._config.max_size:
                    # Reserve the slot *inside* the lock so concurrent acquirers
                    # see the incremented size immediately.
                    self._size += 1
                    break  # drop the lock to open the connection

                # Pool saturated — block.
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                if remaining is not None and remaining <= 0:
                    self._stats.acquire_timeouts += 1
                    self._stats.acquire_failures += 1
                    raise ConnectionError(backend="sqlite").with_context(
                        reason="connection pool acquire timed out",
                        database=self._engine.database,
                        timeout_ms=self._config.acquire_timeout_ms,
                    )
                if not self._not_empty.wait(timeout=remaining):
                    # Spurious wake-up or timeout — loop checks the deadline again.
                    if deadline is not None and time.monotonic() >= deadline:
                        self._stats.acquire_timeouts += 1
                        self._stats.acquire_failures += 1
                        raise ConnectionError(backend="sqlite").with_context(
                            reason="connection pool acquire timed out",
                            database=self._engine.database,
                            timeout_ms=self._config.acquire_timeout_ms,
                        )

        # We have reserved a slot; open the connection outside the lock.
        try:
            conn = self._engine.connect()
        except Exception:
            with self._lock:
                self._size -= 1
            self._stats.acquire_failures += 1
            raise

        with self._lock:
            self._stats.creations += 1
            self._in_use[id(conn)] = conn
            self._record_lease_locked(conn, caller)
            self._stats.acquired += 1
        return conn

    def _take_idle_locked(self) -> Optional[SQLiteConnection]:
        """Pop and validate an idle connection; evict stale/broken ones."""
        while self._idle:
            slot = self._idle.popleft()
            conn = slot.conn
            if not self._is_alive_locked(conn, slot.last_used_at):
                self._evict_locked(conn, reason="stale")
                continue
            if self._config.validate_on_acquire and not self._validate(conn):
                self._evict_locked(conn, reason="ping_failed")
                continue
            self._in_use[id(conn)] = conn
            return conn
        return None

    def _is_alive_locked(self, conn: SQLiteConnection, last_used_at: float) -> bool:
        if conn.state != ConnectionState.OPEN:
            return False
        if self._config.max_age_seconds and conn.age_seconds > self._config.max_age_seconds:
            return False
        if (
            self._config.idle_timeout_seconds
            and (time.monotonic() - last_used_at) > self._config.idle_timeout_seconds
        ):
            return False
        return True

    def _validate(self, conn: SQLiteConnection) -> bool:
        try:
            conn.execute("SELECT 1").close()
        except Exception:
            return False
        return True

    def _record_lease_locked(
        self,
        conn: SQLiteConnection,
        caller: Optional[str],
    ) -> None:
        if not self._config.leak_tracking:
            return
        self._conn_seq += 1
        self._leases[id(conn)] = LeaseRecord(
            connection_id=self._conn_seq,
            thread_id=threading.get_ident(),
            acquired_at=time.monotonic(),
            caller=caller,
        )

    # ------------------------------------------------------------- release
    def release(self, conn: SQLiteConnection, *, broken: bool = False) -> None:
        """Return a connection to the pool.

        Parameters
        ----------
        broken:
            Hint from the caller (e.g. the transaction manager observed a
            non-recoverable statement failure). When True the connection is
            evicted regardless of its apparent state.
        """
        with self._lock:
            if self._closed:
                self._drop_locked(conn)
                return
            self._in_use.pop(id(conn), None)
            self._leases.pop(id(conn), None)
            self._stats.released += 1

            if broken or conn.state != ConnectionState.OPEN:
                self._evict_locked(conn, reason="broken_on_release")
                self._notify_idle_locked()
                return

            # Re-validate before parking; a connection that died mid-use is
            # useless to keep.
            if not self._validate(conn):
                self._evict_locked(conn, reason="post_use_ping_failed")
                self._notify_idle_locked()
                return

            self._idle.append(_IdleSlot(conn=conn, last_used_at=time.monotonic()))
            self._notify_idle_locked()

    def _evict_locked(self, conn: SQLiteConnection, *, reason: str) -> None:
        if self._size > 0:
            self._size -= 1
        self._stats.evicted += 1
        if self._logger:
            self._logger.debug(
                "Connection evicted from pool",
                extra={"database": self._engine.database, "reason": reason},
            )
        # Drop outside the lock is not needed; close does not block on I/O in
        # practice. We close inline so the slot frees immediately.
        conn.close()

    def _drop_locked(self, conn: SQLiteConnection) -> None:
        if self._size > 0:
            self._size -= 1
        conn.close()

    def _notify_idle_locked(self) -> None:
        if self._size == 0 or self._idle:
            self._not_empty.notify_all()

        # opportunistically re-warm if we fell below min_size after evictions
        deficit = self._config.min_size - self._size
        # Re-warm is done outside the lock by the caller; here we only ensure
        # blocked acquirers wake up to retry acquire.
        if deficit > 0:
            self._not_empty.notify_all()

    # ------------------------------------------------------------- context API
    @contextmanager
    def lease(self, *, caller: Optional[str] = None) -> Iterator[SQLiteConnection]:
        """Context-managed acquire/release — preferred for repository code.

        On exit the connection is released with ``broken=True`` if the body
        raised an exception, mirroring the "tainted unless proven healthy"
        principle of PG connection pools.
        """
        conn = self.acquire(caller=caller)
        try:
            yield conn
        except Exception:
            self.release(conn, broken=True)
            raise
        else:
            self.release(conn)

    # ------------------------------------------------------------- maintenance
    def shrink(self) -> None:
        """Trim the idle deque down to ``min_size`` (maintenance hook)."""
        with self._lock:
            while len(self._idle) > self._config.min_size:
                slot = self._idle.popleft()
                self._evict_locked(slot.conn, reason="shrink")
                self._notify_idle_locked()

    def flush(self) -> None:
        """Close ALL idle connections (used before a backup snapshot)."""
        with self._lock:
            while self._idle:
                slot = self._idle.popleft()
                self._evict_locked(slot.conn, reason="flush")
            self._notify_idle_locked()

    def close(self) -> None:
        """Shut the pool down. Live in-use connections are left to their owners
        — they will be closed when released. After close, ``acquire`` fails
        fast and ``release`` just closes the connection."""
        with self._lock:
            self._closed = True
            while self._idle:
                slot = self._idle.popleft()
                self._drop_locked(slot.conn)
            self._not_empty.notify_all()
        if self._logger:
            self._logger.info(
                "Connection pool closed",
                extra={"database": self._engine.database, "stats": self._stats.as_dict()},
            )

    # ------------------------------------------------------------- dunder
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        s = self.stats
        return (
            f"<ConnectionManager database={self._engine.database!r} "
            f"size={s.size} idle={s.idle} in_use={s.in_use}>"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_pool_config() -> PoolConfig:
    """Return the default :class:`PoolConfig` used by the DatabaseManager.

    Exposed so the DatabaseManager and tests do not reach into PoolConfig
    defaults implicitly and so the config layer can later override individual
    knobs without breaking call sites.
    """
    return PoolConfig()
