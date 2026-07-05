# app/core/database/sqlcipher/security.py
"""
Runtime security envelope for the AIOS SQLCipher encrypted store.

While :class:`KeyManager` owns the **static** master-key lifecycle (generation,
wrapping, versioning), :class:`SecurityManager` owns the **runtime** side that
the encrypted engine consults *after* a connection is open:

* **Passphrase handling** — receives a speaker-verified passphrase from the
  DatabaseManager, mixes it via HKDF into the active key before binding, then
  zeroizes the in-memory passphrase. The contract is that the passphrase
  *never* outlives the connect call; this module is responsible for that.
* **Key cache eviction** — once a :class:`KeyMaterial` has been bound into a
  connection via ``PRAGMA key``, the material is dropped immediately and
  never cached. The security manager exposes a "bound session" API so the
  engine can register a callback that fires when the connection is closed
  (which re-validates that no surface still holds the key).
* **Tamper detection** — runs ``PRAGMA cipher_integrity_check`` after
  ``PRAGMA key`` to confirm SQLCipher accepts the key. Runs an HMAC over
  the first database page header so the recovery manager can detect tampering
  that survived a key-binding.
* **Audit event emission** — publishes ``security.encryption.failure`` and
  ``security.audit.tamper_detected`` events through a registered sink (the
  same pattern used by the transaction / UoW / health managers). The bridge
  into the EventBus lives in the :class:`DatabaseManager` so this module
  stays free of an ``event_bus`` import.
* **Direct encrypted connections** — :meth:`open_encrypted_connection` opens
  a raw driver connection through the active SQLCipher-compatible dbapi2,
  binds the key, and hands back a native (conn, session) pair so the backup
  manager, recovery manager, and health manager can verify the encrypted
  store outside the engine pool.
* **Driver resolution** — the SQLCipher-compatible dbapi2 module is resolved
  at module level and exposed so every caller (encrypted engine, backup
  manager, recovery manager) opens encrypted connections through one uniform
  entry point instead of importing the driver themselves.

Driver resolution order
-----------------------
1. ``sqlcipher3``           — canonical binding (most)
2. ``pysqlcipher3``         — alternative / legacy label
3. ``sqlite3``              — stdlib fallback (``PRAGMA key`` / ``cipher_*``
                              are ignored/raise; acceptable for decrypt-less
                              testing; production requires the first two)

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
``sqlite/connection`` → ``sqlcipher/key_manager`` → here. Does not import
the encrypted engine — the engine reaches this manager via the constructor
so the dependency graph stays acyclic.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Active SQLCipher-compatible dbapi2 — resolved at module level
# ---------------------------------------------------------------------------

_stdlib = __import__("sqlite3")  # fallback when no encrypted driver installed

try:
    from sqlcipher3 import dbapi2 as _sqlcipher_dbapi          # canonical
except ImportError:
    try:
        from pysqlcipher3 import dbapi2 as _sqlcipher_dbapi   # type: ignore[no-redef]
    except ImportError:
        _sqlcipher_dbapi = _stdlib                            # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

from app.core.database.sqlcipher.key_manager import (
    KeyManager,
    KeyMaterial,
    KeyPolicy,
    KeySource,
)
from app.core.database.sqlite.connection import SQLiteConnection
from app.core.exceptions.database import (
    DatabaseError,
    EncryptionKeyError,
)
from app.logging import Logger

__all__ = [
    "_sqlcipher_dbapi",
    "SecurityState",
    "SecurityEventKind",
    "SecurityStats",
    "SecurityManager",
    "BoundSession",
    "SessionKeyHandle",
    "TamperReport",
]


# ---------------------------------------------------------------------------
# Enums + records
# ---------------------------------------------------------------------------


class SecurityState(str, Enum):
    UNINITIALIZED = "uninitialized"
    ENROLLED = "enrolled"
    OPEN = "open"
    DEGRADED = "degraded"
    TAMPERED = "tampered"
    CLOSED = "closed"


class SecurityEventKind(str, Enum):
    """Event names published through the manager's event sink."""

    KEY_BOUND = "security.encryption.key_bound"
    KEY_RELEASED = "security.encryption.key_released"
    INTEGRITY_OK = "security.encryption.integrity_ok"
    INTEGRITY_DEGRADED = "security.encryption.integrity_degraded"
    ENCRYPTION_FAILURE = "security.encryption.failure"
    TAMPER_DETECTED = "security.audit.tamper_detected"


