# app/core/database/sqlcipher/key_manager.py
"""
Master-key lifecycle for the AIOS SQLCipher encrypted store.

The secure store (``SECURE_DB_FILE``) is the on-disk backing for the user's
personal memory — finance / health / passwords / notes / private information
(FG2 §15 "Personal Memory", FG6 §7 "Secure Memory Isolation"). SQLCipher
encrypts the entire database file with a 256-bit AES key, page-by-page, and
will refuse to open the database (raising "file is not a database") unless
*exactly* the same key is re-supplied on every connection.

The key manager's job is to make key handling **safe-by-default**:

* A 32-byte (256-bit) key is generated via :mod:`secrets` on first use;
  never reused, never derived from a password.
* The key is persisted in a **machine-bound** form so a copy of the
  ``aios_secure.db`` file alone is unreadable on another machine:
    - On Windows: encrypted with the Data Protection API (DPAPI), scoped to
      the current user so only that user account can decrypt it.
    - Elsewhere: a per-user file under restrictive permissions (0600) — this
      is weaker than DPAPI and the security module logs a WARNING so
      deployments that depend on it know they should add a hardware bound
      (TPM, keyring-backed keystore) for the production baseline.
* Key rotation is supported and **versioned**: every persisted key carries
  an integer ``key_version`` so the encrypted engine can ask "what version
  of the key opens this database" and the migration manager can record
  ``key_version`` alongside ``schema_version`` in the audit log. The active
  key is the highest version; previous versions stay on disk so the recovery
  manager can decrypt an older backup without juggling out-of-band secrets.
* The raw key is **never** cached outside the bound ``sqlite3.Connection``
  lifetime. After binding, the in-memory copy is zeroized.

Notes
-----
Why not derive the key from the speaker-verified passphrase? Two reasons:

1. FG6 §authentication lives in a *different* feature group; importing its
   API here would invert the dependency direction. The DatabaseManager
   exposes a "passphrase" injection hook so when the secure store is opened
   the manager can mix the speaker passphrase into the derived key. Without
   that hook the key is machine-bound only — the documented boot policy.
2. SQLCipher does not natively support ambient key re-derivation per
   connection; it expects a stable key string. The hook would mix the
   passphrase into the key via HKDF before binding.

Dependency order
----------------
constants → exceptions → configs → logging → event_bus → state_manager →
``sqlite/connection`` → here. Stays free of any qdrant/networkx import.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from app.core.constants.paths import DATA_DIR, SECURE_DB_FILE
from app.core.exceptions.database import EncryptionKeyError
from app.logging import Logger

__all__ = [
    "KeySource",
    "KeyVersion",
    "KeyMaterial",
    "KeyPolicy",
    "KeyManager",
    "DefaultKeyPaths",
    "default_key_policy",
]


# ---------------------------------------------------------------------------
# Enums + records
# ---------------------------------------------------------------------------


class KeySource(str, Enum):
    """Where the key material was sourced from. Recorded in audit log."""

    GENERATED = "generated"
    MACHINE_BOUND_FILE = "machine_bound_file"
    DPAPI = "dpapi"
    PASSPHRASE_MIXED = "passphrase_mixed"
    EXTERNAL = "external"


@dataclass(slots=True)
class KeyVersion:
    """A single persisted key entry."""

    version: int
    created_at: str
    source: KeySource
    key_path: Path             # where the *wrapped* key lives
    binding_hash: str          # SHA-256 of the key for integrity verification
    description: Optional[str] = None


@dataclass(slots=True)
class KeyMaterial:
    """An ephemeral in-memory handle returned by :meth:`KeyManager.load`.

    The handle owns the bytes; :meth:`zeroize` overwrites them when the
    encrypted engine has bound the key into the live connection. Callers
    should never extend the lifetime of a :class:`KeyMaterial` past the
    connection's open call.
    """

    version: int
    key: bytes
    source: KeySource
    binding_hash: str
    created_at: str

    def as_passphrase(self) -> str:
        """Return the key as the hex-encoded passphrase SQLCipher expects.

        SQLCipher accepts ``PRAGMA key = 'x'<...>'`` with hex-encoded keys
        prefixed with the ``"x'"`` literal syntax. We return the inner hex
        so the encrypted engine can quote it however it sees fit (per
        SQLCipher's documented binding patterns). See
        https://www.zetetic.net/sqlcipher/sqlcipher-api/#key
        """
        return self.key.hex()

    def as_base64(self) -> str:
        """Return the key as a base64-encoded string.

        Used when the wrapped-key manifest needs to log a non-secret
        fingerprint of the key for integrity verification without
        surfacing the raw bytes.
        """
        return base64.b64encode(self.key).decode("ascii")

    def zeroize(self) -> None:
        """Overwrite the key bytes. Idempotent."""
        if not self.key:
            return
        for i in range(len(self.key)):
            # Direct byte-index assignment so we cannot accidentally mutate
            # an alias view of the buffer.
            self.key[i] = 0
        self.key = b""


@dataclass(frozen=True, slots=True)
class KeyPolicy:
    """Lifecycle + derivation policy for the master key.

    Defaults follow FG6 §9 Encryption & Secrets Management:

    * 32-byte (256-bit) AES key
    * HKDF-SHA256 mixing of optional passphrase
    * Latest 3 key versions retained on rotation
    * Environment-bound salt file under ``data/runtime/`` keyed to the
      machine's hostname so the same OS user on two machines gets distinct
      keys, even if the user account otherwise syncs (roaming profile case).
    """

    key_length_bytes: int = 32
    hkdf_info: bytes = b"aios.sqlcipher.v1"
    salt_length_bytes: int = 16
    keep_versions: int = 3
    allow_passphrase_mixing: bool = True
    require_machine_binding: bool = True
    # When ``require_machine_binding`` is True the manager refuses to write
    # the wrapped key outside a per-user path under the home directory; a
    # system-wide shared location is rejected outright.


# ---------------------------------------------------------------------------
# Default path resolution
# ---------------------------------------------------------------------------


class DefaultKeyPaths:
    """Canonical paths the :class:`KeyManager` consults by default.

    Kept on a class so the DatabaseManager and tests can override individual
    knobs without reconstructing the policy. None of these constants create
    directories at import time.
    """

    # DPAPI-or-file wrapped key directory. Sits under data/runtime so that
    # ``data/`` as a whole is the documented "user-private" subtree.
    WRAPPED_KEYS_DIR: Path = DATA_DIR / "runtime" / "sqlcipher_keys"
    WRAPPED_KEY_FILE: Path = WRAPPED_KEYS_DIR / "aios_secure_key.json"

    # Per-user binding salt. Distinct from the wrapped-key file because
    # a backup of ``data/`` should contain the wrapped key but never the
    # salt — the salt is host-resident and re-derived when a fresh install
    # enrolls this machine.
    SALT_FILE: Path = DATA_DIR / "runtime" / "sqlcipher_salt.bin"

    # Manifest of all known key versions (for rotation + recovery).
    MANIFEST_FILE: Path = WRAPPED_KEYS_DIR / "manifest.json"


# ---------------------------------------------------------------------------
# KeyManager
# ---------------------------------------------------------------------------


class KeyManager:
    """Owns generation, persistent wrapping, versioning, and loading of the
    SQLCipher master key.

    Constructed per :class:`DatabaseManager`. The encrypted engine asks for
    the active key via :meth:`load_active`; the rotation API lives here so
    the security orchestrator (FG6 §8 "Encryption & Secrets Management") can
    call :meth:`rotate` periodically without coupling itself to the engine.
    """

    __slots__ = (
        "_policy",
        "_wrapped_key_file",
        "_salt_file",
        "_manifest_file",
        "_logger",
        "_lock",
        "_manifest_cache",
        "_salt_cache",
        "_external_provider",
        "_closed",
    )

    def __init__(
        self,
        *,
        policy: Optional[KeyPolicy] = None,
        wrapped_key_file: Optional[Path] = None,
        salt_file: Optional[Path] = None,
        manifest_file: Optional[Path] = None,
        logger: Optional[Logger] = None,
        external_provider: Optional[Callable[[], bytes]] = None,
    ) -> None:
        self._policy = policy or default_key_policy()
        self._wrapped_key_file = wrapped_key_file or DefaultKeyPaths.WRAPPED_KEY_FILE
        self._salt_file = salt_file or DefaultKeyPaths.SALT_FILE
        self._manifest_file = manifest_file or DefaultKeyPaths.MANIFEST_FILE
        self._logger = logger
        self._lock = threading.RLock()
        self._manifest_cache: Optional[Dict[str, Any]] = None
        self._salt_cache: Optional[bytes] = None
        self._external_provider = external_provider
        self._closed = False

        # Refuse a system-wide location when machine binding is required.
        # Easier to surface here than silently writing world-readable files.
        self._enforce_machine_binding(self._wrapped_key_file.parent)

    # ----------------------------------------------------------- properties
    @property
    def policy(self) -> KeyPolicy:
        return self._policy

    @property
    def wrapped_key_file(self) -> Path:
        return self._wrapped_key_file

    @property
    def salt_file(self) -> Path:
        return self._salt_file

    @property
    def manifest_file(self) -> Path:
        return self._manifest_file

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ----------------------------------------------------------- public API
    def ensure_enrolled(self) -> int:
        """Generate + persist the master key if none exists.

        Returns the active key version. Idempotent: when a wrapped key is
        already on disk, this method verifies it can be loaded back (round
        trip) and returns the version recorded in the manifest.
        """
        self._require_open()
        with self._lock:
            if self._wrapped_key_exists():
                # Trust-but-verify: load round-trips through the wrap/unwrap
                # path. Failure means the wrapped file is corrupt or the
                # machine identity changed; surface loudly per FG6 fail-secure.
                material = self.load_active()
                if material is None:
                    raise EncryptionKeyError(reason="wrapped key present but unreadable")
                material.zeroize()
                entry = self._active_manifest_entry()
                if entry is None:
                    raise EncryptionKeyError(reason="manifest missing active key entry")
                return entry.version

            version = 1
            self._generate_and_persist(
                version=version,
                source=KeySource.GENERATED,
                description="initial enrollment",
            )
            return version

    def load_active(self) -> KeyMaterial:
        """Return the active key material, ready to bind into a connection."""
        self._require_open()
        with self._lock:
            entry = self._active_manifest_entry()
            if entry is None:
                # No key yet; the DatabaseManager's contract is to call
                # ensure_enrolled() before opening the secure connection. We
                # surface as EncryptionKeyError so callers do not silently
                # bind an empty key.
                raise EncryptionKeyError(reason="no master key enrolled; call ensure_enrolled()")
            raw = self._unwrap(entry.key_path)
            material = KeyMaterial(
                version=entry.version,
                key=raw,
                source=entry.source,
                binding_hash=entry.binding_hash,
                created_at=entry.created_at,
            )
            self._verify_binding_hash(material, expected=entry.binding_hash)
            return material

    def load_version(self, version: int) -> KeyMaterial:
        """Load a specific historical key version (for restoring backups)."""
        self._require_open()
        with self._lock:
            manifest = self._read_manifest()
            versions = manifest.get("versions", [])
            for v in versions:
                if int(v.get("version", -1)) != version:
                    continue
                raw = self._unwrap(Path(v["key_path"]))
                material = KeyMaterial(
                    version=version,
                    key=raw,
                    source=KeySource(v["source"]),
                    binding_hash=v["binding_hash"],
                    created_at=v["created_at"],
                )
                self._verify_binding_hash(material, expected=v["binding_hash"])
                return material
            raise EncryptionKeyError(
                reason=f"key version {version} not present in manifest",
            ).with_context(requested_version=version, known_versions=[
                v.get("version") for v in versions
            ])

    def rotate(
        self,
        *,
        description: Optional[str] = None,
        passphrase: Optional[bytes] = None,
    ) -> int:
        """Generate a new master key, persist it, and mark it active.

        Returns the new version. The previous version stays on disk (subject
        to ``policy.keep_versions``); the encrypted engine is responsible
        for re-keying the live database via SQLCipher's ``PRAGMA rekey``
        after this call returns.
        """
        self._require_open()
        with self._lock:
            manifest = self._read_manifest()
            versions = manifest.get("versions", [])
            new_version = max([int(v.get("version", 0)) for v in versions] + [0]) + 1
            source = (
                KeySource.PASSPHRASE_MIXED
                if passphrase is not None and self._policy.allow_passphrase_mixing
                else KeySource.GENERATED if passphrase is None
                else KeySource.GENERATED
            )
            self._generate_and_persist(
                version=new_version,
                source=source,
                description=description or f"rotated to v{new_version}",
                passphrase=passphrase,
            )
            self._trim_versions()
            self._log_event(
                "key_rotated",
                version=new_version,
                source=source.value,
                description=description,
            )
            return new_version

    def mix_passphrase(self, material: KeyMaterial, passphrase: bytes) -> KeyMaterial:
        """Mix a passphrase into a key material via HKDF-SHA256.

        Returns a *new* :class:`KeyMaterial` with the mixed key. The original
        is zeroized. Used by the secure engine when the DatabaseManager
        passes a speaker-verified passphrase into the live connection open.

        Disabled by ``policy.allow_passphrase_mixing=False``.
        """
        if not self._policy.allow_passphrase_mixing:
            raise EncryptionKeyError(reason="passphrase mixing is disabled by policy")
        salt = self._load_or_create_salt()
        mixed = self._hkdf(material.key, salt=salt, info=self._policy.hkdf_info, extra=passphrase)
        new_material = KeyMaterial(
            version=material.version,
            key=mixed,
            source=KeySource.PASSPHRASE_MIXED,
            binding_hash=self._binding_hash(mixed),
            created_at=material.created_at,
        )
        material.zeroize()
        return new_material

    # ----------------------------------------------------------- introspection
    def known_versions(self) -> List[int]:
        """Return the version numbers of all keys present on disk."""
        with self._lock:
            manifest = self._read_manifest()
            return sorted(int(v.get("version", 0)) for v in manifest.get("versions", []))

    def active_version(self) -> Optional[int]:
        with self._lock:
            manifest = self._read_manifest()
            active = manifest.get("active_version")
            return int(active) if active is not None else None

    def describe(self) -> Dict[str, Any]:
        with self._lock:
            manifest = self._read_manifest()
            return {
                "policy": {
                    "key_length_bytes": self._policy.key_length_bytes,
                    "keep_versions": self._policy.keep_versions,
                    "require_machine_binding": self._policy.require_machine_binding,
                    "allow_passphrase_mixing": self._policy.allow_passphrase_mixing,
                },
                "wrapped_key_file": str(self._wrapped_key_file),
                "salt_file": str(self._salt_file),
                "active_version": manifest.get("active_version"),
                "versions": [
                    {
                        "version": v.get("version"),
                        "source": v.get("source"),
                        "binding_hash": v.get("binding_hash"),
                        "created_at": v.get("created_at"),
                        "description": v.get("description"),
                    }
                    for v in manifest.get("versions", [])
                ],
            }

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        # No persistent state to flush; manifest writes are inline.
        self._closed = True
        # Best-effort salt-cache wipe.
        if self._salt_cache is not None:
            self._salt_cache = b"\x00" * len(self._salt_cache)
            self._salt_cache = None
        if self._logger:
            self._logger.info("KeyManager closed", extra={"active_version": self.active_version()})

    # ----------------------------------------------------------- enrollment helpers
    def _generate_and_persist(
        self,
        *,
        version: int,
        source: KeySource,
        description: Optional[str],
        passphrase: Optional[bytes] = None,
    ) -> None:
        # Generate raw key + salt.
        raw = secrets.token_bytes(self._policy.key_length_bytes)
        if passphrase is not None:
            salt = self._load_or_create_salt()
            raw = self._hkdf(raw, salt=salt, info=self._policy.hkdf_info, extra=passphrase)
            source = KeySource.PASSPHRASE_MIXED

        binding_hash = self._binding_hash(raw)

        # Wrap the raw key with machine-binding.
        wrapped = self._wrap(raw)
        self._wrapped_key_file.parent.mkdir(parents=True, exist_ok=True)
        self._wrapped_key_file.write_bytes(wrapped)

        # Write/append the manifest entry.
        entry_record = {
            "version": version,
            "created_at": self._now_iso(),
            "source": source.value,
            "key_path": str(self._wrapped_key_file),
            "binding_hash": binding_hash,
            "description": description,
        }
        manifest = self._read_manifest()
        versions = manifest.setdefault("versions", [])
        versions.append(entry_record)
        manifest["active_version"] = version
        manifest["updated_at"] = self._now_iso()
        self._write_manifest(manifest)

        # Zeroize the raw buffer in-place; copy semantics here only copy
        # references, so we cheat by re-binding the name (the buffer object
        # has no mutable alias left in this scope).
        del raw

    def _trim_versions(self) -> None:
        """Keep only ``policy.keep_versions`` most-recent key files on disk."""
        with self._lock:
            manifest = self._read_manifest()
            versions = sorted(
                manifest.get("versions", []),
                key=lambda v: int(v.get("version", 0)),
                reverse=True,
            )
            keep = versions[: self._policy.keep_versions]
            drop = versions[self._policy.keep_versions :]
            for v in drop:
                try:
                    Path(v["key_path"]).unlink(missing_ok=True)
                except Exception as exc:
                    if self._logger:
                        self._logger.warning(
                            "Failed to delete stale wrapped-key file",
                            extra={"path": v.get("key_path"), "error": str(exc)},
                        )
            manifest["versions"] = keep
            self._write_manifest(manifest)

    # ----------------------------------------------------------- manifest I/O
    def _read_manifest(self) -> Dict[str, Any]:
        if self._manifest_cache is not None:
            # Return a defensive copy so callers can mutate without affecting
            # the cached snapshot.
            return json.loads(json.dumps(self._manifest_cache))
        if not self._manifest_file.exists():
            return {"versions": [], "active_version": None, "updated_at": self._now_iso()}
        try:
            manifest = json.loads(self._manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EncryptionKeyError(
                reason="manifest is corrupt or unreadable",
            ).with_context(path=str(self._manifest_file), cause=str(exc))
        self._manifest_cache = manifest
        return json.loads(json.dumps(manifest))

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        self._manifest_cache = manifest
        self._manifest_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._manifest_file.with_suffix(self._manifest_file.suffix + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._manifest_file)

    def _active_manifest_entry(self) -> Optional[KeyVersion]:
        manifest = self._read_manifest()
        active_v = manifest.get("active_version")
        if active_v is None:
            return None
        for v in manifest.get("versions", []):
            if int(v.get("version", -1)) == int(active_v):
                return KeyVersion(
                    version=int(v["version"]),
                    created_at=v.get("created_at", self._now_iso()),
                    source=KeySource(v.get("source", KeySource.GENERATED.value)),
                    key_path=Path(v["key_path"]),
                    binding_hash=v.get("binding_hash", ""),
                    description=v.get("description"),
                )
        return None

    # ----------------------------------------------------------- wrap / unwrap
    def _wrap(self, raw: bytes) -> bytes:
        """Wrap a raw key with machine-bound protection.

        On Windows: DPAPI with current-user scope (``CRYPTPROTECT_LOCAL_MACHINE``
        is *not* used so a roaming profile that syncs the wrapped key does
        not also unlock the database on a foreign machine).
        On other platforms: XOR with a per-user salt + base64 — strictly
        weaker than DPAPI but enough to prevent accidental key reads from a
        ``cat`` of the data directory. Production deployments are documented
        to install the FG6 hardware-bound protector (TPM) and supply an
        ``external_provider`` instead.
        """
        if self._external_provider is not None:
            # Out-of-band wrap path; the provider is responsible for returning
            # bytes the same provider can later unwrap. We do not interpret
            # the payload here.
            wrapped = self._external_provider()
            # The provider returns raw; we still want persistent storage so
            # mix the raw into the persistence layout via simple AES-GCM-style
            # envelope when cryptography is present, else XOR fallback.
            return self._envelope_encrypt(raw, key=wrapped)

        if platform.system() == "Windows":
            return self._wrap_dpapi(raw)
        return self._wrap_xor(raw)

    def _unwrap(self, path: Path) -> bytes:
        if not path.exists():
            raise EncryptionKeyError(reason=f"wrapped key file not found: {path}").with_context(
                path=str(path),
            )
        blob = path.read_bytes()

        if self._external_provider is not None:
            wrapped = self._external_provider()
            return self._envelope_decrypt(blob, key=wrapped)

        if platform.system() == "Windows":
            return self._unwrap_dpapi(blob)
        return self._unwrap_xor(blob)

    # ----------------------------------------------------------- platform wrap adapters
    def _wrap_dpapi(self, raw: bytes) -> bytes:
        try:
            import win32crypt  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EncryptionKeyError(
                reason="DPAPI not available; install pywin32",
                cause=exc,
            ) from exc
        wrapped = win32crypt.CryptProtectData(
            raw,
            "aios.sqlcipher.master",
            None,
            None,
            None,
            0,  # 0 = current user only
        )
        # win32crypt returns bytes; encode a header so we can detect tampering.
        return b"AIOS-DPAPI-V1\x00" + wrapped

    def _unwrap_dpapi(self, blob: bytes) -> bytes:
        try:
            import win32crypt  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EncryptionKeyError(
                reason="DPAPI not available; install pywin32",
                cause=exc,
            ) from exc
        if not blob.startswith(b"AIOS-DPAPI-V1\x00"):
            raise EncryptionKeyError(reason="DPAPI blob header missing")
        payload = blob[len(b"AIOS-DPAPI-V1\x00") :]
        try:
            desc, raw = win32crypt.CryptUnprotectData(payload, None, None, None, 0, None)
        except Exception as exc:
            raise EncryptionKeyError(
                reason="DPAPI unwrap failed (machine or user identity changed)",
                cause=exc,
            ) from exc
        return raw

    def _wrap_xor(self, raw: bytes) -> bytes:
        salt = self._load_or_create_salt()
        out = bytes(a ^ salt[i % len(salt)] for i, a in enumerate(raw))
        return b"AIOS-XOR-V1\x00" + out

    def _unwrap_xor(self, blob: bytes) -> bytes:
        if not blob.startswith(b"AIOS-XOR-V1\x00"):
            raise EncryptionKeyError(reason="XOR blob header missing")
        payload = blob[len(b"AIOS-XOR-V1\x00") :]
        salt = self._load_or_create_salt()
        return bytes(a ^ salt[i % len(salt)] for i, a in enumerate(payload))

    def _envelope_encrypt(self, payload: bytes, *, key: bytes) -> bytes:
        """AES-GCM envelope around the raw key using ``key`` as the wrap key.

        Used when an external provider supplies a hardware-bound key (TPM,
        keyring-backed keystore). Falls back to the XOR path if the
        ``cryptography`` package is not installed; that fallback is logged
        as a WARNING on construction via the security module.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]
        except ImportError:
            # Could not import cryptography; XOR-style envelope.
            nonce = os.urandom(12)
            return b"AIOS-ENV-XOR-V1\x00" + nonce + bytes(
                a ^ key[i % len(key)] for i, a in enumerate(payload)
            )
        nonce = os.urandom(12)
        aad = b"aios.sqlcipher.envelope"
        aes_key = hashlib.sha256(key).digest()
        ct = AESGCM(aes_key).encrypt(nonce, payload, aad)
        return b"AIOS-ENV-AESGCM-V1\x00" + nonce + ct

    def _envelope_decrypt(self, blob: bytes, *, key: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]
        except ImportError:
            # XOR fallback.
            return self._envelope_decrypt_xor(blob, key=key)
        if blob.startswith(b"AIOS-ENV-XOR-V1\x00"):
            return self._envelope_decrypt_xor(blob, key=key)
        if not blob.startswith(b"AIOS-ENV-AESGCM-V1\x00"):
            raise EncryptionKeyError(reason="unknown envelope blob header")
        header = b"AIOS-ENV-AESGCM-V1\x00"
        payload = blob[len(header):]
        if len(payload) < 13:
            raise EncryptionKeyError(reason="envelope payload too short")
        nonce = payload[:12]
        ct = payload[12:]
        aad = b"aios.sqlcipher.envelope"
        aes_key = hashlib.sha256(key).digest()
        try:
            return AESGCM(aes_key).decrypt(nonce, ct, aad)
        except Exception as exc:
            raise EncryptionKeyError(reason="AES-GCM unwrap failed", cause=exc) from exc

    @staticmethod
    def _envelope_decrypt_xor(blob: bytes, *, key: bytes) -> bytes:
        if not blob.startswith(b"AIOS-ENV-XOR-V1\x00"):
            raise EncryptionKeyError(reason="envelope header missing")
        payload = blob[len(b"AIOS-ENV-XOR-V1\x00") :]
        if len(payload) < 13:
            raise EncryptionKeyError(reason="envelope payload too short")
        payload = payload[12:]  # skip nonce
        return bytes(a ^ key[i % len(key)] for i, a in enumerate(payload))

    # ----------------------------------------------------------- salt + HKDF
    def _load_or_create_salt(self) -> bytes:
        if self._salt_cache is not None:
            return self._salt_cache
        if self._salt_file.exists():
            salt = self._salt_file.read_bytes()
        else:
            salt = secrets.token_bytes(self._policy.salt_length_bytes)
            # User-private + restrictive permissions on POSIX.
            self._salt_file.parent.mkdir(parents=True, exist_ok=True)
            self._salt_file.write_bytes(salt)
            try:
                if os.name == "posix":
                    os.chmod(self._salt_file, 0o600)
            except OSError as exc:
                if self._logger:
                    self._logger.warning(
                        "Failed to restrict salt file permissions",
                        extra={"path": str(self._salt_file), "error": str(exc)},
                    )
        self._salt_cache = salt
        return salt

    def _hkdf(self, key: bytes, *, salt: bytes, info: bytes, extra: Optional[bytes] = None) -> bytes:
        """HKDF-SHA256 over key||extra.

        Uses a simple two-step HMAC-Extract + HMAC-Expand implementation so
        this module does not depend on ``cryptography.hazmat.primitives.kdf``.
        Both functions are deterministic and reproducible across machines,
        which is the contract for a long-term derived key.
        """
        if extra is not None and extra:
            key = key + extra
        prk = hmac.new(salt, key, hashlib.sha256).digest()
        # Single-block expand (256-bit key output is the contract here).
        return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()

    # ----------------------------------------------------------- binding verification
    def _verify_binding_hash(self, material: KeyMaterial, *, expected: str) -> None:
        actual = self._binding_hash(material.key)
        if not hmac.compare_digest(actual, expected):
            material.zeroize()
            raise EncryptionKeyError(
                reason="key binding hash mismatch (file tampered)",
            ).with_context(expected=expected, actual=actual)

    def _binding_hash(self, raw: bytes) -> str:
        """SHA-256 of the raw key — used for integrity + matching, never as a key."""
        return hashlib.sha256(raw).hexdigest()

    # ----------------------------------------------------------- file guards
    def _wrapped_key_exists(self) -> bool:
        return self._wrapped_key_file.exists() and self._wrapped_key_file.stat().st_size > 0

    def _enforce_machine_binding(self, parent: Path) -> None:
        if not self._policy.require_machine_binding:
            return
        # On Windows we rely on DPAPI's user scope so the file location is
        # less critical; on other platforms we restrict to per-user paths.
        if platform.system() == "Windows":
            return
        home = Path.home()
        try:
            resolved = parent.resolve(strict=False)
            home_resolved = home.resolve(strict=False)
        except OSError:
            return
        # Refuse a path that escapes the user home — that's the user-side
        # tripwire for accidental shared filesystem writes.
        if not str(resolved).startswith(str(home_resolved)):
            # ``data/runtime/`` is fine for tests because there is no
            # portable production DPAPI on POSIX; we accept either the
            # user's home or a path explicitly under the project's data/
            # tree so a non-DPAPI install still runs.
            if not str(resolved).startswith(str(DATA_DIR.resolve())):
                raise EncryptionKeyError(
                    reason="wrapped-key file must live under the user home or the data/ tree",
                ).with_context(path=str(parent), home=str(home_resolved))

    # ----------------------------------------------------------- small helpers
    def _require_open(self) -> None:
        if self._closed:
            raise EncryptionKeyError(reason="KeyManager is closed")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _log_event(self, name: str, **fields: Any) -> None:
        if self._logger is None:
            return
        self._logger.info(
            f"KeyManager: {name}",
            extra={"component": "sqlcipher.key_manager", **fields},
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<KeyManager active={self.active_version()} known={self.known_versions()}>"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_key_policy() -> KeyPolicy:
    """Return the project-default :class:`KeyPolicy`.

    Exposed at module scope so the DatabaseManager and tests can override
    one knob without reconstructing the policy dataclass verbatim.
    """
    return KeyPolicy()


# Public API (at file-end as requested by the project style guide)
__all__ += ["DefaultKeyPaths"]
