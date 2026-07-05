# app/core/database/sqlite/engine.py
"""
SQLite engine — owns the *policy* for opening SQLite connections for a single
database file. The engine is the boundary between "I want a connection to the
metadata DB" and "here is a configured, PRAGMA-applied :class:`SQLiteConnection`".

Why a separate engine instead of just calling ``connect_sqlite()`` everywhere?
------------------------------------------------------------------------
* **One place per database** — every database file (metadata, secure, search
  cache, semantic cache) has exactly one :class:`SQLiteEngine` instance built
  by the :class:`DatabaseManager`. The connection manager / session manager /
  repository layer ask the engine for connections; they never hardcode a path
  or a PRAGMA preset.
* **Consistent PRAGMA preset** — the resolved preset lives on the engine so a
  config reload can refresh it without touching call sites.
* **Isolation level policy** — the engine knows whether the database is in
  auto-commit mode or driven by an external transaction manager and configures
  the underlying connection accordingly.
* **Open/close observability** — the engine emits diagnostics through the
  injected logger and tracks lifetime stats (opens, closes, failures) so the
  health manager has a single source of truth per database.
* **Path validation** — the engine refuses to open a database whose parent
  directory does not exist, preventing the silent creation of ``data/``-shaped
  typos in the wrong working directory.

The engine does NOT pool connections — that is the connection manager's job.
It is intentionally a stateless policy object so multiple pools (one per
worker process, for example) can share the same engine safely.

Dependency order
----------------
constants → exceptions → configs → logging → ``sqlite/pragmas`` →
``sqlite/connection`` → here.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Union

from app.core.database.sqlite.connection import (
    ConnectionState,
    SQLiteConnection,
    connect_sqlite,
)
from app.core.database.sqlite.pragmas import PragmaPreset, preset_for
from app.core.exceptions.database import ConnectionError, DatabaseError
from app.logging import Logger

__all__ = [
    "SQLiteEngineConfig",
    "SQLiteEngine",
    "EngineStats",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SQLiteEngineConfig:
    """Immutable policy for a single SQLite database file.

    Attributes
    ----------
    database:
        Filesystem path, or ``":memory:"`` for an in-memory scratch DB.
    encrypted:
        When True the engine resolves the ``encrypted`` PRAGMA preset (used by
        the SQLCipher engine, which subclasses :class:`SQLiteEngine`). Plain
        SQLite engines leave this False.
    in_memory:
        True for ``":memory:"`` databases; selects the ``memory`` preset.
    read_only:
        True when the connection is used for integrity checks / backups and
        must never write. Selects the ``integrity`` preset.
    check_same_thread:
        Forwarded to ``sqlite3.connect``. The connection manager may pass
        ``False`` *only* when it serializes access externally.
    isolation_level:
        Forwarded to ``sqlite3.connect``. The transaction manager requests
        ``None`` so it drives ``BEGIN``/``COMMIT``/``ROLLBACK`` manually.
    preset_override:
        Optional explicit :class:`PragmaPreset`. When provided it overrides
        the policy-derived preset; used by the SQLCipher engine to inject
        cipher-specific PRAGMAs after key binding.
    """

    database: Union[str, Path]
    encrypted: bool = False
    in_memory: bool = False
    read_only: bool = False
    check_same_thread: bool = True
    isolation_level: Optional[str] = None
    preset_override: Optional[PragmaPreset] = None

    def __post_init__(self) -> None:
        if isinstance(self.database, Path) and str(self.database) != ":memory:":
            # Path validation: parent must exist when backed by a file. We do
            # NOT create it here; directory creation is the bootstrap's job.
            if self.database.parent and not self.database.parent.exists():
                raise DatabaseError(
                    f"SQLite database parent directory does not exist: {self.database.parent}",
                )
        if self.in_memory and str(self.database) != ":memory:":
            # Treat the special in-memory flag consistently.
            object.__setattr__(self, "database", ":memory:")
        if self.encrypted and self.in_memory:
            raise DatabaseError("In-memory encrypted SQLite is not supported")

    @property
    def is_memory(self) -> bool:
        return self.in_memory or str(self.database) == ":memory:"

    def resolve_preset(self) -> PragmaPreset:
        if self.preset_override is not None:
            return self.preset_override
        return preset_for(
            encrypted=self.encrypted,
            in_memory=self.in_memory,
            read_only=self.read_only,
        )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EngineStats:
    """Cumulative lifetime stats for an engine — read by the health manager."""

    opened: int = 0
    closed: int = 0
    failed: int = 0
    in_flight: int = 0

    def reset(self) -> None:
        self.opened = 0
        self.closed = 0
        self.failed = 0
        self.in_flight = 0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SQLiteEngine:
    """Policy object that opens :class:`SQLiteConnection` instances for one DB.

    The engine is thread-safe via an internal RLock that protects only the
    stats counters — opening a connection itself delegates to the stdlib
    ``sqlite3.connect`` which is responsible for its own thread safety. The
    engine never holds the lock across a blocking I/O call.
    """

    __slots__ = (
        "_config",
        "_preset",
        "_logger",
        "_stats",
        "_lock",
        "_closed",
    )

    def __init__(
        self,
        config: SQLiteEngineConfig,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self._config = config
        self._preset = config.resolve_preset()
        self._logger = logger
        self._stats = EngineStats()
        self._lock = threading.RLock()
        self._closed = False

    # ------------------------------------------------------------- properties
    @property
    def database(self) -> str:
        return str(self._config.database)

    @property
    def is_memory(self) -> bool:
        return self._config.is_memory

    @property
    def is_encrypted(self) -> bool:
        return self._config.encrypted

    @property
    def is_read_only(self) -> bool:
        return self._config.read_only

    @property
    def preset(self) -> PragmaPreset:
        return self._preset

    @property
    def config(self) -> SQLiteEngineConfig:
        return self._config

    @property
    def stats(self) -> EngineStats:
        with self._lock:
            return EngineStats(
                opened=self._stats.opened,
                closed=self._stats.closed,
                failed=self._stats.failed,
                in_flight=self._stats.in_flight,
            )

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------- open policy
    def _begin_open(self) -> None:
        if self._closed:
            raise ConnectionError(backend="sqlite").with_context(
                reason="engine is closed",
                database=self.database,
            )

    def _record_open(self) -> None:
        with self._lock:
            self._stats.opened += 1
            self._stats.in_flight += 1

    def _record_close(self) -> None:
        with self._lock:
            self._stats.closed += 1
            if self._stats.in_flight > 0:
                self._stats.in_flight -= 1

    def _record_failure(self) -> None:
        with self._lock:
            self._stats.failed += 1

    # ------------------------------------------------------------- connect API
    def connect(self) -> SQLiteConnection:
        """Open a new :class:`SQLiteConnection` using the engine's policy.

        The caller is responsible for closing the returned connection (or using
        it as a context manager). Pools obtained through :meth:`connection`
        close the connection automatically on context exit.
        """
        self._begin_open()
        try:
            conn = connect_sqlite(
                database=self._config.database,
                preset=self._preset,
                check_same_thread=self._config.check_same_thread,
                isolation_level=self._config.isolation_level,
                logger=self._logger,
            )
        except Exception:
            self._record_failure()
            raise
        self._record_open()
        if self._logger:
            self._logger.debug(
                "SQLite engine opened connection",
                extra={
                    "database": self.database,
                    "preset": self._preset.name,
                    "encrypted": self.is_encrypted,
                    "read_only": self.is_read_only,
                },
            )
        return conn

    @contextmanager
    def connection(self) -> Iterator[SQLiteConnection]:
        """Context-managed connection — closes on exit, even on exception.

        Preferred over :meth:`connect` for one-shot statements. Pools and the
        session manager use :meth:`connect` so they can hold the connection
        across multiple statements.
        """
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
            self._record_close()

    # ------------------------------------------------------------- schema API
    def execute_script(self, script: str) -> None:
        """Run a multi-statement DDL/migration script against the database.

        Opens a transient connection, runs the script, commits when not in
        auto-commit, and closes it. The migration manager prefers running
        migrations inside its own transaction manager; this entry point is for
        idempotent bootstrap scripts (e.g. ``PRAGMA`` tuning, ``VACUUM``).
        """
        if self._config.read_only:
            raise DatabaseError("Cannot execute script on a read-only engine")
        with self.connection() as conn:
            conn.executescript(script)
            # executescript issues an implicit commit; nothing else to do.

    # ------------------------------------------------------------- lifecycle
    def refresh_preset(self, preset: Optional[PragmaPreset] = None) -> None:
        """Re-resolve the PRAGMA preset (after a config reload).

        Already-open connections keep their original preset until they are
        recycled by the pool; new connections opened after this call use the
        refreshed preset.
        """
        with self._lock:
            self._preset = preset or self._config.resolve_preset()

    def close(self) -> None:
        """Mark the engine closed so further ``connect`` calls fail fast.

        Does not close any open connections — pools own their live connections
        and are responsible for draining on shutdown.
        """
        self._closed = True
        if self._logger:
            self._logger.info(
                "SQLite engine closed",
                extra={
                    "database": self.database,
                    "stats": self._stats.__dict__,
                },
            )

    # ------------------------------------------------------------- dunder
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<SQLiteEngine database={self.database!r} "
            f"encrypted={self.is_encrypted} read_only={self.is_read_only} "
            f"in_flight={self._stats.in_flight}>"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_engine_config(
    database: Union[str, Path],
    *,
    encrypted: bool = False,
    in_memory: bool = False,
    read_only: bool = False,
    check_same_thread: bool = True,
    isolation_level: Optional[str] = None,
    preset_override: Optional[PragmaPreset] = None,
) -> SQLiteEngineConfig:
    """Ergonomic constructor for :class:`SQLiteEngineConfig` used by the
    DatabaseManager's wiring stage."""
    return SQLiteEngineConfig(
        database=database,
        encrypted=encrypted,
        in_memory=in_memory,
        read_only=read_only,
        check_same_thread=check_same_thread,
        isolation_level=isolation_level,
        preset_override=preset_override,
    )
