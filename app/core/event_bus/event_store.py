# app/core/event_bus/event_store.py
"""
Durable, queryable event store for the AIOS Event Bus.
======================================================
Every event the bus accepts is offered to the store. The store provides:

* a bounded **in-memory ring buffer** for fast "recent events" queries used by
  the FG5 developer dashboard and live debugging;
* optional **SQLite persistence** (matching the core database stack) for audit,
  crash-recovery replay, and correlation-chain reconstruction;
* **query** helpers by name, category, correlation id, and time window;
* **replay** of persisted events back through a sink (e.g. the bus) so
  interrupted flows can be resumed after a restart.

Round-trip fidelity is delegated to :class:`EventSerializer`, so a replayed
event keeps its original identity, timing, and lifecycle status rather than
being re-stamped.

Thread-safe. SQLite access uses a dedicated connection guarded by a lock, with
WAL mode enabled to align with the recovery strategy used elsewhere in core.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from app.core.constants.events import EventCategory
from app.core.event_bus.event_serializer import EventSerializer
from app.core.event_bus.event_types import Event
from app.core.exceptions import EventError
from app.logging import Logger

__all__ = ["EventStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL,
    source         TEXT,
    priority       INTEGER,
    status         TEXT,
    timestamp      REAL NOT NULL,
    correlation_id TEXT,
    causation_id   TEXT,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
"""


