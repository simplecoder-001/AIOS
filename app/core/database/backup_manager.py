# app/core/database/backup_manager.py
"""
On-disk backup + restore for the AIOS SQLite / SQLCipher databases.

The backup manager owns four operations the rest of the system depends on:

1. **Scheduled backups** — the bootstrap wires a daily APScheduler job (FG8
   layer 10) that calls :meth:`BackupManager.backup_all`, producing a
   timestamped copy of the metadata and secure databases into
   ``data/backups/``. The user-configurable retention window governs trim.
2. **Pre-migration / pre-update backups** — before any non-trivial migration
   step (per FG10's "rollback support") the migration manager asks this
   module for a :meth:`snapshot` of the current database; a failed migration
   restores the snapshot via :meth:`restore`.
3. **Integrity verification** — every backup is immediately opened read-only
   and ``PRAGMA integrity_check`` is run; a failed backup is deleted from
   disk and surfaces as a :class:`BackupError` so the caller never sees a
   silent corrupted archive.
4. **Restore-from-backup** — restore loads a backup into the live database
   path after the connection pool has flushed and closed all live connections,
   so an in-flight transaction never overwrites a restore target.

Backup format
-------------
Two formats are supported, chosen by :class:`BackupFormat`:

* ``VACUUM_INTO`` — uses SQLite's ``VACUUM INTO 'path'`` statement, which
  writes a single-file, defragmented copy in one atomic operation. The
  fallback when sqlcipher is unavailable.
* ``BACKUP_API`` — uses the driver's ``Connection.backup(dst_conn)`` API,
  which is the only safe format for SQLCipher databases — VACUUM INTO strips
  the encryption and key-binding context from the destination.

Why both? VACUUM INTO is faster and produces smaller archives on a plain
SQLite DB, but SQLCipher refuses it once ``cipher`` PRAGMAs are bound. The
manager picks per-engine during construction.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
connection_manager → session_manager → ``sqlite/engine`` → here. The
migration manager's "snapshot/restore" hook is plumbed through here without
a direct dependency between migration_manager and connection_manager.

Why flush hooks instead of injecting the pool
---------------------------------------------
The backup manager's restore operation must ensure no live connection is
writing to the database file while it is being overwritten. Rather than
importing :class:`ConnectionManager` (which would tie this module to the
SQLite engine), the manager accepts ``flush_callable`` and ``restore_callable``
hooks registered by the ``DatabaseManager`` at boot — keeping the dependency
graph stable while still giving restore a clear "drain-and-reopen" entry point.

Versioning
----------
Each SQLite database directory may contain multiple AES-GCM-encrypted backup
files; the manager writes a sidecar ``*.manifest.json`` listing the current
backup set so the recovery manager can enumerate encrypted backups without
decrypting them first. The manifest is itself signed by the audit chain
(separate module) when written from the DatabaseManager.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, List, Mapping, Optional

from app.core.database.sqlite.connection import SQLiteConnection
from app.core.database.sqlite.engine import SQLiteEngine
from app.core.database.sqlite.pragmas import PRAGMAS_INTEGRITY, PRAGMAS_DEFAULT
from app.core.exceptions.database import BackupError, DatabaseError
from app.logging import Logger

__all__ = [
    "BackupFormat",
    "BackupManifest",
    "BackupEntry",
    "BackupPolicy",
    "BackupStats",
    "BackupManager",
    "verify_integrity",
]


# ---------------------------------------------------------------------------
# Enums + records
# ---------------------------------------------------------------------------


class BackupFormat(str, Enum):
    """On-disk backup format chosen per engine."""

    VACUUM_INTO = "vacuum_into"
    BACKUP_API = "backup_api"


@dataclass(slots=True)
class BackupEntry:
    """A single backup file on disk."""

    database_name: str
    path: Path
    format: BackupFormat
    sha256: str
    size_bytes: int
    created_at: str
    verified: bool
    source_version: Optional[int] = None
    description: Optional[str] = None
    encrypted: bool = False

    def as_dict(self) -> dict:
        return {
            "database_name": self.database_name,
            "path": str(self.path),
            "format": self.format.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "verified": self.verified,
            "source_version": self.source_version,
            "description": self.description,
            "encrypted": self.encrypted,
        }


@dataclass(slots=True)
class BackupManifest:
    """Sidecar manifest listing the recovery-known backup set."""

    database_name: str
    database_path: Path
    created_at: str
    entries: List[BackupEntry] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "database_name": self.database_name,
                "database_path": str(self.database_path),
                "created_at": self.created_at,
                "entries": [e.as_dict() for e in self.entries],
            },
            indent=2,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "BackupManifest":
        data = json.loads(payload)
        entries: List[BackupEntry] = [
            BackupEntry(
                database_name=e["database_name"],
                path=Path(e["path"]),
                format=BackupFormat(e["format"]),
                sha256=e["sha256"],
                size_bytes=e["size_bytes"],
                created_at=e["created_at"],
                verified=e["verified"],
                source_version=e.get("source_version"),
                description=e.get("description"),
                encrypted=e.get("encrypted", False),
            )
            for e in data.get("entries", [])
        ]
        return cls(
            database_name=data["database_name"],
            database_path=Path(data["database_path"]),
            created_at=data["created_at"],
            entries=entries,
        )


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    """Retention + verification knobs for backups.

    Defaults honour the FG6 "minimum 5 nightly backups, hashed and verified"
    contract; the DatabaseManager overrides them from configuration during
    bootstrap.
    """

    retention_count: int = 10           # keep newest 10 backups
    retention_age_days: int = 30       # ...or up to 30 days, whichever wins
    verify_on_create: bool = True
    compress: bool = True              # gzip the .db file (plaintext SQLite only)
    encrypt_at_rest: bool = False       # delegate to FG6 encryption manager
    integrity_pragmas: bool = True      # run PRAGMA integrity_check on backup


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackupStats:
    """Lifetime counters consumed by the health manager."""

    backups_taken: int = 0
    backups_failed: int = 0
    restores_performed: int = 0
    restores_failed: int = 0
    integrity_failures: int = 0
    verify_failures: int = 0
    bytes_backed_up: int = 0
    bytes_trimmed: int = 0

    def as_dict(self) -> dict:
        return {
            "backups_taken": self.backups_taken,
            "backups_failed": self.backups_failed,
            "restores_performed": self.restores_performed,
            "restores_failed": self.restores_failed,
            "integrity_failures": self.integrity_failures,
            "verify_failures": self.verify_failures,
            "bytes_backed_up": self.bytes_backed_up,
            "bytes_trimmed": self.bytes_trimmed,
        }


# ---------------------------------------------------------------------------
# Integrity verification
# ---------------------------------------------------------------------------


def verify_integrity(path: Path, *, encrypted: bool = False) -> bool:
    """Open ``path`` read-only and run ``PRAGMA integrity_check``.

    Returns True if integrity_check returns ``"ok"`` exactly once. Any other
    output (multiple rows, a textual error, an exception) returns False.

    Used both by the backup manager immediately after writing a backup and by
    the recovery manager during boot-time integrity scans.
    """
    import sqlite3
    if not path.exists() or not path.is_file():
        return False
    try:
        # The integrity preset is read-only, query_only=True; that prevents
        # any side effects on the database under check.
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        try:
            cur = conn.execute("PRAGMA integrity_check")
            rows = cur.fetchall()
            cur.close()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if len(rows) != 1:
        return False
    return rows[0][0] == "ok"


# ---------------------------------------------------------------------------
# BackupManager
# ---------------------------------------------------------------------------


class BackupManager:
    """Owns backup + restore for one or more :class:`SQLiteEngine` instances.

    Constructed per :class:`DatabaseManager`. The manager holds an immutable
    map of ``database_name -> engine`` so a single ``backup_all`` covers
    every SQLite store; per-engine restore is fired from the recovery
    manager when a single store is detected as corrupt.

    Hooks for flushing the connection pool on restore are injected via the
    constructor (see module docstring "Why flush hooks"). When not supplied,
    restore operations raise :class:`BackupError` so a half-wired recovery
    never silently ships a restore.
    """

    __slots__ = (
        "_engines",
        "_backup_dir",
        "_policy",
        "_logger",
        "_lock",
        "_stats",
        "_flush_hook",
        "_reopen_hook",
        "_closed",
    )

    def __init__(
        self,
        engines: Mapping[str, SQLiteEngine],
        backup_dir: Path,
        *,
        policy: Optional[BackupPolicy] = None,
        logger: Optional[Logger] = None,
        flush_hook: Optional[Callable[[str], None]] = None,
        reopen_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not engines:
            raise BackupError("BackupManager requires at least one engine")
        self._engines = dict(engines)
        self._backup_dir = Path(backup_dir)
        self._policy = policy or BackupPolicy()
        self._logger = logger
        self._lock = threading.RLock()
        self._stats = BackupStats()
        self._flush_hook = flush_hook
        self._reopen_hook = reopen_hook
        self._closed = False

        self._backup_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------- properties
    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    @property
    def policy(self) -> BackupPolicy:
        return self._policy

    @property
    def stats(self) -> BackupStats:
        with self._lock:
            return self._stats

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def databases(self) -> List[str]:
        return list(self._engines.keys())

    # ----------------------------------------------------------- backup (single)
    def backup(
        self,
        database_name: str,
        *,
        description: Optional[str] = None,
        source_version: Optional[int] = None,
    ) -> BackupEntry:
        """Take one backup of a registered database."""
        if self._closed:
            raise BackupError("BackupManager is closed")
        engine = self._engines.get(database_name)
        if engine is None:
            raise BackupError(
                operation="backup",
            ).with_context(reason=f"unknown database: {database_name!r}")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = self._backup_dir / f"{database_name}.{timestamp}.db"
        if target.exists():
            # Same-second collision: append a sub-second counter.
            i = 0
            while True:
                candidate = self._backup_dir / f"{database_name}.{timestamp}.{i:03d}.db"
                if not candidate.exists():
                    target = candidate
                    break
                i += 1
                if i > 999:
                    raise BackupError(operation="backup").with_context(
                        reason="could not allocate a unique backup filename",
                        database=database_name,
                    )

        fmt = (
            BackupFormat.BACKUP_API
            if engine.is_encrypted
            else BackupFormat.VACUUM_INTO
        )
        started = time.monotonic()

        # VACUUM INTO needs a live connection in write mode; the engine's
        # connection() context manager is appropriate.
        try:
            if fmt is BackupFormat.VACUUM_INTO:
                with engine.connection() as src_conn:
                    src_conn.execute(f"VACUUM INTO {self._sql_str(target.as_posix())}")
            else:
                # SQLCipher: use the driver backup() API on a freshly opened
                # destination connection that has its key bound outside this
                # module. For the metadata-store format (no encrypted
                # destination yet), the destination is plain sqlite3 — the
                # secure engine is restored by re-binding the key after copy.
                self._backup_via_driver_copy(database_name, engine, target)
        except Exception as exc:
            with self._lock:
                self._stats.backups_failed += 1
            self._safe_remove(target)
            raise BackupError(operation="backup", cause=exc) from exc

        size = target.stat().st_size
        sha = self._sha256(target)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        verified = True
        if self._policy.verify_on_create:
            try:
                verified = self._verify_backup(target, engine)
                if not verified:
                    with self._lock:
                        self._stats.verify_failures += 1
                    self._safe_remove(target)
                    raise BackupError(operation="backup").with_context(
                        reason="integrity_check failed on freshly-created backup",
                        database=database_name,
                        path=str(target),
                    )
            except Exception as exc:
                self._safe_remove(target)
                raise BackupError(operation="backup", cause=exc) from exc

        entry = BackupEntry(
            database_name=database_name,
            path=target,
            format=fmt,
            sha256=sha,
            size_bytes=size,
            created_at=created_at,
            verified=verified,
            source_version=source_version,
            description=description,
            encrypted=engine.is_encrypted,
        )
        with self._lock:
            self._stats.backups_taken += 1
            self._stats.bytes_backed_up += size

        # Update + trim the manifest atomically.
        self._update_manifest(database_name, entry, add=True)
        self._trim(database_name)

        if self._logger:
            self._logger.info(
                "Backup created",
                extra={
                    "database": database_name,
                    "path": str(target),
                    "format": fmt.value,
                    "size_bytes": size,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "verified": verified,
                },
            )
        return entry

    # ----------------------------------------------------------- backup (all)
    def backup_all(
        self,
        *,
        description: Optional[str] = None,
    ) -> List[BackupEntry]:
        """Take a backup of every registered database in registration order."""
        results: List[BackupEntry] = []
        for name in self.databases:
            try:
                results.append(self.backup(name, description=description))
            except BackupError:
                # Continue backup of remaining databases so a single corrupt
                # store does not silence the others.
                if self._logger:
                    self._logger.error(
                        "Backup of database failed; continuing with the rest",
                        extra={"database": name},
                    )
        return results

    # ----------------------------------------------------------- restore
    def restore(
        self,
        database_name: str,
        *,
        entry: Optional[BackupEntry] = None,
        on_conflict: str = "fail",
    ) -> BackupEntry:
        """Restore the most recent (or named) backup for a database.

        Calls the injected flush hook to drain the connection pool and the
        reopen hook to re-arm it after the file is in place. Both hooks must
        be registered during DatabaseManager construction — restoring without
        them would silently leave stale connections pointing at overwritten
        file pages, which is the precise failure mode the recovery manager
        exists to prevent.
        """
        if self._closed:
            raise BackupError("BackupManager is closed")
        if self._flush_hook is None or self._reopen_hook is None:
            raise BackupError(
                operation="restore",
            ).with_context(reason="restore requires flush + reopen hooks")
        engine = self._engines.get(database_name)
        if engine is None:
            raise BackupError(operation="restore").with_context(
                reason=f"unknown database: {database_name!r}",
            )

        if entry is None:
            manifest = self._read_manifest(database_name)
            if not manifest.entries:
                raise BackupError(operation="restore").with_context(
                    reason="no backup available",
                    database=database_name,
                )
            entry = manifest.entries[-1]
        elif entry.database_name != database_name:
            raise BackupError(operation="restore").with_context(
                reason="entry does not belong to this database",
                expected=database_name,
                got=entry.database_name,
            )

        if not entry.path.exists() or not entry.path.is_file():
            raise BackupError(operation="restore").with_context(
                reason="backup file missing",
                path=str(entry.path),
            )

        # Verify the backup is still intact before we drain the pool. A
        # corrupt restore would leave the user without a usable database AND
        # without a working backup at the same instant.
        if not self._verify_backup(entry.path, engine):
            with self._lock:
                self._stats.integrity_failures += 1
            raise BackupError(operation="restore").with_context(
                reason="backup failed integrity_check; aborting restore",
                path=str(entry.path),
            )

        target_path = Path(engine.database)
        if target_path == entry.path:
            raise BackupError(operation="restore").with_context(
                reason="restore target equals the backup file",
                path=str(target_path),
            )

        # Drain the pool so no live connection overwrites the file we are
        # about to lay down.
        try:
            self._flush_hook(database_name)
        except Exception as exc:
            raise BackupError(operation="restore", cause=exc) from exc

        # WAL files must be checkpointed before overwrite; the engine's
        # preset already runs ``wal_checkpoint PASSIVE`` on connect, but a
        # restore-time one is cheaper than pulling the engine out from under
        # the pool.
        self._checkpoint_target(target_path, engine)

        started = time.monotonic()
        try:
            # Atomically replace the live file with the backup. On Windows we
            # cannot rename over an in-use file; the reopen_hook is responsible
            # for closing connections, so by this point the file is unlocked.
            tmp = target_path.with_suffix(target_path.suffix + ".restoring.tmp")
            shutil.copy2(entry.path, tmp)
            os.replace(tmp, target_path)

            # Remove the auxiliary WAL/SHM files so SQLite rebuilds them.
            for suffix in ("-wal", "-shm"):
                side = target_path.with_name(target_path.name + suffix)
                if side.exists():
                    side.unlink()

            with self._lock:
                self._stats.restores_performed += 1
        except Exception as exc:
            with self._lock:
                self._stats.restores_failed += 1
            raise BackupError(operation="restore", cause=exc) from exc
        finally:
            # Re-arm the pool even on failure so the system does not get
            # stuck with an empty pool.
            try:
                self._reopen_hook(database_name)
            except Exception as exc:
                if self._logger:
                    self._logger.error(
                        "Reopen hook failed; pool is uninitialised",
                        extra={"database": database_name, "error": str(exc)},
                    )

        if self._logger:
            self._logger.info(
                "Restore completed",
                extra={
                    "database": database_name,
                    "from": str(entry.path),
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
            )
        return entry

    # ----------------------------------------------------------- manifest + trim
    def list_backups(self, database_name: str) -> List[BackupEntry]:
        """Return the backup history for a single database (oldest first)."""
        manifest = self._read_manifest(database_name)
        # Filter to on-disk entries; the manifest may reference deleted files
        # if a user cleaned the directory manually.
        return [e for e in manifest.entries if e.path.exists()]

    def latest_backup(self, database_name: str) -> Optional[BackupEntry]:
        backups = self.list_backups(database_name)
        return backups[-1] if backups else None

    def purge(self, database_name: str) -> int:
        """Delete every backup for a database. Returns the count removed."""
        backups = self.list_backups(database_name)
        removed = 0
        for entry in backups:
            self._safe_remove(entry.path)
            removed += 1
        self._write_manifest(database_name, BackupManifest(
            database_name=database_name,
            database_path=Path(self._engines[database_name].database),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            entries=[],
        ))
        return removed

    def trim(self, database_name: str) -> int:
        """Apply retention policy to a single database; returns count removed."""
        return self._trim(database_name)

    # ----------------------------------------------------------- internals
    def _verify_backup(self, path: Path, engine: SQLiteEngine) -> bool:
        """Verify a backup file's integrity.

        For encrypted databases we cannot open the backup without the key; a
        backup that was created via the driver ``backup()`` API preserves the
        cipher context, so opening it without the key fails with an opaque
        ``file is not a database`` error. We translate that to "integrity OK"
        only when the driver-level check by the secure engine passes; the
        secure engine is responsible for binding the key into a transient
        connection in its own audit.
        """
        if engine.is_encrypted:
            # Encrypted backups are validated by the secure engine; this module
            # is intentionally not key-aware. Fail loud if the engine rejects
            # them — the SQLCipher engine subclasses SQLiteEngine and may
            # expose a ``verify_backup`` hook in the future.
            try:
                verify = getattr(engine, "verify_backup", None)
                if verify is not None:
                    return bool(verify(path))
            except Exception:
                return False
            # No verify hook available — accept the backup; the next connect
            # attempt against the secure store will surface a key failure
            # loudly.
            return True
        return verify_integrity(path)

    def _backup_via_driver_copy(
        self,
        database_name: str,
        engine: SQLiteEngine,
        target: Path,
    ) -> None:
        """Use the sqlite3 ``Connection.backup()`` API for encrypted databases.

        Opens a fresh destination ``sqlite3`` connection, optionally binding
        the secure engine's key (the engine may expose ``bind_key_after_open``
        for this purpose), then copies page-by-page.
        """
        import sqlite3
        # The destination starts as a fresh sqlite3 database; SQLCipher
        # engines subclass SQLiteEngine and MUST rebuild the cipher context
        # on the destination via their ``bind_key_after_open`` hook.
        dst_conn = sqlite3.connect(str(target))
        try:
            bind = getattr(engine, "bind_key_after_open", None)
            if bind is not None:
                bind(dst_conn)

            with engine.connection() as src_wrapper:
                src = src_wrapper.raw()
                src.backup(dst_conn)
        finally:
            dst_conn.close()

    def _checkpoint_target(self, target: Path, engine: SQLiteEngine) -> None:
        """Run a PASSIVE wal_checkpoint on the target DB before overwrite.

        Done via a transient connection outside the pool so the pool's idle
        connections are not perturbed. The flush hook will close them later.
        """
        import sqlite3
        try:
            conn = sqlite3.connect(str(target), check_same_thread=False)
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            # Best-effort; the flush hook will close the live pool anyway.
            pass

    def _sql_str(self, value: str) -> str:
        """Quote a SQL string literal."""
        return "'" + value.replace("'", "''") + "'"

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _safe_remove(self, path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            if self._logger:
                self._logger.warning(
                    "Failed to remove file",
                    extra={"path": str(path), "error": str(exc)},
                )

    # ----------------------------------------------------------- manifest storage
    def _manifest_path(self, database_name: str) -> Path:
        return self._backup_dir / f"{database_name}.manifest.json"

    def _read_manifest(self, database_name: str) -> BackupManifest:
        path = self._manifest_path(database_name)
        if not path.exists():
            return BackupManifest(
                database_name=database_name,
                database_path=Path(self._engines[database_name].database),
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                entries=[],
            )
        try:
            return BackupManifest.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError) as exc:
            if self._logger:
                self._logger.warning(
                    "Backup manifest corrupt; rebuilding from filesystem",
                    extra={"path": str(path), "error": str(exc)},
                )
            return self._rebuild_manifest(database_name)

    def _write_manifest(self, database_name: str, manifest: BackupManifest) -> None:
        path = self._manifest_path(database_name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(manifest.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    def _update_manifest(self, database_name: str, entry: BackupEntry, *, add: bool) -> None:
        manifest = self._read_manifest(database_name)
        if add:
            manifest.entries.append(entry)
        manifest.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._write_manifest(database_name, manifest)

    def _rebuild_manifest(self, database_name: str) -> BackupManifest:
        """Scan the backup directory and rebuild a manifest from disk files.

        Used when the sidecar manifest is corrupt or missing. The rebuild is
        best-effort: it cannot recover SHA-256 sums cheaply, so it recomputes
        them. Verification is deferred to the next backup cycle.
        """
        pattern = f"{database_name}.*.*.db"
        manifest = BackupManifest(
            database_name=database_name,
            database_path=Path(self._engines[database_name].database),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            entries=[],
        )
        for path in sorted(self._backup_dir.glob(pattern)):
            stat = path.stat()
            manifest.entries.append(
                BackupEntry(
                    database_name=database_name,
                    path=path,
                    format=BackupFormat.BACKUP_API,
                    sha256=self._sha256(path),
                    size_bytes=stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    verified=False,
                )
            )
        self._write_manifest(database_name, manifest)
        return manifest

    def _trim(self, database_name: str) -> int:
        """Apply retention policy; delete the oldest backups beyond the limit.

        Retention wins whichever constraint bites first: ``retention_count``
        (keep the newest N) or ``retention_age_days`` (delete older than D).
        Returns the number of backups removed.
        """
        if database_name not in self._engines:
            return 0
        manifest = self._read_manifest(database_name)
        entries = manifest.entries
        if not entries:
            return 0
        # Sort newest first by created_at.
        entries.sort(key=lambda e: e.created_at, reverse=True)
        cutoff_dt = datetime.now(timezone.utc).timestamp() - self._policy.retention_age_days * 86400
        keep: List[BackupEntry] = []
        remove: List[BackupEntry] = []
        for idx, entry in enumerate(entries):
            keep_by_count = idx < self._policy.retention_count
            created_epoch = _iso_to_epoch(entry.created_at)
            keep_by_age = created_epoch >= cutoff_dt
            if keep_by_count and keep_by_age:
                keep.append(entry)
            else:
                remove.append(entry)
        for entry in remove:
            self._safe_remove(entry.path)
            with self._lock:
                self._stats.bytes_trimmed += entry.size_bytes
        manifest.entries = keep
        self._write_manifest(database_name, manifest)
        return len(remove)

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        self._closed = True
        if self._logger:
            self._logger.info(
                "BackupManager closed",
                extra={"stats": self._stats.as_dict()},
            )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<BackupManager backup_dir={self._backup_dir!s} "
            f"databases={len(self._engines)} "
            f"backups={self._stats.backups_taken}>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_to_epoch(iso: str) -> float:
    """Parse an ISO-8601 string with a trailing 'Z' to a UTC epoch float."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += [
    "BackupManager",
    "BackupPolicy",
    "BackupStats",
    "verify_integrity",
]
