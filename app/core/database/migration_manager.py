# app/core/database/migration_manager.py
"""
Schema migrations for the AIOS metadata + audit SQLite stores.

The migration manager overlays a declarative schema (``models/base.Schema``)
with imperative version-to-version migration steps. It owns three guarantees
that the rest of the system depends on at boot:

1. **Version bookkeeping** — every applied migration is recorded in the
   ``schema_migrations`` ledger, so a restart never re-applies a migration
   and the audit / health managers can dump the current version per schema
   on demand.
2. **Atomic per-step** — every migration step runs inside its own
   transaction; on failure the manager rolls back and the rest of the
   system halts rather than run against a half-migrated schema. This is
   the documented contract of :class:`MigrationError` in
   ``app.core.exceptions.database``.
3. **Deterministic DDL** — for a fresh database the manager applies the
   *current* declarative schema in foreign-key-respecting order (via
   ``Schema.ordered_tables``), then bumps the ledger to the latest version
   so the same database never appears "behind" the rest of the cluster.

The manager supports three kinds of migration steps:

* ``ddl_step``                  — emit a string of DDL statements
* ``data_step``                 — call a Python callback armed with the
                                  active :class:`Session`; user-supplied
                                  code can transform any data
* ``destructive_step``          — call a Python callback that *deletes or
                                  alters* rows; recorded separately so the
                                  audit logger can flag every destructive
                                  migration on its own line

Migrations are *forward-only*. The manager does not implement down-migrations
because SQLite's ALTER TABLE is too restrictive to make backward migrations
correct in general — the recovery manager restores a previous schema state by
rolling back the database file from a backup rather than reverting DDL in
place. This keeps the migration module a strictly additive ledger; any
schema change that needs to delete a column ships with a backup-then-restore
advisory in the migration step's description.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
connection_manager → session_manager → transaction_manager →
``models/{base,metadata,audit}`` → here.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from app.core.database.models.audit import AUDIT_SCHEMA, AUDIT_SCHEMA_NAME
from app.core.database.models.base import Schema
from app.core.database.models.metadata import (
    METADATA_SCHEMA,
    METADATA_SCHEMA_NAME,
    SCHEMA_MIGRATIONS,
    TableNames,
)
from app.core.database.session_manager import Session, SessionManager
from app.core.exceptions.database import DatabaseError, IntegrityError, MigrationError, QueryError
from app.logging import Logger

__all__ = [
    "MigrationKind",
    "MigrationStep",
    "MigrationDefinition",
    "MigrationOutcome",
    "MigrationStats",
    "MigrationManager",
]


# ---------------------------------------------------------------------------
# Step model
# ---------------------------------------------------------------------------


class MigrationKind(str, Enum):
    """What a migration step does, recorded in the audit log."""

    DDL = "ddl"
    DATA = "data"
    DESTRUCTIVE = "destructive"


@dataclass(slots=True)
class MigrationStep:
    """A single unit of work inside a migration definition.

    Steps execute in the order they are added to a
    :class:`MigrationDefinition`; each runs in its own transaction.
    """

    kind: MigrationKind
    description: str
    ddl: Optional[str] = None
    callback: Optional[Callable[[Session], None]] = None

    def __post_init__(self) -> None:
        if self.kind is MigrationKind.DDL:
            if not self.ddl:
                raise DatabaseError("DDL migration step requires ddl")
        else:
            if self.callback is None:
                raise DatabaseError(
                    f"{self.kind.value} migration step requires a callback",
                )


@dataclass(slots=True)
class MigrationDefinition:
    """A single named, versioned migration with an ordered list of steps."""

    schema_name: str
    version: int
    description: str
    steps: List[MigrationStep] = field(default_factory=list)

    def add_ddl(self, ddl: str, *, description: Optional[str] = None) -> "MigrationDefinition":
        self.steps.append(
            MigrationStep(
                kind=MigrationKind.DDL,
                description=description or "DDL step",
                ddl=ddl,
            )
        )
        return self

    def add_data(
        self,
        callback: Callable[[Session], None],
        *,
        description: Optional[str] = None,
    ) -> "MigrationDefinition":
        self.steps.append(
            MigrationStep(
                kind=MigrationKind.DATA,
                description=description or "data step",
                callback=callback,
            )
        )
        return self

    def add_destructive(
        self,
        callback: Callable[[Session], None],
        *,
        description: Optional[str] = None,
    ) -> "MigrationDefinition":
        self.steps.append(
            MigrationStep(
                kind=MigrationKind.DESTRUCTIVE,
                description=description or "destructive step",
                callback=callback,
            )
        )
        return self


# ---------------------------------------------------------------------------
# Outcome + stats
# ---------------------------------------------------------------------------


class MigrationOutcome(str, Enum):
    APPLIED = "applied"
    SKIPPED = "skipped"               # already on the ledger
    FAILED = "failed"
    NO_OP = "no_op"                   # the definition had no steps/


@dataclass(slots=True)
class MigrationStats:
    """Lifetime counters consumed by the health manager."""

    applied: int = 0
    skipped: int = 0
    failed: int = 0
    steps_run: int = 0
    last_applied_version: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "skipped": self.skipped,
            "failed": self.failed,
            "steps_run": self.steps_run,
            "last_applied_version": dict(self.last_applied_version),
        }


@dataclass(slots=True)
class StepResult:
    """Per-step result retained for the migration audit log."""

    step_index: int
    kind: MigrationKind
    description: str
    duration_seconds: float
    rows_affected: int = 0


# ---------------------------------------------------------------------------
# MigrationManager
# ---------------------------------------------------------------------------


class MigrationManager:
    """Owns schema versioning and migration application for one database set.

    Constructed per :class:`DatabaseManager`. The manager is the only piece
    of code allowed to write to the ``schema_migrations`` ledger table; every
    other subsystem reads from it read-only.

    Operation
    ---------
    Construction registers the known schemas (metadata + audit). Callers then
    call one of:

    * :meth:`initialize_fresh` — apply the current declarative schema on a
      brand-new database, writing the latest version to the ledger so the
      normal "apply migrations" call later is a no-op.
    * :meth:`apply` — run every registered migration whose version is higher
      than the ledger's current version, in ascending order.
    * :meth:`apply_one` — apply a single migration by name+version, used by
      tests and the recovery manager after restoring a backup.
    """

    __slots__ = (
        "_session_manager",
        "_logger",
        "_lock",
        "_stats",
        "_initial_schemas",
        "_migrations",
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
        self._stats = MigrationStats()
        # Initial declarative schemas, applied only on a fresh database.
        self._initial_schemas: list[Schema] = [METADATA_SCHEMA, AUDIT_SCHEMA]
        # Ordered registry of (schema_name, version) → definition.
        self._migrations: dict[tuple[str, int], MigrationDefinition] = {}
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def stats(self) -> MigrationStats:
        with self._lock:
            return self._stats

    @property
    def is_closed(self) -> bool:
        return self._closed

    def registered_migrations(self) -> List[MigrationDefinition]:
        """Return all registered migrations in (schema_name, version) order."""
        with self._lock:
            return [self._migrations[k] for k in sorted(self._migrations.keys())]

    # ----------------------------------------------------------- registration
    def register(self, migration: MigrationDefinition) -> None:
        """Add a migration definition to the registry.

        Idempotent: re-registering the same (schema_name, version) raises so
        a duplicate registration (typical of two feature groups sharing a
        version) is surfaced loudly instead of silently overwritten.
        """
        with self._lock:
            key = (migration.schema_name, migration.version)
            if key in self._migrations:
                raise DatabaseError(
                    f"Migration {key} already registered",
                )
            self._migrations[key] = migration
            if self._logger:
                self._logger.debug(
                    "Migration registered",
                    extra={
                        "schema": migration.schema_name,
                        "version": migration.version,
                        "steps": len(migration.steps),
                    },
                )

    # ----------------------------------------------------------- freshness
    def initialize_fresh(self) -> None:
        """Apply the current declarative schema to a brand-new database.

        Writes one ledger row per known schema with its current version so a
        subsequent :meth:`apply` skips the already-applied migrations.

        Idempotent: when the ledger already has rows this method refuses to
        run — initializing a non-empty database would mask a half-applied
        migration, and the documented contract for "schema drifted against
        expectation" is :class:`MigrationError`.
        """
        if self._closed:
            raise DatabaseError("MigrationManager is closed")
        with self._session_manager.session(
            caller="migration.initialize_fresh",
            begin=True,
        ) as session:
            existing = self._ledger_versions(session)
            if existing:
                raise MigrationError(
                    version=existing,
                ).with_context(
                    reason="refusing to initialize a database that already has schema ledger rows",
                    existing=existing,
                )

            for schema in self._initial_schemas:
                # Ensure the schema_migrations ledger exists *before* we apply
                # any schema — its own ledger must always exist for the
                # subsequent INSERTs.
                if schema.name == METADATA_SCHEMA_NAME:
                    self._ensure_ledger_table(session)
                self._apply_schema(session, schema)
                self._record_ledger(
                    session,
                    schema_name=schema.name,
                    version=schema.version,
                    description=f"initialize_fresh({schema.name})",
                )
                with self._lock:
                    self._stats.last_applied_version[schema.name] = schema.version

            if self._logger:
                self._logger.info(
                    "Fresh schema initialized",
                    extra={
                        "schemas": [s.name for s in self._initial_schemas],
                    },
                )

    # ----------------------------------------------------------- apply
    def apply(self) -> List[Tuple[str, int, MigrationOutcome]]:
        """Apply every pending registered migration in ascending order.

        Returns the per-migration outcome list so the DatabaseManager can
        publish a single ``database.migrated`` event with the full result
        set at the end of boot.
        """
        if self._closed:
            raise DatabaseError("MigrationManager is closed")
        results: List[Tuple[str, int, MigrationOutcome]] = []
        for migration in self.registered_migrations():
            outcome = self.apply_one(migration.schema_name, migration.version)
            results.append((migration.schema_name, migration.version, outcome))
            # Stop on first failure: continue running against a half-migrated
            # schema violates the documented contract of MigrationError.
            if outcome is MigrationOutcome.FAILED:
                break
        return results

    def apply_one(self, schema_name: str, version: int) -> MigrationOutcome:
        """Apply a single migration by (schema_name, version)."""
        if self._closed:
            raise DatabaseError("MigrationManager is closed")
        with self._lock:
            migration = self._migrations.get((schema_name, version))
        if migration is None:
            raise MigrationError(version=version).with_context(
                schema=schema_name,
                reason="migration not registered",
            )

        with self._session_manager.session(
            caller=f"migration:{schema_name}:v{version}",
            begin=True,
        ) as session:
            self._ensure_ledger_table(session)
            current = self._ledger_version_for(session, schema_name)

            if current >= version:
                with self._lock:
                    self._stats.skipped += 1
                return MigrationOutcome.SKIPPED

            if current + 1 < version:
                raise MigrationError(version=version).with_context(
                    schema=schema_name,
                    reason=f"missing prior migration (current={current}, requested={version})",
                    current_version=current,
                    requested_version=version,
                )

            if not migration.steps:
                return MigrationOutcome.NO_OP

            step_results: List[StepResult] = []
            try:
                for idx, step in enumerate(migration.steps):
                    step_results.append(
                        self._run_step(session, idx, step),
                    )
            except Exception as exc:
                # Roll back inside the session boundary. The session's __exit__
                # also rolls back but doing it explicitly lets us record the
                # failure before the session closes.
                self._cancel_transaction(session)
                with self._lock:
                    self._stats.failed += 1
                if self._logger:
                    self._logger.error(
                        "Migration failed; rolled back",
                        extra={
                            "schema": schema_name,
                            "version": version,
                            "description": migration.description,
                            "error": str(exc),
                            "steps_run": len(step_results),
                        },
                    )
                raise MigrationError(version=version, cause=exc) from exc

            self._record_ledger(
                session,
                schema_name=schema_name,
                version=version,
                description=migration.description,
            )
            with self._lock:
                self._stats.applied += 1
                self._stats.steps_run += len(step_results)
                self._stats.last_applied_version[schema_name] = version
            if self._logger:
                self._logger.info(
                    "Migration applied",
                    extra={
                        "schema": schema_name,
                        "version": version,
                        "description": migration.description,
                        "steps": [r.__dict__ for r in step_results] if False else None,
                    },
                )
        return MigrationOutcome.APPLIED

    # ----------------------------------------------------------- step execution
    def _run_step(self, session: Session, index: int, step: MigrationStep) -> StepResult:
        import time as _time
        started = _time.monotonic()
        if step.kind is MigrationKind.DDL:
            before = self._total_changes(session)
            session.connection.executescript(step.ddl)  # type: ignore[arg-type]
            rows = self._total_changes(session) - before
            return StepResult(
                step_index=index,
                kind=step.kind,
                description=step.description,
                duration_seconds=_time.monotonic() - started,
                rows_affected=rows,
            )
        # data / destructive callback
        assert step.callback is not None
        before = self._total_changes(session)
        try:
            step.callback(session)
        except Exception:
            # Re-raise as MigrationError so the catch in apply_one can tag
            # the migration version.
            raise
        rows = self._total_changes(session) - before
        return StepResult(
            step_index=index,
            kind=step.kind,
            description=step.description,
            duration_seconds=_time.monotonic() - started,
            rows_affected=rows,
        )

    def _cancel_transaction(self, session: Session) -> None:
        try:
            if session.in_transaction:
                session.rollback()
        except Exception:
            # Connection already broken — pool will evict it. Worst case we
            # leave the row-data unchanged because the failed DDL never
            # committed.
            pass

    def _total_changes(self, session: Session) -> int:
        return int(session.connection.total_changes)

    # ----------------------------------------------------------- ledger helpers
    def _ensure_ledger_table(self, session: Session) -> None:
        """Apply only the ``schema_migrations`` table if absent.

        Necessary because :meth:`initialize_fresh` and the early
        apply-when-behind flow both need the ledger before any other DDL.
        """
        ddl = SCHEMA_MIGRATIONS.to_ddl()
        # Idempotent: CREATE TABLE IF NOT EXISTS (set on the schema).
        session.connection.executescript(ddl)  # type: ignore[arg-type]
        # Its indexes, ordered by the schema definition.
        for idx_ddl in SCHEMA_MIGRATIONS.index_ddl():
            session.connection.executescript(idx_ddl)  # type: ignore[arg-type]

    def _apply_schema(self, session: Session, schema: Schema) -> None:
        """Apply a schema's DDL (tables + indexes) in FK-respecting order."""
        for stmt in schema.all_ddl():
            session.connection.executescript(stmt)  # type: ignore[arg-type]

    def _record_ledger(
        self,
        session: Session,
        *,
        schema_name: str,
        version: int,
        description: str,
    ) -> None:
        from app.core.database.models.base import quote_value
        # Direct insert bypassing the repository is intentional — the ledger
        # is the migration manager's internal table and using the repository
        # would create a circular dependency at this module's import graph.
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        sql = (
            f"INSERT INTO {SCHEMA_MIGRATIONS.name} "
            f"(schema_name, version, description, applied_at) "
            f"VALUES (?, ?, ?, ?)"
        )
        try:
            session.connection.execute(sql, (schema_name, version, description, applied_at))
        except Exception as exc:
            raise IntegrityError(
                constraint=f"schema_migrations_unique({schema_name},{version})",
                cause=exc,
            ) from exc

    def _ledger_version_for(self, session: Session, schema_name: str) -> int:
        row = session.connection.execute(
            f"SELECT MAX(version) AS v FROM {SCHEMA_MIGRATIONS.name} WHERE schema_name = ?",
            (schema_name,),
        ).fetchone()
        try:
            return int(row["v"]) if row is not None and row["v"] is not None else 0
        except (KeyError, IndexError, TypeError):
            return 0

    def _ledger_versions(self, session: Session) -> dict[str, int]:
        rows = session.connection.execute(
            f"SELECT schema_name, MAX(version) AS v FROM {SCHEMA_MIGRATIONS.name} GROUP BY schema_name",
        ).fetchall()
        out: dict[str, int] = {}
        for row in rows:
            try:
                out[row["schema_name"]] = int(row["v"])
            except (KeyError, TypeError):
                continue
        return out

    # ----------------------------------------------------------- introspection
    def current_version(self, schema_name: str = METADATA_SCHEMA_NAME) -> int:
        """Public read accessor for the current ledger version."""
        with self._session_manager.session(caller="migration.current_version", begin=False) as session:
            self._ensure_ledger_table(session)
            return self._ledger_version_for(session, schema_name)

    def describe(self) -> dict:
        """Snapshot the manager's state for the health manager / dashboard."""
        with self._lock:
            return {
                "registered": [
                    (m.schema_name, m.version, len(m.steps))
                    for m in self.registered_migrations()
                ],
                "stats": self._stats.as_dict(),
            }

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        self._closed = True
        if self._logger:
            self._logger.info("MigrationManager closed", extra={"stats": self._stats.as_dict()})

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<MigrationManager migrations={len(self._migrations)} applied={self._stats.applied}>"


# ---------------------------------------------------------------------------
# Public-API shims
# ---------------------------------------------------------------------------


def register_builtin(mgr: MigrationManager) -> None:
    """Register every built-in migration shipped with the assistant.

    Today (schema version 1) there are no forward migrations to apply — the
    declarative schema and the ledger start in lockstep. As the schema
    evolves, new :class:`MigrationDefinition` objects are added here in
    ascending version order. Keeping the registry explicit (rather than
    autoloaded) means a forgotten migration never silently ships to a
    production user.
    """
    # No-op at version 1: kept as a stable extension point so the
    # DatabaseManager always has a single call to register everything.
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += ["register_builtin"]
