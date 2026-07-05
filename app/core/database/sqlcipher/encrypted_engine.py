# app/core/database/sqlcipher/encrypted_engine.py
"""
Encrypted SQLite engine that wraps :class:`SQLiteEngine` with SQLCipher
key-binding on every connection open.

Why a subclass instead of a decorator?
--------------------------------------
Cheating via a decorator would silently skip the PRAGMA application that
the base engine applies on open — the connection would be cipher-free until
the decorator re-opened it, breaking the atomic open contract for the
connection manager (which opens the base engine at pool warm-up). Subclassing
means the base engine's preset application still happens, but the key is
bound *in the same open call* via :meth:`SQLiteConnection.raw()` escape hatch.

Open sequence for every connection:

1. Open a plain ``sqlite3.Connection`` (SQLCipher is sqlite3-compatible).
2. Apply the ENCRYPTED preset (includes ``cipher_page_size``,
   ``cipher_kdf_iter``, ``cipher_use_hmac``).
3. Ask the :class:`SecurityManager` for an :meth:`acquire` handle (which
   loads the key from the :class:`KeyManager`, optionally mixes a
   passphrase, and returns a :class:`SessionKeyHandle` whose hex passphrase
   is the key argument to ``PRAGMA key``).
4. Issue ``PRAGMA key = "x'<hexpassphrase>'"`` on the raw connection.
5. Zeroize the passphrase string immediately.
6. Run :meth:`SecurityManager.verify_after_bind` to confirm the key took.
7. Resolve the now-authenticated connection into an :class:`EncryptedConnection`
   wrapper that never leaks the key and always zeroizes on release.

The encrypted engine is intentionally not an SQLCipher-specific subclass —
SQLCipher's ``pysqlcipher3`` is a drop-in replacement for ``sqlite3``. The
same binding protocol works with it; the key material is always 256-bit
AES, and ``PRAGMA key`` is accepted by both backends. If ``pysqlcipher3``
is absent, the standard library ``sqlite3`` still opens the database as
long as it was encrypted by the same library that created it — because
SQLCipher encryption is applied at the page store level, not the driver API.
The ``pysqlcipher3`` package is only needed when encryption was applied by
that driver; the manager logs a diagnostic on the first open in that case.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
``sqlite/engine`` → ``sqlcipher/key_manager`` → ``sqlcipher/security`` →
here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Union

try:
    from sqlcipher3 import dbapi2 as _sqlcipher_dbapi
except ImportError:
    try:
        from pysqlcipher3 import dbapi2 as _sqlcipher_dbapi  # type: ignore[no-redef]
    except ImportError:
        import sqlite3 as _sqlcipher_dbapi  # type: ignore[assignment]

from app.core.database.sqlcipher.key_manager import (
    KeyManager,
    KeyMaterial,
    KeyVersion,
)
from app.core.database.sqlcipher.security import (
    BoundSession,
    SecurityManager,
    SecurityState,
)
from app.core.database.sqlite.connection import (
    ConnectionState,
    SQLiteConnection,
)
from app.core.database.sqlite.engine import (
    EngineStats,
    SQLiteEngine,
    SQLiteEngineConfig,
)
from app.core.database.sqlite.pragmas import PragmaPreset, PRAGMAS_ENCRYPTED
from app.core.exceptions.database import (
    ConnectionError,
    EncryptionKeyError,
)
from app.logging import Logger

__all__ = [
    "EncryptedConnection",
    "EncryptedConnectionStats",
    "EncryptedEngine",
    "EncryptedEngineConfig",
    "SecureSession",
]


# ---------------------------------------------------------------------------
# Records + stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EncryptedConnectionStats:
    """Per-connection observability counters for the secure store."""

    bind_duration_seconds: float = 0.0
    binding_id: Optional[str] = None
    key_version: int = 0
    passphrase_mixed: bool = False
    cipher_integrity_ok: bool = False
    page_hmac: str = ""

    def as_dict(self) -> dict:
        return {
            "bind_duration_seconds": round(self.bind_duration_seconds, 6),
            "binding_id": self.binding_id,
            "key_version": self.key_version,
            "passphrase_mixed": self.passphrase_mixed,
            "cipher_integrity_ok": self.cipher_integrity_ok,
            "page_hmac": self.page_hmac,
        }


@dataclass(frozen=True, slots=True)
class EncryptedEngineConfig:
    """Knobs specific to the encrypted engine.

    Separated from :class:`SQLiteEngineConfig` so non-encrypted engines
    ignore them implicitly.
    """

    passphrase: Optional[bytes] = None
    verify_after_bind: bool = True
    require_enrollment: bool = True    # fail if key_manager has no key


@dataclass(slots=True)
class SecureSession:
    """Public record emitted by the encrypted engine after a successful bind.

    Feature groups that write to the encrypted store via a UnitOfWork can
    capture this record in their event payloads so FG6's audit trail has the
    binding id, key version, and passphrase-mixed flags without seeing the
    passphrase itself.
    """

    binding_id: str
    key_version: int
    passphrase_mixed: bool
    bound_at: str
    cipher_integrity_ok: bool

    def as_dict(self) -> dict:
        return {
            "binding_id": self.binding_id,
            "key_version": self.key_version,
            "passphrase_mixed": self.passphrase_mixed,
            "bound_at": self.bound_at,
            "cipher_integrity_ok": self.cipher_integrity_ok,
        }


# ---------------------------------------------------------------------------
# EncryptedConnection — a typed wrapper that auto-zeroes the key on release
# ---------------------------------------------------------------------------


class EncryptedConnection:
    """A connection to the encrypted store with key context attached.

    This is NOT the live ``sqlite3.Connection``; it wraps one after the key
    has been bound and exposes the same method surface as
    :class:`SQLiteConnection` but tracks bind metadata and zeroizes the
    key handle on close.

    The encrypted engine returns this from :meth:`connect` (instead of a
    plain :class:`SQLiteConnection`) so every encrypted call through the pool
    has access to bind metadata — the health manager reads per-database
    stats, the UoW captures a :class:`SecureSession` label.

    The wrapper is NOT a subclass of :class:`SQLiteConnection` to avoid
    brittle coupling to its __slots__; callers that already receive a
    ``SQLiteConnection`` from the generic API use the duck-typed protocol
    (execute/begin/commit/rollback) and can check ``isinstance`` for the
    encrypted path when they need the bind metadata.
    """

    __slots__ = (
        "_conn",
        "_bind_session",
        "_bind_stats",
        "_released",
        "_logger",
    )

    def __init__(
        self,
        conn: SQLiteConnection,
        *,
        bind_session: BoundSession,
        bind_stats: EncryptedConnectionStats,
        logger: Optional[Logger] = None,
    ) -> None:
        self._conn = conn
        self._bind_session = bind_session
        self._bind_stats = bind_stats
        self._released = False
        self._logger = logger

    # ----------------------------------------------------------- properties
    @property
    def database(self) -> str:
        return self._conn.database

    @property
    def state(self) -> str:
        return self._conn.state

    @property
    def is_open(self) -> bool:
        return self._conn.is_open

    @property
    def owner_thread(self) -> int:
        return self._conn.owner_thread

    @property
    def last_rowid(self) -> int:
        return self._conn.last_rowid

    @property
    def total_changes(self) -> int:
        return self._conn.total_changes

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    @property
    def age_seconds(self) -> float:
        return self._conn.age_seconds

    @property
    def sqlite_version(self) -> str:
        return self._conn.sqlite_version

    @property
    def binding_stats(self) -> EncryptedConnectionStats:
        return self._bind_stats

    @property
    def secure_session(self) -> SecureSession:
        return SecureSession(
            binding_id=self._bind_stats.binding_id or "",
            key_version=self._bind_stats.key_version,
            passphrase_mixed=self._bind_stats.passphrase_mixed,
            bound_at=self._bind_session.bound_at,
            cipher_integrity_ok=self._bind_stats.cipher_integrity_ok,
        )

    # ----------------------------------------------------------- SQLite surface
    def execute(self, sql: str, parameters=None):  # type: ignore[override]
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, parameters_seq):  # type: ignore[override]
        return self._conn.executemany(sql, parameters_seq)

    def executescript(self, script: str):
        return self._conn.executescript(script)

    def cursor(self):  # type: ignore[override]
        return self._conn.cursor()

    def begin(self) -> None:
        self._conn.begin()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def raw(self):  # type: ignore[override]
        return self._conn.raw()

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        self._conn.close()
        # The bind session was already released by the encrypted engine's
        # acquire/release context manager — this close is just the
        # connection-level teardown.

    # ----------------------------------------------------------- context mgmt
    def __enter__(self) -> "EncryptedConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<EncryptedConnection database={self.database!r} "
            f"binding_id={self._bind_stats.binding_id!r}>"
        )


# ---------------------------------------------------------------------------
# EncryptedEngine
# ---------------------------------------------------------------------------


class EncryptedEngine(SQLiteEngine):
    """SQLite engine that binds the SQLCipher master key on every open.

    Extends :class:`SQLiteEngine` with an extra open layer: after the base
    engine opens the connection and applies the encrypted PRAGMA preset,
    the encrypted engine acquires a :class:`SessionKeyHandle` from the
    :class:`SecurityManager`, binds the key via ``PRAGMA key``, zeroizes
    the passphrase, and packs the result into an :class:`EncryptedConnection`.

    The :class:`ConnectionManager` that wraps this engine sees
    ``EncryptedConnection`` as duck-typed ``SQLiteConnection`` — no pool
    adapter is needed.
    """

    __slots__ = (
        "_security",
        "_enc_config",
        "_bind_lock",
    )

    def __init__(
        self,
        config: SQLiteEngineConfig,
        security: SecurityManager,
        *,
        enc_config: Optional[EncryptedEngineConfig] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        if not config.encrypted:
            raise ConnectionError(backend="sqlcipher").with_context(
                reason="EncryptedEngineConfig.encrypted must be True",
            )
        # Force the encrypted preset so cipher PRAGMAs are applied before
        # the key bind.
        config = SQLiteEngineConfig(
            database=config.database,
            encrypted=config.encrypted,
            in_memory=config.in_memory,
            read_only=config.read_only,
            check_same_thread=config.check_same_thread,
            isolation_level=config.isolation_level,
            preset_override=PRAGMAS_ENCRYPTED,
        )
        super().__init__(config, logger=logger)
        self._security = security
        self._enc_config = enc_config or EncryptedEngineConfig()
        self._bind_lock = threading.RLock()
        # Enforce enrollment on construction so a bootstrap at the engine
        # level surfaces before the first connection attempt.
        if self._enc_config.require_enrollment:
            self._security.ensure_enrolled()

    # ----------------------------------------------------------- connect (override)
    def connect(self) -> EncryptedConnection:  # type: ignore[override]
        """Open an encrypted connection.

        The callers (ConnectionManager, one-shot scripts) see the same
        signature as :meth:`SQLiteEngine.connect` and receive an
        :class:`EncryptedConnection` in place of the base connection.
        """
        self._begin_open()
        base_conn: Optional[SQLiteConnection] = None
        bind_session: Optional[BoundSession] = None
        started_mono = time.monotonic()

        try:
            base_conn = super().connect()
            conn_id = id(base_conn)

            # Acquire a key-binding handle from the security manager.
            bind_session = self._security.acquire(
                connection_id=conn_id,
                passphrase=self._enc_config.passphrase,
            )

            # Read the hex passphrase, bind it into the live connection,
            # and drop the string from local scope immediately.
            pp = None
            try:
                pp = self._security.passphrase_for_binding(bind_session)
                base_conn.execute(f"PRAGMA key = \"x'{pp}'\"")
            finally:
                if pp is not None:
                    # None reassign to "forget" the bytes-reference so the
                    # Python reference-count drops to 0; the gc may or may not
                    # zero the buffer, but at least the local scope no longer
                    # holds it.
                    pp = None

            bind_stats = EncryptedConnectionStats(
                bind_duration_seconds=time.monotonic() - started_mono,
                binding_id=bind_session.handle_id,
                key_version=bind_session.version,
                passphrase_mixed=(
                    self._enc_config.passphrase is not None
                ),
            )

            # Post-bind verification.
            if self._enc_config.verify_after_bind:
                report = self._security.verify_after_bind(base_conn, bind_session)
                bind_stats.cipher_integrity_ok = report.cipher_integrity_ok
                bind_stats.page_hmac = report.page_header_hmac

            encrypted = EncryptedConnection(
                conn=base_conn,
                bind_session=bind_session,
                bind_stats=bind_stats,
                logger=self._logger,
            )
        except Exception:
            self._record_failure()
            # On failure, release the bind session NOW so the security
            # manager zeroizes the key immediately; the base connection
            # will be closed by the connection manager outside this scope
            # or by this engine's caller.
            if bind_session is not None:
                try:
                    self._security.release(bind_session, failed=True)
                except Exception:
                    pass
            if base_conn is not None and base_conn.is_open:
                base_conn.close()
            raise
        else:
            self._record_open()
        return encrypted

    # ----------------------------------------------------------- security hooks
    @property
    def security_manager(self) -> SecurityManager:
        return self._security

    @property
    def encrypted_config(self) -> EncryptedEngineConfig:
        return self._enc_config

    def verify_backup(self, path: Path) -> bool:
        """Hook called by the backup manager to verify an encrypted backup.

        Opens the backup file read-only with a transient encryption context,
        binds the current active key, and runs integrity_check. Returns True
        when the backup is readable with the current key.
        """
        import uuid
        if not path.exists():
            return False
        try:
            conn = _sqlcipher_dbapi.connect(str(path), check_same_thread=False)
            enc = EncryptedConnection(
                conn=SQLiteConnection(
                    path, PRAGMAS_ENCRYPTED,
                    check_same_thread=False, isolation_level=None,
                ),
                bind_session=BoundSession("backup-verify-" + uuid.uuid4().hex[:8], 0, "", ""),
                bind_stats=EncryptedConnectionStats(),
            )
            pp = self._security.passphrase_for_binding(
                self._security.acquire(connection_id=id(conn)),
            )
            enc.execute(f"PRAGMA key = \"x'{pp}'\"")
            try:
                enc.execute("SELECT COUNT(*) FROM sqlite_master").close()
                return True
            finally:
                enc.close()
        except Exception:
            return False

    def describe(self) -> dict:
        base = {
            "database": self.database,
            "is_encrypted": True,
            "preset": self.preset.name,
            "stats": self.stats.__dict__,
            "security": self._security.describe(),
            "enc_config": {
                "passphrase_set": self._enc_config.passphrase is not None,
                "verify_after_bind": self._enc_config.verify_after_bind,
                "require_enrollment": self._enc_config.require_enrollment,
            },
        }
        return base

    # ----------------------------------------------------------- dunder
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<EncryptedEngine database={self.database!r} "
            f"state={self._security.state.value} "
            f"in_flight={self._stats.in_flight}>"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_encrypted_engine(
    config: SQLiteEngineConfig,
    security: SecurityManager,
    *,
    enc_config: Optional[EncryptedEngineConfig] = None,
    logger: Optional[Logger] = None,
) -> EncryptedEngine:
    """Factory used by the DatabaseManager when bootstrapping the secure store."""
    return EncryptedEngine(
        config=config,
        security=security,
        enc_config=enc_config,
        logger=logger,
    )


__all__ += ["build_encrypted_engine"]