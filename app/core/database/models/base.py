# app/core/database/models/base.py
"""
Declarative table model base for the AIOS metadata database.

The metadata store (SQLite, ``METADATA_DB_FILE``) holds every relational table
required by the assistant: admins, permissions, memory logs, conversation
history, scheduled tasks, tool history, assistant settings, security events,
search cache, voice profiles, installed models, plugin registry (FG2 §20).

Design
------
The application deliberately avoids pulling SQLAlchemy / SQLModel into Phase 0
boot. Instead it uses a tiny, dependency-free, sqlite3-friendly declarative
abstraction that lets the migration manager emit DDL while keeping a typed,
introspectable schema description available at import time. Qdrant and the
knowledge graph store do not use these models.

A ``Table`` is a declaration of columns plus constraints; ``Column`` carries a
SQLite type, Python type, affinity, nullability, primary key, default and
uniqueness. The ``to_ddl()`` method produces a single ``CREATE TABLE`` SQL
statement honouring IF NOT EXISTS, foreign keys, and the configured collation —
exactly what the migration manager feeds to the engine.

Dependency order
----------------
* ``constants`` → ``exceptions`` → ``logging`` → here.
* No import from ``sqlite`` / ``connection_manager`` / ``engine`` (a pure
  declarative schema layer must stay import-safe from any backend).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.core.exceptions.database import DatabaseError

__all__ = [
    "Affinity",
    "OnDelete",
    "Column",
    "ForeignKey",
    "Index",
    "Table",
    "Schema",
]


# ---------------------------------------------------------------------------
# SQLite type affinity (per https://www.sqlite.org/datatype3.html §3.1)
# ---------------------------------------------------------------------------


class Affinity(str, Enum):
    """SQLite storage affinity rules — used by DDL emission."""

    TEXT = "TEXT"
    NUMERIC = "NUMERIC"
    INTEGER = "INTEGER"
    REAL = "REAL"
    BLOB = "BLOB"

    @classmethod
    def from_python(cls, py_type: type) -> "Affinity":
        """Best-effort mapping from a Python type to SQLite affinity."""
        if py_type in (bool, int):
            return cls.INTEGER
        if py_type is float:
            return cls.REAL
        if py_type in (bytes, bytearray):
            return cls.BLOB
        return cls.TEXT


class OnDelete(str, Enum):
    """Referential ON DELETE actions accepted by SQLite."""

    RESTRICT = "RESTRICT"
    SET_NULL = "SET NULL"
    CASCADE = "CASCADE"
    NO_ACTION = "NO ACTION"
    SET_DEFAULT = "SET DEFAULT"


# ---------------------------------------------------------------------------
# Column & constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """A single ``REFERENCES`` clause targeting a parent column."""

    table: str
    column: str
    on_delete: OnDelete = OnDelete.RESTRICT
    on_update: OnDelete = OnDelete.RESTRICT

    def to_sql(self) -> str:
        return (
            f"REFERENCES {_quote_ident(self.table)}({_quote_ident(self.column)}) "
            f"ON DELETE {self.on_delete.value} ON UPDATE {self.on_update.value}"
        )


@dataclass(frozen=True, slots=True)
class Column:
    """A single column declaration.

    Parameters
    ----------
    name:
        Column identifier — never quoted on construction; quoting is applied
        at DDL emission time so callers can use natural Python strings.
    type:
        Python type used to derive SQLite affinity when ``affinity`` is None.
    affinity:
        Optional explicit affinity override (preferred for clarity).
    primary_key:
        True for a single-column PK. Composite PKs are declared at the table
        level via ``primary_key_cols``.
    nullable:
        True if the column may be NULL. Defaults to True; mirrors SQL semantics.
    unique:
        True to enforce a unique constraint on this column.
    default:
        Default value emitted verbatim when non-None. ``allow_optional_default``
        never wraps the value; callers are responsible for quoting raw SQL
        (use :func:`quote_value` for safe literals).
    foreign_key:
        Optional single ``ForeignKey`` targeting a parent column.
    index:
        True to create an implicit index on this column inside the schema.
    """

    name: str
    type: type = str
    affinity: Optional[Affinity] = None
    primary_key: bool = False
    nullable: bool = True
    unique: bool = False
    default: Any = None
    foreign_key: Optional[ForeignKey] = None
    index: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise DatabaseError("Column name must be non-empty")
        if self.primary_key and not self.nullable is False and self.affinity is None:
            # INTEGER PRIMARY KEY columns are rowid aliases in SQLite; pin the
            # affinity explicitly so the DDL is unambiguous.
            object.__setattr__(self, "affinity", Affinity.INTEGER)

    @property
    def effective_affinity(self) -> Affinity:
        return self.affinity or Affinity.from_python(self.type)

    def to_sql(self) -> str:
        parts: List[str] = [_quote_ident(self.name), self.effective_affinity.value]

        if self.primary_key:
            # SQLite single-column INTEGER PRIMARY KEY implies NOT NULL.
            parts.append("PRIMARY KEY")
        else:
            if not self.nullable:
                parts.append("NOT NULL")
            if self.unique:
                parts.append("UNIQUE")

        if self.default is not None:
            parts.append(f"DEFAULT {self._render_default()}")

        if self.foreign_key is not None:
            parts.append(self.foreign_key.to_sql())

        return " ".join(parts)

    def _render_default(self) -> str:
        """Render the default value as a SQL literal fragment."""
        if isinstance(self.default, str):
            # Use the public quoting helper for safety.
            return quote_value(self.default)
        if isinstance(self.default, bool):
            return "1" if self.default else "0"
        if self.default is None:
            return "NULL"
        return str(self.default)


@dataclass(frozen=True, slots=True)
class Index:
    """A standalone CREATE INDEX definition."""

    name: str
    table: str
    columns: Tuple[str, ...]
    unique: bool = False

    def to_sql(self) -> str:
        unique_kw = "UNIQUE " if self.unique else ""
        cols = ", ".join(_quote_ident(c) for c in self.columns)
        return f"CREATE {unique_kw}INDEX IF NOT EXISTS {_quote_ident(self.name)} ON {_quote_ident(self.table)} ({cols})"


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Table:
    """A single declarative table description.

    Honoured by the migration manager when computing schema diffs and by the
    SQLite engine when creating new databases. Foreign-key enforcement is the
    caller's responsibility (PRAGMA ``foreign_keys=ON`` — see ``sqlite/pragmas``).
    """

    name: str
    columns: Tuple[Column, ...]
    primary_key_cols: Tuple[str, ...] = ()
    indexes: Tuple[Index, ...] = ()
    without_rowid: bool = False
    strict: bool = False
    if_not_exists: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise DatabaseError("Table name must be non-empty")
        if not self.columns:
            raise DatabaseError(f"Table '{self.name}' has no columns")
        names = {c.name for c in self.columns}
        for pk in self.primary_key_cols:
            if pk not in names:
                raise DatabaseError(
                    f"Composite PK column '{pk}' not defined on table '{self.name}'"
                )
        for col in self.columns:
            if col.primary_key and self.primary_key_cols:
                # Single-column PRIMARY KEY column conflict with composite PK.
                raise DatabaseError(
                    f"Table '{self.name}' mixes single-column and composite primary keys"
                )

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column:
        for col in self.columns:
            if col.name == name:
                return col
        raise DatabaseError(f"Column '{name}' not defined on table '{self.name}'")

    def to_ddl(self) -> str:
        if_not_exists = "IF NOT EXISTS " if self.if_not_exists else ""

        column_lines: List[str] = [c.to_sql() for c in self.columns]

        if self.primary_key_cols:
            cols = ", ".join(_quote_ident(c) for c in self.primary_key_cols)
            column_lines.append(f"PRIMARY KEY ({cols})")

        modifiers: List[str] = []
        if self.strict:
            modifiers.append("STRICT")
        if self.without_rowid:
            modifiers.append("WITHOUT ROWID")

        body = ",\n  ".join(column_lines)
        modifier_clause = f" {', '.join(modifiers)}" if modifiers else ""
        return (
            f"CREATE TABLE {if_not_exists}{_quote_ident(self.name)} (\n  {body}\n){modifier_clause};"
        )

    def index_ddl(self) -> List[str]:
        return [idx.to_sql() for idx in self.indexes]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Schema:
    """A bundle of tables that belong together (e.g. the metadata schema).

    The migration manager builds one of these per named schema version so it
    can compute diffs and emit ordered DDL, honouring foreign-key ordering
    through :meth:`ordered_tables` (Kahn topological sort over FK refs).
    """

    name: str
    version: int
    tables: Tuple[Table, ...] = ()
    event_source: str = "app.core.database"

    def __post_init__(self) -> None:
        seen: Dict[str, Table] = {}
        for table in self.tables:
            if table.name in seen:
                raise DatabaseError(f"Duplicate table '{table.name}' in schema '{self.name}'")
            seen[table.name] = table

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise DatabaseError(f"Table '{name}' not present in schema '{self.name}'")

    def ordered_tables(self) -> List[Table]:
        """Return tables in dependency order (parents before children)."""
        order: List[Table] = []
        visited: Dict[str, int] = {}
        # unsorted iteration order is deterministic because ``tables`` is a tuple

        def visit(node: Table, path: Tuple[str, ...]) -> None:
            state = visited.get(node.name, 0)
            if state == 2:
                return
            if state == 1:
                cycle = " -> ".join(path + (node.name,))
                raise DatabaseError(f"Foreign-key cycle detected: {cycle}")
            visited[node.name] = 1
            for col in node.columns:
                if col.foreign_key is None:
                    continue
                parent_name = col.foreign_key.table
                if parent_name == node.name:
                    continue  # self-reference — fine
                parent = next(
                    (t for t in self.tables if t.name == parent_name), None
                )
                if parent is None:
                    # External / pre-existing parent — skip ordering.
                    continue
                visit(parent, path + (node.name,))
            visited[node.name] = 2
            order.append(node)

        for table in self.tables:
            visit(table, ())
        return order

    def all_ddl(self) -> List[str]:
        """Return every DDL statement for the schema (tables + indexes)."""
        statements: List[str] = []
        for table in self.ordered_tables():
            statements.append(table.to_ddl())
            statements.extend(table.index_ddl())
        return statements


# ---------------------------------------------------------------------------
# Public quoting helpers
# ---------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    """Quote an identifier safely (double quotes, embedded quotes doubled)."""
    return f"\"{name.replace('\"', '\"\"')}\""


def quote_value(value: Any) -> str:
    """Render a Python value as a safe SQL single-quoted literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return f"'{text.replace(chr(39), chr(39) + chr(39))}'"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ += ["quote_value"]
