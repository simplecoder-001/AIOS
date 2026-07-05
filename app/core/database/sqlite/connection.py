# app/core/database/sqlite/connection.py
"""
Low-level ``sqlite3`` connection wrapper used by every SQLite/SQLCipher engine.

The Python stdlib ``sqlite3.Connection`` is *not* thread-safe by default: a
connection created on one thread may not be used from another unless the
optional ``check_same_thread=False`` flag is passed *and* the caller manually
serialize access. AIOS is multi-threaded (FG1 worker pool, FG2 async runtime,
FG3 execution engine), so every connection opened here enforces a strict
single-thread ownership policy and surfaces a uniform, typed interface.

Why a wrapper instead of using ``sqlite3`` directly?
---------------------------------------------------
* **Thread-affinity enforcement** — a connection belongs to exactly one thread
  by default; attempts to share it across threads raise a :class:`ConnectionError`
  *before* the ``sqlite3`` module gets a chance to raise its own opaque
  ``ProgrammingError``.
* **Uniform row factory** — every connection uses :class:`sqlite3.Row` so
  repositories can write ``row["name"]`` instead of positional tuples, without
  re-configuring it on every acquire.
* **Cursor hygiene** — :meth:`execute`, :meth:`executemany`, and
  :meth:`executescript` return the cursor so callers can chain, but they are
  always used as context managers so they are closed even on exception.
* **PRAGMA application** — the engine feeds a :class:`PragmaPreset` into the
  constructor so that every connection is configured identically, the moment
  it is opened.
* **Lifecycle observability** — close/finalize hooks let the connection manager
  keep its poolaccurate without polling.
* **Diagnostics** — last-insert-rowid, total-changes, and the underlying
  ``sqlite3`` version are surfaced without exposing the raw driver to callers.

Dependency order
----------------
constants → exceptions → configs → logging → ``sqlite/pragmas`` → here.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence, Union

from app.core.database.sqlite.pragmas import PragmaPreset
from app.core.exceptions.database import ConnectionError, QueryError
from app.logging import Logger

__all__ = [
    "SQLiteConnection",
    "ConnectionState",
    "connect_sqlite",
]

# A SQL value bearer. We accept str/bytes/int/float/None natively; everything
# else must be serialized by the repository layer.
SQLValue = Union[str, bytes, int, float, None]
SQLParameters = Union[Sequence[SQLValue], dict[str, Any]]


class ConnectionState:
    """Connection-level lifecycle states observed by the pool/manager.

    These mirror the low-level states of ``sqlite3.Connection``; we expose them
    as a stable name set rather than boolean flags so the health manager can
    record transitions in the audit trail.
    """

    OPEN = "open"
    CLOSED = "closed"
    BROKEN = "broken"          # an exception left the connection unusable
    EXPIRED = "expired"         # pool evicted it; pending close


class SQLiteConnection:
    """Typed, thread-affine wrapper around a :class:`sqlite3.Connection`.

    Parameters
    ----------
    database:
        Filesystem path or ``":memory:"`` for an in-memory database.
    preset:
        :class:`PragmaPreset` applied in order on open. The preset is owned by
        the engine's configuration; the wrapper does not mutate it.
    check_same_thread:
        When ``True`` (the default), only the owning thread may use the
        connection. The connection manager may pass ``False`` *only* when it
        also serializes access via its own external lock — never otherwise.
    isolation_level:
        Passed verbatim to ``sqlite3.connect``. The transaction manager
        requests ``None`` so that it drives BEGIN/COMMIT/ROLLBACK manually;
        auto-transaction callers leave the default.
    logger:
        Optional logger for diagnostics. Never receives SQL payloads at INFO.
    """

    __slots__ = (
        "_conn",
        "_database",
        "_preset",
        "_owner_thread",
        "_check_same_thread",
        "_state",
        "_logger",
        "_lock",
        "_open_at",
    )

    def __init__(
        self,
        database: Union[str, Path],
        preset: PragmaPreset,
        *,
        check_same_thread: bool = True,
        isolation_level: Optional[str] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        self._database = str(database)
        self._preset = preset
        self._owner_thread = threading.get_ident()
        self._check_same_thread = check_same_thread
        self._logger = logger
        self._lock = threading.RLock()
        self._state = ConnectionState.CLOSED
        self._open_at: float = 0.0
        self._conn: Optional[sqlite3.Connection] = None

        self._open(isolation_level=isolation_level)

    # ------------------------------------------------------------------ open
    def _open(self, *, isolation_level: Optional[str]) -> None:
        try:
            self._conn = sqlite3.connect(
                self._database,
                check_same_thread=self._check_same_thread,
                isolation_level=isolation_level,
                # Use sqlite3.Row so callers can index by column name.
                row_factory=sqlite3.Row,
                # Fullyfeatured URIs are not needed; speed up path handling.
                uri=False,
            )
        except sqlite3.Error as exc:
            raise ConnectionError(backend="sqlite", cause=exc) from exc

        # Apply the configured PRAGMAs in statement order.
        if self._conn is not None and len(self._preset) > 0:
            try:
                self._conn.executescript("\n".join(self._preset.statements()))
            except sqlite3.Error as exc:
                # A PRAGMA failure leaves a fresh connection in an undefined
                # state; close and fail fast.
                self._safe_close()
                raise ConnectionError(backend="sqlite", cause=exc) from exc

        import time as _time
        self._open_at = _time.monotonic()
        self._state = ConnectionState.OPEN
        if self._logger:
            self._logger.debug(
                "SQLite connection opened",
                extra={
                    "database": self._database,
                    "preset": self._preset.name,
                    "thread": self._owner_thread,
                },
            )

    # --------------------------------------------------------------- guards
    def _ensure_thread(self) -> None:
        if (
            self._check_same_thread
            and threading.get_ident() != self._owner_thread
        ):
            raise ConnectionError(
                backend="sqlite",
            ).with_context(
                reason="connection used from a foreign thread",
                owner_thread=self._owner_thread,
                caller_thread=threading.get_ident(),
            )

    def _ensure_open(self) -> sqlite3.Connection:
        self._ensure_thread()
        if self._conn is None or self._state != ConnectionState.OPEN:
            raise ConnectionError(backend="sqlite").with_context(
                reason="connection is not open",
                state=self._state,
            )
        return self._conn

    # --------------------------------------------------------------- execute
    def execute(
        self,
        sql: str,
        parameters: Optional[SQLParameters] = None,
    ) -> sqlite3.Cursor:
        """Execute a single statement and return the cursor.

        Raises :class:`QueryError` on any driver failure so the repository layer
        never has to translate ``sqlite3`` errors itself.
        """
        conn = self._ensure_open()
        try:
            if parameters is None:
                return conn.execute(sql)
            return conn.execute(sql, parameters)
        except sqlite3.Error as exc:
            raise QueryError(statement=sql, cause=exc) from exc

    def executemany(
        self,
        sql: str,
        parameters_seq: Sequence[SQLParameters],
    ) -> sqlite3.Cursor:
        """Execute a parameterized statement against a batch of parameters."""
        conn = self._ensure_open()
        try:
            return conn.executemany(sql, parameters_seq)
        except sqlite3.Error as exc:
            raise QueryError(statement=sql, cause=exc) from exc

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Execute a multi-statement script (DDL, migrations)."""
        conn = self._ensure_open()
        try:
            return conn.executescript(script)
        except sqlite3.Error as exc:
            raise QueryError(statement=script[:500], cause=exc) from exc

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        """Context-managed cursor — closed even on exception."""
        conn = self._ensure_open()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # ----------------------------------------------------------- transactions
    def begin(self) -> None:
        """Issue ``BEGIN`` so the transaction manager has explicit control."""
        conn = self._ensure_open()
        try:
            conn.execute("BEGIN")
        except sqlite3.Error as exc:
            raise QueryError(statement="BEGIN", cause=exc) from exc

    def commit(self) -> None:
        conn = self._ensure_open()
        try:
            conn.commit()
        except sqlite3.Error as exc:
            raise QueryError(statement="COMMIT", cause=exc) from exc

    def rollback(self) -> None:
        """Best-effort rollback — never raises so exception handlers stay safe."""
        conn = self._ensure_open()
        try:
            conn.rollback()
        except sqlite3.Error as exc:
            if self._logger:
                self._logger.warning(
                    "Rollback failed; connection marked broken",
                    extra={"database": self._database, "error": str(exc)},
                )
            self._mark_broken()

    # --------------------------------------------------------------- introspect
    @property
    def last_rowid(self) -> int:
        conn = self._ensure_open()
        return int(conn.lastrowid or 0)

    @property
    def total_changes(self) -> int:
        conn = self._ensure_open()
        return int(conn.total_changes)

    @property
    def in_transaction(self) -> bool:
        conn = self._ensure_open()
        return bool(conn.in_transaction)

    @property
    def database(self) -> str:
        return self._database

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == ConnectionState.OPEN

    @property
    def owner_thread(self) -> int:
        return self._owner_thread

    @property
    def age_seconds(self) -> float:
        import time as _time
        if self._open_at == 0.0:
            return 0.0
        return _time.monotonic() - self._open_at

    @property
    def sqlite_version(self) -> str:
        return sqlite3.sqlite_version

    # ----------------------------------------------------------- introspect raw
    def raw(self) -> sqlite3.Connection:
        """Return the underlying ``sqlite3.Connection``.

        Discouraged for application code — exposed for the migration manager
        and the backup manager, which need driver-level APIs (``backup()``,
        ``iterdump()``) that are out of scope for the typed wrapper.
        """
        return self._ensure_open()

    # ------------------------------------------------------------------ close
    def _safe_close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            self._conn = None

    def _mark_broken(self) -> None:
        self._state = ConnectionState.BROKEN
        self._safe_close()

    def close(self) -> None:
        with self._lock:
            if self._state == ConnectionState.CLOSED:
                return
            self._safe_close()
            self._state = ConnectionState.CLOSED
            if self._logger:
                self._logger.debug(
                    "SQLite connection closed",
                    extra={
                        "database": self._database,
                        "age_seconds": round(self.age_seconds, 3),
                    },
                )

    # ----------------------------------------------------------- context mgmt
    def __enter__(self) -> "SQLiteConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Automatic commit/rollback only when the caller has NOT taken over
        # transaction control via ``begin()``. ``in_transaction`` is False in
        # auto-commit mode and when the outermost savepoint cleared.
        if exc is not None:
            try:
                if self.is_open and self.in_transaction:
                    self.rollback()
            finally:
                self.close()
        else:
            try:
                if self.is_open and self.in_transaction:
                    self.commit()
            finally:
                self.close()

    def __del__(self) -> None:
        # Never raise from __del__; best-effort close.
        try:
            if getattr(self, "_state", ConnectionState.CLOSED) == ConnectionState.OPEN:
                self._safe_close()
        except Exception:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def connect_sqlite(
    database: Union[str, Path],
    preset: PragmaPreset,
    *,
    check_same_thread: bool = True,
    isolation_level: Optional[str] = None,
    logger: Optional[Logger] = None,
) -> SQLiteConnection:
    """Open and return a :class:`SQLiteConnection`.

    Convenience entry point used by the SQLite engine and ad-hoc callers
    (tests, scripts). Application code should prefer going through the
    connection manager / engine so the pool can be observed and recycled.
    """
    return SQLiteConnection(
        database=database,
        preset=preset,
        check_same_thread=check_same_thread,
        isolation_level=isolation_level,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += ["SQLValue", "SQLParameters"]