class EventStore:
    """Records events in memory and (optionally) in SQLite.

    Parameters
    ----------
    db_path:
        Path to the SQLite file. When ``None`` the store is memory-only.
    buffer_size:
        Capacity of the in-memory ring buffer of most-recent events.
    logger:
        Optional logger for store diagnostics.
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        *,
        buffer_size: int = 1000,
        logger: Optional[Logger] = None,
    ) -> None:
        self._buffer: Deque[Event] = deque(maxlen=buffer_size)
        self._lock = threading.RLock()
        self._logger = logger
        self._db_path = str(db_path) if db_path is not None else None
        self._conn: Optional[sqlite3.Connection] = None
        if self._db_path is not None:
            self._init_db()

    # ------------------------------------------------------------- database
    def _init_db(self) -> None:
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[arg-type]
            self._conn = sqlite3.connect(
                self._db_path, check_same_thread=False  # guarded by self._lock
            )
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise EventError(
                f"Failed to initialize event store at {self._db_path!r}",
                cause=exc,
            ) from exc

    @property
    def persistent(self) -> bool:
        return self._conn is not None

    # --------------------------------------------------------------- record
    def record(self, event: Event) -> None:
        """Append ``event`` to the ring buffer and persist it if enabled.

        Persistence failures are logged but never raised into the publish path,
        so a store issue cannot block live event delivery.
        """
        with self._lock:
            self._buffer.append(event)
            if self._conn is None:
                return
            try:
                ctx = event.context
                self._conn.execute(
                    "INSERT OR REPLACE INTO events "
                    "(event_id, name, category, source, priority, status, "
                    " timestamp, correlation_id, causation_id, payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.name,
                        event.category.value,
                        event.source,
                        int(event.priority) if event.priority is not None else None,
                        event.status.value,
                        event.timestamp,
                        ctx.correlation_id if ctx else None,
                        ctx.causation_id if ctx else None,
                        EventSerializer.serialize(event),
                    ),
                )
                self._conn.commit()
            except (sqlite3.Error, Exception) as exc:  # noqa: BLE001 - never block publish
                if self._logger:
                    self._logger.error(
                        "Failed to persist event",
                        extra={"event": event.name, "event_id": event.event_id, "error": str(exc)},
                    )

    # ---------------------------------------------------------------- recent
    def recent(self, limit: int = 100) -> List[Event]:
        """Return the most recent events from the in-memory buffer."""
        with self._lock:
            items = list(self._buffer)
        return items[-limit:][::-1]

    # ---------------------------------------------------------------- query
    def query(
        self,
        *,
        name: Optional[str] = None,
        category: Optional[EventCategory] = None,
        correlation_id: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 500,
    ) -> List[Event]:
        """Query persisted events with optional filters (persistent stores only).

        For memory-only stores, filters are applied to the ring buffer instead.
        """
        if self._conn is None:
            return self._query_buffer(name, category, correlation_id, since, until, limit)

        clauses: List[str] = []
        params: List[Any] = []
        if name is not None:
            clauses.append("name = ?"); params.append(name)
        if category is not None:
            clauses.append("category = ?"); params.append(category.value)
        if correlation_id is not None:
            clauses.append("correlation_id = ?"); params.append(correlation_id)
        if since is not None:
            clauses.append("timestamp >= ?"); params.append(since)
        if until is not None:
            clauses.append("timestamp <= ?"); params.append(until)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT payload FROM events {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise EventError("Event store query failed", cause=exc) from exc

        return [EventSerializer.deserialize(row[0]) for row in rows]

    def correlation_chain(self, correlation_id: str) -> List[Event]:
        """Return every event in a flow, ordered oldest → newest."""
        events = self.query(correlation_id=correlation_id, limit=10_000)
        return sorted(events, key=lambda e: e.timestamp)

    def _query_buffer(
        self,
        name: Optional[str],
        category: Optional[EventCategory],
        correlation_id: Optional[str],
        since: Optional[float],
        until: Optional[float],
        limit: int,
    ) -> List[Event]:
        with self._lock:
            items = list(self._buffer)

        def keep(e: Event) -> bool:
            if name is not None and e.name != name:
                return False
            if category is not None and e.category is not category:
                return False
            if correlation_id is not None and e.correlation_id != correlation_id:
                return False
            if since is not None and e.timestamp < since:
                return False
            if until is not None and e.timestamp > until:
                return False
            return True

        return [e for e in reversed(items) if keep(e)][:limit]

    # --------------------------------------------------------------- replay
    def replay(
        self,
        sink: Callable[[Event], Any],
        *,
        correlation_id: Optional[str] = None,
        since: Optional[float] = None,
        name: Optional[str] = None,
    ) -> int:
        """Re-emit persisted events through ``sink`` in chronological order.

        Used by the Recovery Manager to resume interrupted flows after a
        restart. Returns the number of events replayed.
        """
        events = self.query(
            name=name, correlation_id=correlation_id, since=since, limit=100_000
        )
        events.sort(key=lambda e: e.timestamp)
        count = 0
        for event in events:
            try:
                sink(event)
                count += 1
            except Exception as exc:  # noqa: BLE001 - replay is best-effort
                if self._logger:
                    self._logger.error(
                        "Replay failed for event",
                        extra={"event": event.name, "event_id": event.event_id, "error": str(exc)},
                    )
        if self._logger:
            self._logger.info("Replayed events", extra={"count": count})
        return count

    # ----------------------------------------------------------- maintenance
    def prune(self, older_than_seconds: float) -> int:
        """Delete persisted events older than the cutoff. Returns rows removed."""
        if self._conn is None:
            return 0
        cutoff = time.time() - older_than_seconds
        with self._lock:
            try:
                cur = self._conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
                self._conn.commit()
                return cur.rowcount
            except sqlite3.Error as exc:
                raise EventError("Event store prune failed", cause=exc) from exc

    def count(self) -> int:
        """Total persisted event count (or buffer size for memory-only stores)."""
        if self._conn is None:
            with self._lock:
                return len(self._buffer)
        with self._lock:
            try:
                return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            except sqlite3.Error as exc:
                raise EventError("Event store count failed", cause=exc) from exc

    def clear_buffer(self) -> None:
        """Empty the in-memory ring buffer (does not touch persisted rows)."""
        with self._lock:
            self._buffer.clear()

    def close(self) -> None:
        """Flush and close the SQLite connection. Call on shutdown."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                finally:
                    self._conn = None

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        mode = "sqlite" if self.persistent else "memory"
        return f"<EventStore mode={mode} buffer={len(self._buffer)}>"
