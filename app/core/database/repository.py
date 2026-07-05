# app/core/database/repository.py
"""
Generic repository for the AIOS metadata + audit SQLite stores.

The 13 metadata tables plus the 4 audit tables defined under ``models/`` are
the durable backing for every administrative fact in the assistant (admins,
permissions, conversation history, scheduled tasks, tool history, audit records,
plugin registry, etc.). Each feature group owns one or more repositories that
read and write those tables.

Rather than have every feature group re-implement ``INSERT/SELECT/UPDATE/
DELETE`` with the same quoting, JSON-serialization, trace-id stamping, and
integrity-error translation, the :class:`Repository` base class captures the
shared contract and exposes typed convenience operations. Subclasses remain
free to drop down to raw SQL via :meth:`execute` for complex queries.

Design rules
------------
* **Fail loud on misuse** — every call passes the active :class:`Session` so
  the caller is forced to be transaction-aware; there is no hidden "find an
  ambient session" magic that would tempt callers to skip the transaction
  boundary.
* **JSON-aware columns** — declarations in ``models/metadata.py`` mark
  JSON-bearing columns by name (see :class:`JsonColumns`); the repository
  automatically ``json.dumps`` Python dicts/lists on write and parses them
  back on read so callers never touch raw JSON strings.
* **Identifier quoting** — never trust caller-supplied identifiers to be
  SQL-safe; ``_q`` quotes every column/table name and ``_lit`` quotes every
  literal.
* **Pagination** — :meth:`paginate` returns a :class:`Page` with a cursor
  for stable ordering on the user-supplied sort key. The repository never
  uses ``OFFSET`` for production paging because offset-paging on a WAL
  database regresses linearly with depth.
* **Audit correlation** — every emitted row uses the session's ``trace_id``
  as a default for trace columns, so a single audit query joins all writes
  in a causal chain.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → connection_manager →
session_manager → transaction_manager → ``models/{base,metadata,audit}`` → here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from app.core.database.models.base import quote_value
from app.core.database.models.metadata import TableNames as MD
from app.core.database.session_manager import Session
from app.core.exceptions.database import DatabaseError, IntegrityError, QueryError
from app.logging import Logger

__all__ = [
    "JsonColumns",
    "Cursor",
    "Page",
    "Repository",
    "MetadataRepository",
    "Timestamps",
]


# ---------------------------------------------------------------------------
# JSON-column catalog
# ---------------------------------------------------------------------------


class JsonColumns:
    """Column-name sets that hold JSON payloads.

    Repositories declared in :mod:`models.metadata` carry several TEXT columns
    whose contents are serialized JSON (``payload``, ``metadata``, ``request``,
    ``response``, ``permissions``, ``capabilities``, ``network_policy``,
    ``value`` for ``assistant_settings``). Repository reads/writes those
    columns automatically; this catalog is the single source of truth so a
    typo in a repository cannot accidentally store a non-JSON string.
    """

    PAYLOAD = "payload"
    METADATA = "metadata"
    REQUEST = "request"
    RESPONSE = "response"
    PERMISSIONS = "permissions"
    CAPABILITIES = "capabilities"
    NETWORK_POLICY = "network_policy"
    VALUE = "value"
    CONTEXT = "context"
    PREVIOUS_STATE = "previous_state"
    RESTORED_STATE = "restored_state"

    ALL: frozenset[str] = frozenset(
        {
            PAYLOAD,
            METADATA,
            REQUEST,
            RESPONSE,
            PERMISSIONS,
            CAPABILITIES,
            NETWORK_POLICY,
            VALUE,
            CONTEXT,
            PREVIOUS_STATE,
            RESTORED_STATE,
        }
    )

    @classmethod
    def for_table(cls, table_name: str) -> frozenset[str]:
        """Return the JSON columns present on a metadata-table name.

        Built once for all metadata + audit tables. Look-ups are O(1) and the
        result is immutable so repositories can cache aggressively.
        """
        return _JSON_BY_TABLE.get(table_name, frozenset())


_JSON_BY_TABLE: dict[str, frozenset[str]] = {
    MD.MEMORY_LOGS: frozenset({JsonColumns.PAYLOAD, JsonColumns.METADATA}),
    MD.CONVERSATION_HISTORY: frozenset({JsonColumns.METADATA}),
    MD.SCHEDULED_TASKS: frozenset({JsonColumns.PAYLOAD}),
    MD.TOOL_HISTORY: frozenset({JsonColumns.REQUEST, JsonColumns.RESPONSE}),
    MD.ASSISTANT_SETTINGS: frozenset({JsonColumns.VALUE}),
    MD.SECURITY_EVENTS: frozenset({JsonColumns.PAYLOAD}),
    MD.INSTALLED_MODELS: frozenset({JsonColumns.METADATA}),
    MD.PLUGIN_REGISTRY: frozenset(
        {
            JsonColumns.PERMISSIONS,
            JsonColumns.CAPABILITIES,
            JsonColumns.NETWORK_POLICY,
        }
    ),
    # audit tables
    "audit_records": frozenset({JsonColumns.PAYLOAD, JsonColumns.CONTEXT}),
    "recovery_operations": frozenset({JsonColumns.METADATA}),
}
# ``search_cache.result`` is JSON too but it lives in a different table-shape
# (cached provider response); include it explicitly here:
_JSON_BY_TABLE[MD.SEARCH_CACHE] = frozenset({"result"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _q(identifier: str) -> str:
    """Quote a SQL identifier (column / table name)."""
    if not identifier:
        raise DatabaseError("Empty SQL identifier")
    return f'"{identifier.replace(chr(34), chr(34) + chr(34))}"'


def _lit(value: Any) -> str:
    """Render a Python value as a SQL literal fragment."""
    return quote_value(value)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with seconds precision and a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Timestamps:
    """Standard timestamp column names used across metadata tables."""

    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    EXPIRES_AT = "expires_at"
    LAST_USED_AT = "last_used_at"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Cursor:
    """Opaque paging cursor for stable keyset pagination.

    Encode the last seen value of the sort key (and its rowid as a tie-breaker)
    so subsequent calls continue past rows added concurrently without skipping
    or duplicating. The cursor is treated as opaque by callers; only the
    repository that issued it should interpret its contents.
    """

    last_key: Any
    last_rowid: int

    def as_tuple(self) -> Tuple[Any, int]:
        return self.last_key, self.last_rowid


@dataclass(slots=True)
class Page:
    """A single page of repository rows."""

    rows: List[Mapping[str, Any]]
    next_cursor: Optional[Cursor]
    has_more: bool
    page_size: int
    total_estimate: Optional[int] = None

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------


def _decode_row(row: Any, json_columns: frozenset[str]) -> dict:
    """Convert a ``sqlite3.Row`` to a dict, parsing known JSON columns."""
    out: dict = {k: row[k] for k in row.keys()}
    for col in json_columns:
        if col not in out:
            continue
        raw = out[col]
        if raw is None or isinstance(raw, (dict, list)):
            continue
        if isinstance(raw, (bytes, bytearray)):
            continue
        if isinstance(raw, str) and raw:
            try:
                out[col] = json.loads(raw)
            except (TypeError, ValueError):
                # Keep the raw string; the repository decided to fetch but
                # the row may predate the JSON convention.
                pass
    return out


def _encode_value(column: str, value: Any, json_columns: frozenset[str]) -> Any:
    """Encode a Python value for binding, serializing JSON columns."""
    if column in json_columns and value is not None:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str, ensure_ascii=False)
    return value


def _rowid_of(row: Any) -> int:
    """Best-effort fetch of an opaque row's rowid."""
    try:
        return int(row["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Repository base
# ---------------------------------------------------------------------------


class Repository:
    """Generic SQL helper bound to one table.

    Subclass responsibilities
    ------------------------
    * Set :attr:`table_name` to one of :class:`TableNames` (or the audit
      ``AuditTableNames`` constants).
    * Override :meth:`row_to_model` / :meth:`model_to_row` when typed
      dataclasses are wanted by the feature-group API.
    """

    __slots__ = (
        "_table_name",
        "_json_columns",
        "_logger",
    )

    def __init__(
        self,
        table_name: str,
        *,
        json_columns: Optional[frozenset[str]] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        if not table_name:
            raise DatabaseError("Repository table_name must be non-empty")
        self._table_name = table_name
        self._json_columns = json_columns if json_columns is not None else JsonColumns.for_table(table_name)
        self._logger = logger

    # ----------------------------------------------------------- properties
    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def json_columns(self) -> frozenset[str]:
        return self._json_columns

    # ----------------------------------------------------------- low-level execute
    def execute(
        self,
        session: Session,
        sql: str,
        params: Sequence[Any] = (),
    ) -> List[dict]:
        """Run a SQL statement against the session and return decoded rows."""
        try:
            cur = session.connection.execute(sql, tuple(params))
        except Exception as exc:
            self._classify_error(exc, sql)
        try:
            rows = [_decode_row(r, self._json_columns) for r in cur.fetchall()]
        finally:
            cur.close()
        return rows

    def execute_one(
        self,
        session: Session,
        sql: str,
        params: Sequence[Any] = (),
    ) -> Optional[dict]:
        rows = self.execute(session, sql, params)
        return rows[0] if rows else None

    def execute_scalar(
        self,
        session: Session,
        sql: str,
        params: Sequence[Any] = (),
    ) -> Any:
        try:
            cur = session.connection.execute(sql, tuple(params))
        except Exception as exc:
            self._classify_error(exc, sql)
        try:
            row = cur.fetchone()
        finally:
            cur.close()
        if row is None:
            return None
        keys = row.keys()
        if not keys:
            return None
        return row[keys[0]]

    # ----------------------------------------------------------- CRUD
    def insert(
        self,
        session: Session,
        row: Mapping[str, Any],
        *,
        returning: Optional[Sequence[str]] = None,
    ) -> Optional[dict]:
        """Insert a single row, returning selected columns if requested."""
        cols = list(row.keys())
        if not cols:
            raise DatabaseError(f"Cannot insert empty row into {self._table_name}")

        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(_q(c) for c in cols)
        values = tuple(_encode_value(c, row[c], self._json_columns) for c in cols)

        returning_sql = ""
        if returning:
            returning_sql = " RETURNING " + ", ".join(_q(c) for c in returning)

        sql = (
            f"INSERT INTO {_q(self._table_name)} ({col_sql}) "
            f"VALUES ({placeholders}){returning_sql}"
        )
        try:
            cur = session.connection.execute(sql, values)
        except Exception as exc:
            self._classify_error(exc, sql)
        try:
            if returning:
                fetched = cur.fetchone()
                if fetched is None:
                    return None
                return _decode_row(fetched, self._json_columns)
            return {"id": cur.lastrowid}
        finally:
            cur.close()

    def insert_many(
        self,
        session: Session,
        rows: Iterable[Mapping[str, Any]],
    ) -> int:
        """Bulk insert rows. Returns the number of rows changed."""
        rows_list = list(rows)
        if not rows_list:
            return 0
        cols = list(rows_list[0].keys())
        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(_q(c) for c in cols)
        sql = f"INSERT INTO {_q(self._table_name)} ({col_sql}) VALUES ({placeholders})"
        values_seq = [
            tuple(_encode_value(c, r.get(c), self._json_columns) for c in cols)
            for r in rows_list
        ]
        try:
            cur = session.connection.executemany(sql, values_seq)
        except Exception as exc:
            self._classify_error(exc, sql)
        changed = cur.rowcount if cur.rowcount is not None else 0
        cur.close()
        return int(changed)

    def find_by_id(self, session: Session, row_id: int) -> Optional[dict]:
        return self.execute_one(
            session,
            f"SELECT * FROM {_q(self._table_name)} WHERE id = ?",
            (row_id,),
        )

    def find_by(
        self,
        session: Session,
        *,
        where: Mapping[str, Any],
        limit: Optional[int] = None,
        order_by: Optional[Sequence[str]] = None,
    ) -> List[dict]:
        """Find rows matching an equality-AND ``where`` dict."""
        if not where:
            raise DatabaseError("find_by requires at least one where column")
        clauses = " AND ".join(f"{_q(c)} = ?" for c in where.keys())
        params: list[Any] = list(where.values())
        sql = f"SELECT * FROM {_q(self._table_name)} WHERE {clauses}"
        if order_by:
            sql += " ORDER BY " + ", ".join(_q(c) for c in order_by)
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.execute(session, sql, tuple(params))

    def find_one_by(self, session: Session, where: Mapping[str, Any]) -> Optional[dict]:
        rows = self.find_by(session, where=where, limit=1)
        return rows[0] if rows else None

    def update(
        self,
        session: Session,
        *,
        set_: Mapping[str, Any],
        where: Mapping[str, Any],
    ) -> int:
        """``UPDATE ... SET ... WHERE col=val AND ...``. Returns rows changed.

        ``set_`` may include timestamp columns — when ``updated_at`` is present
        and ``None``, the repository fills it with the current UTC time so
        callers do not have to remember the convention. ``set_`` must be
        non-empty and ``where`` must be non-empty to prevent accidental
        full-table updates.
        """
        if not set_:
            raise DatabaseError("UPDATE requires at least one SET column")
        if not where:
            raise DatabaseError("UPDATE requires a WHERE clause to prevent full-table writes")

        patched = dict(set_)
        if "updated_at" in patched and patched["updated_at"] is None:
            patched["updated_at"] = _now_iso()

        set_sql = ", ".join(f"{_q(c)} = ?" for c in patched.keys())
        where_sql = " AND ".join(f"{_q(c)} = ?" for c in where.keys())
        sql = f"UPDATE {_q(self._table_name)} SET {set_sql} WHERE {where_sql}"
        params: list[Any] = [
            _encode_value(c, patched[c], self._json_columns) for c in patched.keys()
        ] + list(where.values())
        try:
            cur = session.connection.execute(sql, tuple(params))
        except Exception as exc:
            self._classify_error(exc, sql)
        changed = cur.rowcount if cur.rowcount is not None else 0
        cur.close()
        return int(changed)

    def delete(
        self,
        session: Session,
        *,
        where: Mapping[str, Any],
    ) -> int:
        """``DELETE ... WHERE col=val AND ...``. Returns rows deleted."""
        if not where:
            raise DatabaseError("DELETE requires a WHERE clause to prevent full-table deletes")
        where_sql = " AND ".join(f"{_q(c)} = ?" for c in where.keys())
        sql = f"DELETE FROM {_q(self._table_name)} WHERE {where_sql}"
        try:
            cur = session.connection.execute(sql, tuple(where.values()))
        except Exception as exc:
            self._classify_error(exc, sql)
        changed = cur.rowcount if cur.rowcount is not None else 0
        cur.close()
        return int(changed)

    def count(
        self,
        session: Session,
        *,
        where: Optional[Mapping[str, Any]] = None,
    ) -> int:
        sql = f"SELECT COUNT(*) FROM {_q(self._table_name)}"
        params: tuple[Any, ...] = ()
        if where:
            sql += " WHERE " + " AND ".join(f"{_q(c)} = ?" for c in where.keys())
            params = tuple(where.values())
        result = self.execute_scalar(session, sql, params)
        return int(result) if result is not None else 0

    # ----------------------------------------------------------- keyset pagination
    def paginate(
        self,
        session: Session,
        *,
        order_by: str,
        page_size: int = 50,
        cursor: Optional[Cursor] = None,
        where: Optional[Mapping[str, Any]] = None,
        ascending: bool = True,
    ) -> Page:
        """Keyset-paginate a table by a sortable column.

        ``order_by`` must be unique (or ``id`` is appended as a tie-breaker
        automatically). Direction respects ``ascending``. ``cursor`` is the
        opaque handle returned by a previous :class:`Page`. ``where`` is an
        equality-AND filter applied before pagination.
        """
        if page_size < 1:
            raise DatabaseError("page_size must be >= 1")
        if not order_by:
            raise DatabaseError("paginate requires an order_by column")

        where_parts: list[str] = []
        params: list[Any] = []
        if where:
            for k, v in where.items():
                where_parts.append(f"{_q(k)} = ?")
                params.append(v)

        direction = "ASC" if ascending else "DESC"
        cursor_clause = ""
        if cursor is not None:
            op = ">" if ascending else "<"
            cursor_clause = f" AND ({_q(order_by)} {op} ? OR ({_q(order_by)} = ? AND id > ?))"
            params.extend([cursor.last_key, cursor.last_key, cursor.last_rowid])

        sql = (
            f"SELECT * FROM {_q(self._table_name)}"
            + (" WHERE " + " AND ".join(where_parts) if where_parts else "")
            + cursor_clause
            + f" ORDER BY {_q(order_by)} {direction}, id ASC LIMIT {page_size + 1}"
        )
        rows = self.execute(session, sql, tuple(params))

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        next_cursor: Optional[Cursor] = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = Cursor(last_key=last.get(order_by), last_rowid=_rowid_of(last))
        return Page(
            rows=page_rows,
            next_cursor=next_cursor,
            has_more=has_more,
            page_size=page_size,
        )

    # ----------------------------------------------------------- subclass hooks
    def row_to_model(self, row: Mapping[str, Any]) -> Any:
        """Override to map a decoded row into a typed model."""
        return dict(row)

    def model_to_row(self, model: Any) -> Mapping[str, Any]:
        """Override to map a typed model into a row dict."""
        if isinstance(model, Mapping):
            return dict(model)
        if hasattr(model, "__dict__"):
            # Best-effort: drop private attrs.
            return {k: v for k, v in vars(model).items() if not k.startswith("_")}
        raise DatabaseError(
            f"Cannot convert {type(model).__name__} to row; override model_to_row",
        )

    # ----------------------------------------------------------- error translation
    def _classify_error(self, exc: BaseException, sql: str) -> None:
        """Translate a driver exception into an AIOS exception.

        ``IntegrityError`` is detected by SQLite's error name so callers that
        rely on a constraint violation can catch the typed exception directly.
        Anything else is wrapped as :class:`QueryError`. Always re-raises.
        """
        # sqlite3.IntegrityError lives at the driver layer; we check by name
        # so this module does not import sqlite3 directly.
        cls_name = type(exc).__name__
        if cls_name == "IntegrityError":
            raise IntegrityError(constraint=str(exc), cause=exc) from exc
        raise QueryError(statement=sql, cause=exc) from exc

    # ----------------------------------------------------------- introspect
    def all_timestamp_columns(self) -> Tuple[str, ...]:
        """Return the table's timestamp columns for repositories that wish to
        auto-stamp them on insert/update."""
        return _TIMESTAMP_BY_TABLE.get(self._table_name, ())


_TIMESTAMP_BY_TABLE: dict[str, tuple[str, ...]] = {
    MD.ADMINS: ("created_at", "updated_at"),
    MD.PERMISSIONS: ("granted_at",),
    MD.MEMORY_LOGS: ("created_at", "updated_at"),
    MD.CONVERSATION_HISTORY: ("created_at",),
    MD.SCHEDULED_TASKS: ("created_at", "updated_at"),
    MD.TOOL_HISTORY: ("created_at",),
    MD.ASSISTANT_SETTINGS: ("updated_at",),
    MD.SECURITY_EVENTS: ("created_at",),
    MD.SEARCH_CACHE: ("created_at", "last_used_at"),
    MD.VOICE_PROFILES: ("created_at", "updated_at"),
    MD.INSTALLED_MODELS: ("installed_at",),
    MD.PLUGIN_REGISTRY: ("installed_at", "updated_at"),
    MD.SCHEMA_MIGRATIONS: ("applied_at",),
}


# ---------------------------------------------------------------------------
# MetadataRepository — convenience base for all metadata tables
# ---------------------------------------------------------------------------


class MetadataRepository(Repository):
    """Base class for repositories over the metadata schema.

    Adds table-aware defaults (auto-stamp created_at/updated_at if absent)
    so feature-group repositories can stay focused on domain logic.
    """

    __slots__ = ()

    def insert(self, session, row, *, returning=None):  # type: ignore[override]
        stamped = self._stamp_timestamps(dict(row))
        return super().insert(session, stamped, returning=returning)

    def insert_many(self, session, rows):  # type: ignore[override]
        stamped_rows = [self._stamp_timestamps(dict(r)) for r in rows]
        return super().insert_many(session, stamped_rows)

    def update(self, session, *, set_, where):  # type: ignore[override]
        # We let the base class fill updated_at when callers left it None;
        # here we only inject it when the table has an updated_at column at
        # all and the caller did not mention it.
        ts_cols = self.all_timestamp_columns()
        if "updated_at" in ts_cols and "updated_at" not in set_:
            set_ = {**set_, "updated_at": _now_iso()}
        return super().update(session, set_=set_, where=where)

    def _stamp_timestamps(self, row: dict) -> dict:
        ts_cols = self.all_timestamp_columns()
        if not ts_cols:
            return row
        now = _now_iso()
        for col in ts_cols:
            if col == "updated_at" and (col not in row or row.get(col) is None):
                row.setdefault(col, now)
            elif col == "created_at" and (col not in row or row.get(col) is None):
                row.setdefault(col, now)
            elif col not in row:
                # Insert "now" only for created columns; for others leave
                # the caller's responsibility.
                if col in ("created_at", "installed_at", "granted_at", "applied_at"):
                    row[col] = now
        return row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += ["MetadataRepository", "TableNames", "Timestamps", "Cursor", "Page"]
