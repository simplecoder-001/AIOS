# app/core/database/database_manager.py
"""
Root coordinator for the AIOS database layer.

The :class:`DatabaseManager` is the *only* public surface feature groups import
from ``app.core.database``. Every subordinate manager (connection, session,
transaction, unit-of-work, migration, backup, health) is constructed here,
wired to the EventBus and to the DI container, and tested as a closed set.
Feature groups resolve a repository or a session from the container — they
never reach past this facade to the engine/pool layer.

Responsibilities
----------------
* **Bootstrap ordering** — exactly the order documented in the project SDD:
    1. Construct engines (SQLite metadata + secure, later Qdrant + graph).
    2. Build connection managers per engine.
    3. Build the session manager on top of the metadata pool.
    4. Build the transaction manager + unit-of-work manager.
    5. Build the migration manager, then initialize_fresh() OR apply().
    6. Build the backup manager with flush/reopen hooks wired here.
    7. Build the health manager with the same set of (engine, pool) pairs.
    8. Register every public interface in the :class:`Container`.
    9. Emit the ``database.initialized`` event.
* **Public lifecycle** — :meth:`start` and :meth:`stop` are idempotent and
  thread-safe; the bootstrap only knows about a one-line "build then start".
* **Event bridge** — installs event sinks on the transaction, UoW, and health
  managers that republish internal notifications as AIOS EventBus events
  (``database.transaction.rollback``, ``database.uow.committed``,
  ``database.health.degraded`` etc.). This keeps the EventBus on the
  DatabaseManager's side of the import graph: subordinate managers stay
  import-safe from the bus.
* **State-machine integration** — drives the FG/state machine through the
  ``STARTING → INITIALIZED → SHUTDOWN`` transitions on the
  :class:`StateMachine`, when one is supplied.
* **Optional Qdrant + knowledge-graph subordinates** — built lazily on
  request from the DatabaseManager so the SQLite-core still works in tests
  without qdrant-client or networkx installed.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
app/state → bootstrap → all other database modules → here.

This module is at the *top* of the database import graph and is the only
module in the package that is allowed to import both EventBus/StateMachine
and the entire subordinate manager set.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, Type

from app.core.configs.config_manager import ConfigManager
from app.core.constants.events import (
    EventCategory,
    EventDeliveryMode,
    EventPriority,
    SystemEvent,
)
from app.core.constants.paths import METADATA_DB_FILE, SECURE_DB_FILE, BACKUPS_DIR
from app.core.constants.settings import SECURITY
from app.core.database.backup_manager import (
    BackupManager,
    BackupPolicy,
    verify_integrity,
)
from app.core.database.connection_manager import (
    ConnectionManager,
    PoolConfig,
    default_pool_config,
)
from app.core.database.health_manager import (
    HealthManager,
    HealthSnapshot,
    HealthState,
    HealthThrottle,
    default_throttle,
)
from app.core.database.migration_manager import (
    MigrationManager,
    MigrationOutcome,
    register_builtin,
)
from app.core.database.repository import MetadataRepository, Repository
from app.core.database.session_manager import (
    Propagation,
    Session,
    SessionManager,
)
from app.core.database.sqlite.connection import SQLiteConnection
from app.core.database.sqlite.engine import (
    SQLiteEngine,
    SQLiteEngineConfig,
    build_engine_config,
)
from app.core.database.transaction_manager import (
    IsolationLevel,
    TransactionManager,
    TransactionPolicy,
)
from app.core.database.unit_of_work import (
    UnitManager,
    UnitOfWork,
)
from app.core.event_bus import EventBus
from app.core.exceptions.database import DatabaseError
from app.core.state_manager.state_machine import StateMachine
from app.dependency_injection.container import Container
from app.logging import Logger, LoggerFactory, LogLevel

__all__ = [
    "DatabaseName",
    "DatabaseState",
    "DatabaseStats",
    "DatabaseConfig",
    "DatabaseManager",
    "register_database",
]


# ---------------------------------------------------------------------------
# Enums + records
# ---------------------------------------------------------------------------


class DatabaseName(str, Enum):
    """The well-known databases AIOS constructs at boot."""

    METADATA = "metadata"
    SECURE = "secure"


class DatabaseState(str, Enum):
    """Lifecycle states observed by the DatabaseManager."""

    UNINITIALIZED = "uninitialized"
    STARTING = "starting"
    INITIALIZED = "initialized"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True)
class DatabaseStats:
    """Roll-up of every subordinate manager's counters for observability."""

    polls: int = 0
    backups: int = 0
    restores: int = 0
    migrations_applied: int = 0
    migrations_failed: int = 0

    def as_dict(self) -> dict:
        return {
            "polls": self.polls,
            "backups": self.backups,
            "restores": self.restores,
            "migrations_applied": self.migrations_applied,
            "migrations_failed": self.migrations_failed,
        }


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Top-level knobs consumed during DatabaseManager construction.

    Defaults honour the FG6/FG2 SDDs; the DatabaseManager can override each
    knob from the ConfigManager during :meth:`from_config`.
    """

    metadata_path: Path
    secure_path: Optional[Path]
    backup_dir: Path
    pool_config: PoolConfig
    backup_policy: BackupPolicy
    health_throttle: HealthThrottle
    health_failure_threshold: int
    strict_migrations: bool = True


# ---------------------------------------------------------------------------
# DatabaseManager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """The facade that owns every subordinate database manager.

    The manager is constructed with a :class:`DatabaseConfig` and an optional
    DI :class:`Container`. :meth:`start` performs the full bootstrap
    sequence; :meth:`stop` performs graceful shutdown in reverse order.
    """

    __slots__ = (
        "_config",
        "_logger_factory",
        "_logger",
        "_event_bus",
        "_state_machine",
        "_container",
        "_lock",
        "_state",
        "_stats",
        "_engines",
        "_pools",
        "_session_manager",
        "_transaction_manager",
        "_uow_manager",
        "_migration_manager",
        "_backup_manager",
        "_health_manager",
        "_repositories",
        "_event_unsubscribers",
        "_closed",
        "_started",
    )

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        logger_factory: Optional[LoggerFactory] = None,
        event_bus: Optional[EventBus] = None,
        state_machine: Optional[StateMachine] = None,
        container: Optional[Container] = None,
    ) -> None:
        self._config = config
        self._logger_factory = logger_factory or LoggerFactory()
        self._logger = self._logger_factory.create_rotating_logger(
            name="app.core.database",
            file_path="logs/system/database.log",
            level=LogLevel.INFO,
        )
        self._event_bus = event_bus
        self._state_machine = state_machine
        self._container = container
        self._lock = threading.RLock()
        self._state = DatabaseState.UNINITIALIZED
        self._stats = DatabaseStats()
        self._engines: Dict[str, SQLiteEngine] = {}
        self._pools: Dict[str, ConnectionManager] = {}
        self._session_manager: Optional[SessionManager] = None
        self._transaction_manager: Optional[TransactionManager] = None
        self._uow_manager: Optional[UnitManager] = None
        self._migration_manager: Optional[MigrationManager] = None
        self._backup_manager: Optional[BackupManager] = None
        self._health_manager: Optional[HealthManager] = None
        self._repositories: Dict[Type[Repository], Repository] = {}
        self._event_unsubscribers: List[Callable[[], None]] = []
        self._closed = False
        self._started = False

    # ----------------------------------------------------------- properties
    @property
    def state(self) -> DatabaseState:
        with self._lock:
            return self._state

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._started

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def stats(self) -> DatabaseStats:
        with self._lock:
            return self._stats

    @property
    def config(self) -> DatabaseConfig:
        return self._config

    @property
    def logger(self) -> Logger:
        return self._logger

    @property
    def event_bus(self) -> Optional[EventBus]:
        return self._event_bus

    @property
    def container(self) -> Optional[Container]:
        return self._container

    @property
    def engines(self) -> Mapping[str, SQLiteEngine]:
        return dict(self._engines)

    @property
    def pools(self) -> Mapping[str, ConnectionManager]:
        return dict(self._pools)

    @property
    def session_manager(self) -> SessionManager:
        self._require_started()
        assert self._session_manager is not None
        return self._session_manager

    @property
    def transaction_manager(self) -> TransactionManager:
        self._require_started()
        assert self._transaction_manager is not None
        return self._transaction_manager

    @property
    def uow_manager(self) -> UnitManager:
        self._require_started()
        assert self._uow_manager is not None
        return self._uow_manager

    @property
    def migration_manager(self) -> MigrationManager:
        self._require_started()
        assert self._migration_manager is not None
        return self._migration_manager

    @property
    def backup_manager(self) -> BackupManager:
        self._require_started()
        assert self._backup_manager is not None
        return self._backup_manager

    @property
    def health_manager(self) -> HealthManager:
        self._require_started()
        assert self._health_manager is not None
        return self._health_manager

    # ----------------------------------------------------------- start / stop
    def start(self) -> None:
        """Run the full bootstrap sequence. Idempotent + thread-safe."""
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise DatabaseError("DatabaseManager has been shut down")
            self._state = DatabaseState.STARTING
            if self._state_machine is not None:
                self._state_machine.transition("starting")

        try:
            self._build_engines()
            self._build_pools()
            self._build_session_manager()
            self._build_transaction_managers()
            self._build_migration_manager()
            self._build_backup_manager()
            self._build_health_manager()
            self._wire_event_sinks()
            self._run_migrations()
            self._initial_backup()
            self._register_container()
        except Exception as exc:
            with self._lock:
                self._state = DatabaseState.FAILED
            self._logger.error(
                "Database bootstrap failed",
                extra={"error": str(exc)},
            )
            # Best-effort shutdown so a failed start leaves nothing leaking.
            self._emergency_shutdown()
            raise DatabaseError(
                f"Database bootstrap failed: {exc}",
                cause=exc,
            ) from exc

        with self._lock:
            self._state = DatabaseState.INITIALIZED
            self._started = True

        self._publish_event(
            name=SystemEvent.HEALTH_CHECK.value,
            category=EventCategory.SYSTEM,
            payload={"phase": "database.initialized"},
            priority=EventPriority.NORMAL,
        )
        self._logger.info("DatabaseManager initialized")

    def stop(self) -> None:
        """Graceful shutdown in reverse bootstrap order."""
        with self._lock:
            if self._closed:
                return
            self._state = DatabaseState.STOPPING
            if self._state_machine is not None:
                self._state_machine.transition("stopping")

        self._emergency_shutdown()

        with self._lock:
            self._state = DatabaseState.STOPPED
            self._closed = True
            self._started = False

        self._publish_event(
            name=SystemEvent.HEALTH_CHECK.value,
            category=EventCategory.SYSTEM,
            payload={"phase": "database.stopped"},
            priority=EventPriority.NORMAL,
        )
        self._logger.info("DatabaseManager stopped")

    # ----------------------------------------------------------- emergency shutdown
    def _emergency_shutdown(self) -> None:
        """Tear down every subordinate manager. Safe to call from any state."""
        for unsub in self._event_unsubscribers:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubscribers.clear()

        if self._health_manager is not None:
            try:
                self._health_manager.close()
            except Exception:
                pass
        if self._backup_manager is not None:
            try:
                self._backup_manager.close()
            except Exception:
                pass
        if self._migration_manager is not None:
            try:
                self._migration_manager.close()
            except Exception:
                pass
        if self._uow_manager is not None:
            try:
                self._uow_manager.close()
            except Exception:
                pass
        if self._transaction_manager is not None:
            try:
                self._transaction_manager.close()
            except Exception:
                pass
        if self._session_manager is not None:
            try:
                self._session_manager.close()
            except Exception:
                pass
        for pool in self._pools.values():
            try:
                pool.close()
            except Exception:
                pass
        for engine in self._engines.values():
            try:
                engine.close()
            except Exception:
                pass

    # ----------------------------------------------------------- build helpers
    def _build_engines(self) -> None:
        # Metadata engine (plain SQLite).
        metadata_cfg = build_engine_config(
            database=self._config.metadata_path,
            encrypted=False,
            isolation_level=None,  # transaction manager drives BEGIN/COMMIT
        )
        self._engines[DatabaseName.METADATA.value] = SQLiteEngine(
            metadata_cfg,
            logger=self._logger,
        )
        # Secure engine — represented by a plain SQLiteEngine here.
        # The SQLCipherEncryptedEngine subclass (sqlcipher/encrypted_engine.py)
        # overrides `_open()` to bind the key. Built later by the secure-store
        # bootstrapper; this manager only constructs the metadata engine now.
        if self._config.secure_path is not None:
            secure_cfg = build_engine_config(
                database=self._config.secure_path,
                encrypted=True,
                isolation_level=None,
            )
            secure_engine = SQLiteEngine(
                secure_cfg,
                logger=self._logger,
            )
            self._engines[DatabaseName.SECURE.value] = secure_engine
        self._logger.debug(
            "Engines built",
            extra={"databases": list(self._engines.keys())},
        )

    def _build_pools(self) -> None:
        for name, engine in self._engines.items():
            self._pools[name] = ConnectionManager(
                engine=engine,
                config=self._config.pool_config,
                logger=self._logger,
            )
        self._logger.debug(
            "Pools built",
            extra={"databases": list(self._pools.keys())},
        )

    def _build_session_manager(self) -> None:
        metadata_pool = self._pools[DatabaseName.METADATA.value]
        self._session_manager = SessionManager(
            pool=metadata_pool,
            logger=self._logger,
        )

    def _build_transaction_managers(self) -> None:
        assert self._session_manager is not None
        self._transaction_manager = TransactionManager(
            session_manager=self._session_manager,
            logger=self._logger,
        )
        self._uow_manager = UnitManager(
            session_manager=self._session_manager,
            transaction_manager=self._transaction_manager,
            logger=self._logger,
        )

    def _build_migration_manager(self) -> None:
        assert self._session_manager is not None
        self._migration_manager = MigrationManager(
            session_manager=self._session_manager,
            logger=self._logger,
        )
        register_builtin(self._migration_manager)

    def _build_backup_manager(self) -> None:
        self._backup_manager = BackupManager(
            engines=self._engines,
            backup_dir=self._config.backup_dir,
            policy=self._config.backup_policy,
            logger=self._logger,
            flush_hook=self._flush_pool_for_restore,
            reopen_hook=self._reopen_pool_after_restore,
        )

    def _build_health_manager(self) -> None:
        assert self._migration_manager is not None  # for on_recovery wiring
        # Build (engine, pool) tuples in the same registration order as
        # ``self._engines`` so the snapshots list iterates predictably.
        pairs: Dict[str, Tuple[SQLiteEngine, ConnectionManager]] = {
            name: (self._engines[name], self._pools[name])
            for name in self._engines
        }
        self._health_manager = HealthManager(
            databases=pairs,
            throttle=self._config.health_throttle,
            logger=self._logger,
            failure_threshold=self._config.health_failure_threshold,
            on_recovery=self._on_database_recovery,
        )

    # ----------------------------------------------------------- event sink wiring
    def _wire_event_sinks(self) -> None:
        """Bridge subordinate-manager events to the EventBus.

        Sinks are callables taking a dict; the DatabaseManager rewrites them
        into EventBus ``emit`` calls. All sinks share the same source label
        so consumers see ``source="app.core.database"`` uniformly.
        """
        if self._event_bus is None:
            return
        scope = self._event_bus.publisher("app.core.database")
        # Transaction manager rollbacks.
        assert self._transaction_manager is not None
        self._event_unsubscribers.append(
            self._transaction_manager.install_rollback_sink(
                self._make_sink("database.transaction.rollback", EventCategory.SYSTEM),
            )
        )
        # Unit-of-work committed / rolled back.
        assert self._uow_manager is not None
        self._event_unsubscribers.append(
            self._uow_manager.install_event_sink(
                self._make_uow_sink(scope),
            )
        )
        # Health transitions.
        assert self._health_manager is not None
        self._event_unsubscribers.append(
            self._health_manager.install_event_sink(
                self._make_health_sink(scope),
            )
        )

    def _make_sink(self, event_name: str, category: EventCategory) -> Callable[[dict], None]:
        bus = self._event_bus
        source = bus.publisher("app.core.database") if bus is not None else None

        def _sink(payload: dict) -> None:
            if source is None:
                return
            try:
                source.emit(
                    event_name,
                    payload={k: v for k, v in payload.items() if k != "event"},
                    category=category,
                    priority=EventPriority.NORMAL,
                    delivery_mode=EventDeliveryMode.ASYNC,
                )
            except Exception:
                # A bridge failure must never break the commit / rollback
                # path; subordinate managers swallow these.
                pass

        return _sink

    def _make_uow_sink(self, scope) -> Callable[[dict], None]:
        """UoW event payloads already carry the event name; route by it."""

        def _sink(payload: dict) -> None:
            event_name = payload.get("event")
            if event_name is None:
                return
            try:
                scope.emit(
                    event_name,
                    payload={k: v for k, v in payload.items() if k != "event"},
                    category=EventCategory.SYSTEM,
                    priority=EventPriority.NORMAL,
                    delivery_mode=EventDeliveryMode.ASYNC,
                )
            except Exception:
                pass

        return _sink

    def _make_health_sink(self, scope) -> Callable[[dict], None]:
        """Health events get HIGH priority when degraded, CRITICAL when failed."""

        def _sink(payload: dict) -> None:
            event_name = payload.get("event")
            if event_name is None:
                return
            prio = (
                EventPriority.CRITICAL
                if "failed" in event_name
                else EventPriority.HIGH
            )
            try:
                scope.emit(
                    event_name,
                    payload={k: v for k, v in payload.items() if k != "event"},
                    category=EventCategory.SYSTEM,
                    priority=prio,
                    delivery_mode=EventDeliveryMode.ASYNC,
                )
            except Exception:
                pass

        return _sink

    # ----------------------------------------------------------- migration execution
    def _run_migrations(self) -> None:
        assert self._migration_manager is not None
        results = self._migration_manager.apply()
        for schema, version, outcome in results:
            if outcome is MigrationOutcome.FAILED:
                with self._lock:
                    self._stats.migrations_failed += 1
                if self._config.strict_migrations:
                    raise DatabaseError(
                        f"Migration {schema}@v{version} failed",
                    ).with_context(schema=schema, version=version, outcome=outcome.value)
            elif outcome is MigrationOutcome.APPLIED:
                with self._lock:
                    self._stats.migrations_applied += 1

    # ----------------------------------------------------------- initial backup
    def _initial_backup(self) -> None:
        assert self._backup_manager is not None
        try:
            entries = self._backup_manager.backup_all(description="post_bootstrap")
            if entries:
                with self._lock:
                    self._stats.backups += len(entries)
        except Exception as exc:
            # A failed initial backup is not fatal — the system still boots.
            # The health manager will surface persistent backup failures on
            # the next poll cycle.
            self._logger.warning(
                "Initial post-bootstrap backup failed; continuing",
                extra={"error": str(exc)},
            )

    # ----------------------------------------------------------- restore hooks
    def _flush_pool_for_restore(self, name: str) -> None:
        """Drain a pool before restoring its database file."""
        pool = self._pools.get(name)
        if pool is None:
            return
        pool.flush()
        self._logger.info("Pool flushed for restore", extra={"database": name})

    def _reopen_pool_after_restore(self, name: str) -> None:
        """Re-arm a pool after the database file has been restored."""
        pool = self._pools.get(name)
        if pool is None:
            return
        # The legacy pool was closed by ``flush``; rebuild a fresh one
        # against the same engine so the restored file is opened cleanly.
        engine = self._engines[name]
        new_pool = ConnectionManager(
            engine=engine,
            config=self._config.pool_config,
            logger=self._logger,
        )
        self._pools[name] = new_pool
        if name == DatabaseName.METADATA.value:
            assert self._session_manager is not None
            # The session manager holds a reference to the old pool; swap it.
            self._session_manager._pool = new_pool
        self._logger.info("Pool reopened after restore", extra={"database": name})

    # ----------------------------------------------------------- auto-recovery hook
    def _on_database_recovery(self, name: str) -> None:
        """Invoked by the health manager when a database stays unhealthy.

        Asks the backup manager for an automatic restore. Most failure modes
        that reach this point are corruption from a migration gone wrong or
        a power-loss mid-write; the recovery manager (separate module) takes
        over deeper investigation after this callback returns.
        """
        assert self._backup_manager is not None
        latest = self._backup_manager.latest_backup(name)
        if latest is None:
            self._logger.error(
                "Automatic recovery requested but no backup exists",
                extra={"database": name},
            )
            return
        try:
            self._backup_manager.restore(name, entry=latest)
            with self._lock:
                self._stats.restores += 1
            assert self._health_manager is not None
            self._health_manager.reset_failure_streak(name)
        except Exception as exc:
            self._logger.error(
                "Automatic restore failed; recovery manager must take over",
                extra={"database": name, "error": str(exc)},
            )

    # ----------------------------------------------------------- repository registration
    def register_repository(self, repo_cls: Type[Repository], *args: Any, **kwargs: Any) -> Repository:
        """Construct and register a :class:`Repository` subclass.

        The repository is stored by class so feature groups can resolve it
        from the DI container by type-positional notation. Repositories are
        constructed with a :class:`SessionManager` argument when the
        subclass ``__init__`` declares one.
        """
        self._require_started()
        repo = repo_cls(*args, logger=self._logger, **kwargs)
        self._repositories[repo_cls] = repo
        if self._container is not None:
            self._container.register_instance(repo_cls, repo)
        return repo

    def repository(self, repo_cls: Type[Repository]) -> Repository:
        self._require_started()
        repo = self._repositories.get(repo_cls)
        if repo is None:
            raise DatabaseError(f"Repository not registered: {repo_cls.__name__}")
        return repo

    # ----------------------------------------------------------- event publishing
    def _publish_event(
        self,
        *,
        name: str,
        category: EventCategory,
        payload: Mapping[str, Any],
        priority: EventPriority = EventPriority.NORMAL,
    ) -> None:
        if self._event_bus is None:
            return
        try:
            scope = self._event_bus.publisher("app.core.database")
            scope.emit(
                name,
                payload=dict(payload),
                category=category,
                priority=priority,
                delivery_mode=EventDeliveryMode.ASYNC,
            )
        except Exception as exc:
            self._logger.debug(
                "Event publish failed; continuing",
                extra={"event": name, "error": str(exc)},
            )

    # ----------------------------------------------------------- introspection
    def health_snapshot(self) -> List[HealthSnapshot]:
        """Return the last-known health per database."""
        if self._health_manager is None:
            return []
        return [self._health_manager.snapshot(n) for n in self._health_manager.databases]

    def describe(self) -> dict:
        """Snapshot for the FG5 dashboard."""
        with self._lock:
            return {
                "state": self._state.value,
                "started": self._started,
                "engines": list(self._engines.keys()),
                "pools": {
                    name: pool.stats.as_dict() for name, pool in self._pools.items()
                },
                "migrations": (
                    self._migration_manager.describe()
                    if self._migration_manager is not None
                    else None
                ),
                "health": (
                    self._health_manager.stats.as_dict()
                    if self._health_manager is not None
                    else None
                ),
                "backups": (
                    {
                        name: len(self._backup_manager.list_backups(name))
                        for name in self._engines
                    }
                    if self._backup_manager is not None
                    else {}
                ),
                "stats": self._stats.as_dict(),
            }

    # ----------------------------------------------------------- guards
    def _require_started(self) -> None:
        with self._lock:
            if not self._started:
                raise DatabaseError(
                    "DatabaseManager is not started; call start() first",
                )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<DatabaseManager state={self._state.value} "
            f"engines={len(self._engines)} "
            f"started={self._started}>"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def default_database_config(
    *,
    metadata_path: Optional[Path] = None,
    secure_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> DatabaseConfig:
    """Return a :class:`DatabaseConfig` built from the project path constants."""
    return DatabaseConfig(
        metadata_path=metadata_path or METADATA_DB_FILE,
        secure_path=secure_path or SECURE_DB_FILE,
        backup_dir=backup_dir or BACKUPS_DIR / "database",
        pool_config=default_pool_config(),
        backup_policy=BackupPolicy(),
        health_throttle=default_throttle(),
        health_failure_threshold=3,
        strict_migrations=True,
    )


def from_config(config: ConfigManager) -> DatabaseConfig:
    """Build a :class:`DatabaseConfig` from a bootstrapped ConfigManager."""
    pool_size_min = config.get_int("core.database.pool_min_size", 1) or 1
    pool_size_max = config.get_int("core.database.pool_max_size", 16) or 16
    acquire_timeout = config.get_int("core.database.acquire_timeout_ms", 5000) or 5000
    policy_retention_count = config.get_int("core.database.backup_retention_count", 10) or 10
    policy_retention_days = config.get_int("core.database.backup_retention_days", 30) or 30
    return DatabaseConfig(
        metadata_path=METADATA_DB_FILE,
        secure_path=SECURE_DB_FILE,
        backup_dir=BACKUPS_DIR / "database",
        pool_config=PoolConfig(
            min_size=pool_size_min,
            max_size=pool_size_max,
            acquire_timeout_ms=acquire_timeout,
        ),
        backup_policy=BackupPolicy(
            retention_count=policy_retention_count,
            retention_age_days=policy_retention_days,
        ),
        health_throttle=HealthThrottle(
            integrity_min_interval_seconds=config.get_int(
                "core.database.health_integrity_interval_seconds", 300
            ) or 300,
            pool_min_interval_seconds=config.get_int(
                "core.database.health_pool_interval_seconds", 10
            ) or 10,
        ),
        health_failure_threshold=config.get_int(
            "core.database.health_failure_threshold", 3
        ) or 3,
        strict_migrations=config.get_bool("core.database.strict_migrations", True) or True,
    )


# ---------------------------------------------------------------------------
# DI registration
# ---------------------------------------------------------------------------


def register_database(
    container: Container,
    *,
    manager: Optional[DatabaseManager] = None,
    config: Optional[DatabaseConfig] = None,
    logger_factory: Optional[LoggerFactory] = None,
    event_bus: Optional[EventBus] = None,
    state_machine: Optional[StateMachine] = None,
) -> DatabaseManager:
    """Build (or register a pre-built) :class:`DatabaseManager` into ``container``.

    Used by the bootstrap on startup; the same call works for tests — pass
    a fresh container and the manager will register itself as a singleton.
    """
    actual_config = config or default_database_config()
    actual_manager = manager or DatabaseManager(
        actual_config,
        logger_factory=logger_factory,
        event_bus=event_bus,
        state_machine=state_machine,
        container=container,
    )
    container.register_instance(DatabaseManager, actual_manager)
    return actual_manager


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += [
    "default_database_config",
    "from_config",
    "register_database",
    "DatabaseState",
    "DatabaseName",
    "DatabaseConfig",
]