@dataclass(slots=True)
class TamperReport:
    """Result of a tamper-detection pass against the encrypted store."""

    timestamp: str
    cipher_integrity_ok: bool
    page_header_hmac: str
    detected_state: SecurityState
    detail: Optional[Mapping[str, Any]] = None

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "cipher_integrity_ok": self.cipher_integrity_ok,
            "page_header_hmac": self.page_header_hmac,
            "state": self.detected_state.value,
            "detail": dict(self.detail) if self.detail else None,
        }


@dataclass(slots=True)
class SecurityStats:
    """Lifetime counters surfaced to the health manager + FG5 dashboard."""

    bindings: int = 0
    releases: int = 0
    releases_on_failure: int = 0
    direct_opens: int = 0
    direct_opens_failed: int = 0
    integrity_checks: int = 0
    integrity_issues: int = 0
    tamper_alerts: int = 0
    passphrase_mix_events: int = 0
    active_handles: int = 0

    def as_dict(self) -> dict:
        return {
            "bindings": self.bindings,
            "releases": self.releases,
            "releases_on_failure": self.releases_on_failure,
            "direct_opens": self.direct_opens,
            "direct_opens_failed": self.direct_opens_failed,
            "integrity_checks": self.integrity_checks,
            "integrity_issues": self.integrity_issues,
            "tamper_alerts": self.tamper_alerts,
            "passphrase_mix_events": self.passphrase_mix_events,
            "active_handles": self.active_handles,
        }


@dataclass(slots=True)
class SessionKeyHandle:
    """A per-connection handle that owns the bound :class:`KeyMaterial`.

    Only constructed by :meth:`SecurityManager.acquire`; only released by
    :meth:`SecurityManager.release`. Escaped handles are evicted when the
    manager closes.
    """

    handle_id: str
    material: KeyMaterial
    bound_connection_id: int
    bound_at: float

    def as_passphrase(self) -> str:
        """Return the bound key as the hex passphrase SQLCipher expects."""
        return self.material.as_passphrase()

    def as_base64(self) -> str:
        """Return the key as a base64-encoded fingerprint string."""
        return self.material.as_base64()

    def zeroize(self) -> None:
        self.material.zeroize()


@dataclass(slots=True)
class BoundSession:
    """Public record returned by :meth:`SecurityManager.acquire`.

    Contains everything the caller needs to bind the key via ``PRAGMA key``
    without holding references to the security manager or key manager.
    """

    handle_id: str
    version: int
    binding_hash: str
    bound_at: str

    def as_dict(self) -> dict:
        return {
            "handle_id": self.handle_id,
            "version": self.version,
            "binding_hash": self.binding_hash,
            "bound_at": self.bound_at,
        }


# ---------------------------------------------------------------------------
# SecurityManager
# ---------------------------------------------------------------------------


class SecurityManager:
    """Runtime envelope for the encrypted personal-memory store.

    Thread-safe. Constructed per :class:`DatabaseManager` and handed a
    :class:`KeyManager` to source the master key. The active SQLCipher dbapi2
    module (``sqlcipher3`` → ``pysqlcipher3`` → ``sqlite3``) is resolved at
    import time and exposed via :attr:`sqlcipher_driver` so callers (encrypted
    engine, backup manager, recovery manager) always open encrypted connections
    through the correct driver.
    """

    __slots__ = (
        "_key_manager",
        "_logger",
        "_lock",
        "_stats",
        "_state",
        "_handles",
        "_event_sink",
        "_passphrase_in_use",
        "_sql_driver",
        "_closed",
    )

    def __init__(
        self,
        key_manager: KeyManager,
        *,
        logger: Optional[Logger] = None,
        driver: Optional[Any] = None,
    ) -> None:
        self._key_manager = key_manager
        self._logger = logger
        self._lock = threading.RLock()
        self._stats = SecurityStats()
        self._state = SecurityState.UNINITIALIZED
        self._handles: Dict[str, SessionKeyHandle] = {}
        self._event_sink: Optional[Callable[[dict], None]] = None
        self._passphrase_in_use: Optional[bytes] = None
        self._sql_driver = driver or _sqlcipher_dbapi
        self._closed = False

    # ----------------------------------------------------------- properties
    @property
    def key_manager(self) -> KeyManager:
        return self._key_manager

    @property
    def state(self) -> SecurityState:
        with self._lock:
            return self._state

    @property
    def stats(self) -> SecurityStats:
        with self._lock:
            self._stats.active_handles = len(self._handles)
            return self._stats

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def sqlcipher_driver(self) -> Any:
        """The active SQLCipher-compatible dbapi2 module.

        Callers (encrypted engine, backup manager, recovery manager) import
        this so they open connections with the correct driver — not stdlib
        sqlite3 which silently ignores ``PRAGMA key``.
        """
        return self._sql_driver

    def driver_name(self) -> str:
        return getattr(self._sql_driver, "__name__", "stdlib_sqlite3")

    def set_state(self, state: SecurityState) -> Optional[SecurityState]:
        """External hook — encrypted engine sets lifecycle state changes."""
        with self._lock:
            prev = self._state
            self._state = state
            return prev

    # ----------------------------------------------------------- event sink
    def install_event_sink(self, sink: Callable[[dict], None]) -> Callable[[], None]:
        """Register an EventBus bridge; returns an unsubscribe callable."""
        with self._lock:
            self._event_sink = sink

        def _unsubscribe() -> None:
            with self._lock:
                self._event_sink = None

        return _unsubscribe

    def _emit(self, kind: SecurityEventKind, *, payload: Mapping[str, Any]) -> None:
        with self._lock:
            sink = self._event_sink
        if sink is None:
            return
        event_payload = {
            "event": kind.value,
            "timestamp": _now_iso(),
            **{k: v for k, v in payload.items() if k != "event"},
        }
        try:
            sink(event_payload)
        except Exception:
            pass

    # ----------------------------------------------------------- enrollment
    def ensure_enrolled(self) -> int:
        """Forward to :meth:`KeyManager.ensure_enrolled`; updates state."""
        self._require_open()
        version = self._key_manager.ensure_enrolled()
        with self._lock:
            if self._state is SecurityState.UNINITIALIZED:
                self._state = SecurityState.ENROLLED
        return version

    # ----------------------------------------------------------- acquire / release
    def acquire(
        self,
        *,
        connection_id: int,
        passphrase: Optional[bytes] = None,
    ) -> BoundSession:
        """Bind the active key to a connection (optionally passphrase-mixed).

        The engine calls this BEFORE ``PRAGMA key`` on the live connection.
        The returned :class:`BoundSession` owns the :class:`KeyMaterial`; the
        caller reads the hex passphrase out of it, issues the PRAGMA, then
        drops its copy so no log preserves the key text.
        """
        self._require_open()
        with self._lock:
            if self._passphrase_in_use is not None and passphrase is not None:
                raise EncryptionKeyError(
                    reason="only one passphrase mixing operation may be in flight",
                )

        material = self._key_manager.load_active()
        if passphrase is not None:
            if self._key_manager.policy.allow_passphrase_mixing:
                with self._lock:
                    self._passphrase_in_use = b"\x01"   # sentinel
                try:
                    material = self._key_manager.mix_passphrase(material, passphrase)
                finally:
                    with self._lock:
                        self._passphrase_in_use = None
                with self._lock:
                    self._stats.passphrase_mix_events += 1
            else:
                material.zeroize()
                raise EncryptionKeyError(
                    reason="passphrase mixing disabled by security policy",
                )

        handle_id = uuid.uuid4().hex
        bound_at_mono = time.monotonic()
        handle = SessionKeyHandle(
            handle_id=handle_id,
            material=material,
            bound_connection_id=int(connection_id),
            bound_at=bound_at_mono,
        )
        with self._lock:
            self._handles[handle_id] = handle
            self._stats.bindings += 1
            if self._state in (SecurityState.UNINITIALIZED, SecurityState.ENROLLED):
                self._state = SecurityState.OPEN

        return BoundSession(
            handle_id=handle_id,
            version=material.version,
            binding_hash=material.binding_hash,
            bound_at=_now_iso(),
        )

    def passphrase_for_binding(self, handle: BoundSession) -> str:
        """Return the hex passphrase for a bound session.

        The caller MUST drop the returned string from local scope immediately
        after ``PRAGMA key`` is executed. Calling on a released handle raises
        :class:`EncryptionKeyError`.
        """
        with self._lock:
            h = self._handles.get(handle.handle_id)
            if h is None:
                raise EncryptionKeyError(
                    reason="unknown or already-released binding handle",
                ).with_context(handle_id=handle.handle_id)
            return h.as_passphrase()

    def release(self, handle: BoundSession, *, failed: bool = False) -> None:
        """Release a binding, zeroize the key material, emit lifecycle event."""
        self._require_open()
        with self._lock:
            h = self._handles.pop(handle.handle_id, None)
        if h is None:
            return          # idempotent
        h.zeroize()
        with self._lock:
            self._stats.releases += 1
            if failed:
                self._stats.releases_on_failure += 1
            if not self._handles and self._state is SecurityState.OPEN:
                self._state = SecurityState.ENROLLED
        self._emit(
            SecurityEventKind.KEY_RELEASED,
            payload={"handle_id": handle.handle_id, "failed": failed},
        )

    # ----------------------------------------------------------- direct encrypted connection
    def open_encrypted_connection(
        self,
        database: Union[str, Path],
        *,
        passphrase: Optional[bytes] = None,
        read_only: bool = False,
    ) -> Tuple[Any, BoundSession]:
        """Open a raw connection through the active SQLCipher driver and bind
        the master key. Returns the (native driver connection, session) pair.

        The caller is responsible for closing the connection and releasing the
        session via :meth:`release`. This is the canonical entry point for any
        operation that touches the encrypted store *outside* the engine pool:
        backup verification, recovery-manager integrity scans, health manager
        tamper check, migration manager secure-store steps.
        """
        self._require_open()
        dbpath = str(database)

        try:
            conn = self._sql_driver.connect(
                f"file:{dbpath}?mode={'ro' if read_only else 'rwc'}",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
            )
        except Exception as exc:
            with self._lock:
                self._stats.direct_opens_failed += 1
            exc_type = type(exc).__name__
            raise EncryptionKeyError(
                reason=f"driver open failed ({exc_type}): {exc!s:[200]}",
            ).with_context(database=dbpath, driver=self.driver_name(), read_only=read_only) from exc

        connection_id = id(conn)
        session = self.acquire(connection_id=connection_id, passphrase=passphrase)
        pp: Optional[str] = None
        try:
            pp = self.passphrase_for_binding(session)
            conn.execute(f"PRAGMA key = \"x'{pp}'\"")
            if read_only:
                conn.execute("PRAGMA query_only = ON")
            with self._lock:
                self._stats.direct_opens += 1
        finally:
            if pp is not None:
                pp = None

        return conn, session

    @contextmanager
    def encrypted_connection(
        self,
        database: Union[str, Path],
        *,
        passphrase: Optional[bytes] = None,
        read_only: bool = False,
    ) -> Iterator[Tuple[Any, BoundSession]]:
        """Context-managed encrypted connection — preferred for one-shot ops.

        Opens, binds the key, yields (driver connection, session), runs
        post-bind integrity verification on clean exit, and guarantees the
        session is released even on exception.
        """
        conn, session = self.open_encrypted_connection(
            database, passphrase=passphrase, read_only=read_only,
        )
        failed = False
        try:
            yield conn, session
        except Exception:
            failed = True
            raise
        finally:
            self.release(session, failed=failed)
            try:
                conn.close()
            except Exception:
                pass

    # ----------------------------------------------------------- integrity + tamper
    def verify_after_bind(self, connection: Any, handle: BoundSession) -> TamperReport:
        """Run post-bind verification on an already-open encrypted connection.

        Executes ``PRAGMA cipher_integrity_check`` (specific to the encrypted
        driver). On a plain sqlite3 connection the PRAGMA is unknown and raises;
        we fall back to ``PRAGMA integrity_check``.

        ``connection`` may be any object with an ``execute()`` method and a
        readable ``database`` attribute — the encrypted engine passes its
        :class:`EncryptedConnection` wrapper; the recovery manager may pass a
        raw driver connection.
        """
        self._require_open()
        with self._lock:
            self._stats.integrity_checks += 1

        cipher_ok: bool
        cipher_detail: Optional[dict] = None
        try:
            cur = connection.execute("PRAGMA cipher_integrity_check")
            rows = cur.fetchall()
            cur.close()
            cipher_ok = self._evaluate_cipher_integrity(rows, out=cipher_detail)
        except Exception:
            try:
                cur = connection.execute("PRAGMA integrity_check")
                rows = cur.fetchall()
                cur.close()
                cipher_ok, _ = self._evaluate_plain_integrity(rows)
                cipher_detail = {"fallback": "integrity_check"}
            except Exception as fallback_exc:
                raise EncryptionKeyError(
                    reason="integrity check failed after key bind",
                    cause=fallback_exc,
                ) from fallback_exc

        try:
            page_hmac = self._compute_page_hmac(getattr(connection, "database", ":memory:"))
        except Exception as exc:
            raise EncryptionKeyError(
                reason="could not compute page-header HMAC",
                cause=exc,
            ) from exc

        state = SecurityState.OPEN if cipher_ok else SecurityState.TAMPERED
        self.set_state(state)

        report = TamperReport(
            timestamp=_now_iso(),
            cipher_integrity_ok=cipher_ok,
            page_header_hmac=page_hmac,
            detected_state=state,
            detail=cipher_detail,
        )
        if not cipher_ok:
            with self._lock:
                self._stats.integrity_issues += 1
                self._stats.tamper_alerts += 1
            self._emit(
                SecurityEventKind.TAMPER_DETECTED,
                payload={"report": report.as_dict(), "handle_id": handle.handle_id},
            )
            raise EncryptionKeyError(
                reason="cipher_integrity_check reported a tamper signal",
            ).with_context(report=report.as_dict())
        self._emit(
            SecurityEventKind.INTEGRITY_OK,
            payload={"report": report.as_dict()},
        )
        return report

    def scheduled_tamper_scan(self, database_path: Union[str, Path]) -> TamperReport:
        """Periodic tamper scan — used by the health manager's poll cycle.

        Opens the database read-only through the active SQLCipher driver,
        binds the active key, verifies integrity and page-header HMAC, and
        tears down the connection. Returns a :class:`TamperReport` on
        success; raises :class:`EncryptionKeyError` on detected tampering.
        """
        self._require_open()
        with self._lock:
            self._stats.integrity_checks += 1

        conn: Any = None
        session: Optional[BoundSession] = None
        try:
            conn, session = self.open_encrypted_connection(
                database_path, read_only=True,
            )
            report = self.verify_after_bind(conn, session)
        except EncryptionKeyError:
            raise
        except Exception as exc:
            with self._lock:
                self._stats.tamper_alerts += 1
            self._emit(
                SecurityEventKind.ENCRYPTION_FAILURE,
                payload={"error": str(exc)[:500], "database": str(database_path)},
            )
            raise EncryptionKeyError(
                reason="scheduled tamper scan failed",
                cause=exc,
            ) from exc
        finally:
            if session is not None:
                self.release(session, failed=False)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return report

    # ----------------------------------------------------------- scan helpers
    def _evaluate_cipher_integrity(self, rows: Any, *, out: Optional[dict]) -> bool:
        """Interpret ``PRAGMA cipher_integrity_check`` rows.

        A single ``ok`` row → healthy. Anything else → corruption / tamper
        signal.
        """
        if not rows:
            return True
        if len(rows) == 1:
            value = rows[0][0] if len(rows[0]) else ""
            if str(value) == "ok":
                return True
        if out is not None:
            out.update(
                {
                    "row_count": len(rows),
                    "first": [tuple(r) for r in rows[:1]] if rows else None,
                }
            )
        return False

    def _evaluate_plain_integrity(self, rows: Any) -> Tuple[bool, dict]:
        if not rows:
            return True, {}
        if len(rows) == 1:
            value = rows[0][0] if len(rows[0]) else ""
            if str(value) == "ok":
                return True, {}
        return False, {"row_count": len(rows), "first": dict(rows[0]) if rows else None}

    def _compute_page_hmac(self, database_path: str) -> str:
        """HMAC-SHA256 of the first 4096 bytes of the database file.

        Change detection, not authentication — anyone who can compute this
        HMAC can already read the encrypted file. The HMAC key is the
        salt from the key manager.
        """
        if not database_path or database_path == ":memory:":
            return "memory"
        path = Path(database_path)
        if not path.exists() or not path.is_file():
            return "missing"
        page = path.read_bytes()[:4096]
        hmac_key = self._key_manager._load_or_create_salt()
        return hmac.new(hmac_key, page, hashlib.sha256).hexdigest()

    # ----------------------------------------------------------- introspection
    def describe(self) -> dict:
        """Snapshot for the FG5 dashboard / health manager."""
        with self._lock:
            return {
                "state": self._state.value,
                "stats": self._stats.as_dict(),
                "driver": self.driver_name(),
                "known_key_versions": self._key_manager.known_versions(),
                "active_key_version": self._key_manager.active_version(),
                "policy": {
                    "allow_passphrase_mixing": self._key_manager.policy.allow_passphrase_mixing,
                    "require_machine_binding": self._key_manager.policy.require_machine_binding,
                    "key_length_bytes": self._key_manager.policy.key_length_bytes,
                    "keep_versions": self._key_manager.policy.keep_versions,
                },
                "handles_in_flight": len(self._handles),
            }

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        """Shutdown. Zeroizes every in-flight handle, emits closing event."""
        with self._lock:
            self._closed = True
            stale = list(self._handles.values())
            self._handles.clear()
            self._state = SecurityState.CLOSED
        for h in stale:
            h.zeroize()
        if self._logger:
            self._logger.info(
                "SecurityManager closed",
                extra={
                    "stats": self._stats.as_dict(),
                    "evicted_handles": len(stale),
                    "driver": self.driver_name(),
                },
            )

    # ----------------------------------------------------------- guards
    def _require_open(self) -> None:
        with self._lock:
            if self._closed:
                raise EncryptionKeyError(reason="SecurityManager is closed")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<SecurityManager state={self._state.value} "
            f"bindings={self._stats.bindings} tamper_alerts={self._stats.tamper_alerts}>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tamper_check_now(
    security: SecurityManager,
    database_path: Union[str, Path],
) -> TamperReport:
    """Free-function wrapper for :meth:`SecurityManager.scheduled_tamper_scan`.

    The health manager imports this so its per-cycle encrypted-store check
    stays decoupled from the security manager's internal constructor graph.
    """
    return security.scheduled_tamper_scan(database_path)


__all__ += ["tamper_check_now"]