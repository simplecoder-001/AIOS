# app/core/database/sqlite/pragmas.py
"""
SQLite PRAGMA presets for AIOS database engines.

SQLite exposes a large set of runtime PRAGMAs that fundamentally alter the
safety, durability, and concurrency characteristics of a connection. Getting
them wrong at the connection level silently degrades the system: a missing
``foreign_keys=ON`` allows orphaned rows, a missing ``WAL`` mode serializes
readers behind writers, and a missing ``busy_timeout`` produces spurious
``database is locked`` errors under the multi-threaded voice pipeline.

This module owns three named, frozen presets so every connection opened by
``connection_manager`` always uses consistent, governed settings. Each preset
is a tuple of ``(name, value)`` pairs that the engine applies in order
immediately after opening a connection.

Presets
-------
* ``PRAGMAS_DEFAULT``    — general metadata / cache databases.
                          Foreign keys enforced, WAL enabled, synchronous=NORMAL.
* ``PRAGMAS_ENCRYPTED``  — SQLCipher / personal-memory store.
                          Adds ``cipher_page_size`` and ``cipher_kdf_iter`` policy
                          hints (commands are a no-op on plain sqlite3).
* ``PRAGMAS_MEMORY``     — in-memory test / scratch databases.
                          Zero durability, maximum throughput.
* ``PRAGMAS_INTEGRITY``  — read-only checks for the health manager.
                          No writes allowed; optimal for ``PRAGMA integrity_check``.

The associated busy-timeout default honours the configured value at
``core.database.sqlite_busy_timeout_ms`` (see app_config.yaml).

Dependency order
----------------
constants → exceptions → configs → logging → here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Iterator, Mapping, Tuple

from app.core.configs.config_manager import ConfigManager
from app.core.constants.settings import SECURITY

__all__ = [
    "Pragma",
    "PragmaPreset",
    "PRAGMAS_DEFAULT",
    "PRAGMAS_ENCRYPTED",
    "PRAGMAS_MEMORY",
    "PRAGMAS_INTEGRITY",
    "DEFAULT_PRESET",
    "resolve_preset",
    "preset_for",
]


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


Pragma = Tuple[str, object]
"""A single ``(name, value)`` PRAGMA pair."""


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PragmaPreset:
    """An ordered, immutable PRAGMA preset.

    Order matters: ``journal_mode`` must be set before ``synchronous`` so that
    SQLite re-evaluates the sync mode against the active journal; ``foreign_keys``
    is set last as a runtime guard.
    """

    name: str
    pragmas: Tuple[Pragma, ...]
    description: str = ""

    def __post_init__(self) -> None:
        # Stable set used by ``__contains__``; tuples preserve order.
        seen = set()
        for pragma_name, _ in self.pragmas:
            if not pragma_name:
                raise ValueError("Pragma name must be non-empty")
            if pragma_name in seen:
                raise ValueError(f"Duplicate pragma {pragma_name!r} in preset {self.name!r}")
            seen.add(pragma_name)

    def __iter__(self) -> Iterator[Pragma]:
        return iter(self.pragmas)

    def __len__(self) -> int:
        return len(self.pragmas)

    def __contains__(self, pragma_name: str) -> bool:
        return any(p[0] == pragma_name for p in self.pragmas)

    def get(self, pragma_name: str, default: object = None) -> object:
        for name, value in self.pragmas:
            if name == pragma_name:
                return value
        return default

    def as_dict(self) -> Mapping[str, object]:
        """Return a mapping view of the preset (order not guaranteed)."""
        return MappingProxyType({name: value for name, value in self.pragmas})

    def statements(self) -> Tuple[str, ...]:
        """Return the preset as ordered ``PRAGMA name = value;`` SQL fragments.

        Strings are quoted; booleans become 0/1; integers/floats are emitted
        verbatim. This is the exact inclusion order applied by the engine.
        """
        return tuple(f"PRAGMA {name} = {_format_value(value)};" for name, value in self.pragmas)


# ---------------------------------------------------------------------------
# Preset builders
# ---------------------------------------------------------------------------


def _default_busy_timeout() -> int:
    """Resolve the busy timeout from the config manager (best-effort).

    Returns the configured ``core.database.sqlite_busy_timeout_ms`` if
    the config manager has been bootstrapped; otherwise a 5-second baseline.
    """
    try:
        return int(
            ConfigManager.instance().get(
                "core.database.sqlite_busy_timeout_ms", 5000
            )
        )
    except Exception:  # noqa: BLE001 — config may not be loaded in unit tests
        return 5000


def _build_default_preset() -> PragmaPreset:
    busy_timeout = _default_busy_timeout()
    return PragmaPreset(
        name="default",
        description="General metadata / cache databases — WAL, foreign keys, NORMAL sync.",
        pragmas=(
            ("journal_mode", "wal"),
            ("wal_autocheckpoint", 1000),
            ("wal_checkpoint", "PASSIVE"),
            ("synchronous", "NORMAL"),
            ("temp_store", "MEMORY"),
            ("cache_size", -65536),            # 64 MiB negative = KiB
            ("mmap_size", 268435456),          # 256 MiB memory-mapped I/O ceiling
            ("foreign_keys", True),
            ("busy_timeout", busy_timeout),
            ("auto_vacuum", "INCREMENTAL"),
            ("encoding", "UTF-8"),
            ("recursive_triggers", True),
            ("defer_foreign_keys", False),
            ("max_page_count", 1_073_741_824),  # 1 TiB safety ceiling
        ),
    )


def _build_encrypted_preset() -> PragmaPreset:
    busy_timeout = _default_busy_timeout()
    return PragmaPreset(
        name="encrypted",
        description="SQLCipher / SQLCipher-compatible personal-memory store.",
        pragmas=(
            # SQLCipher-specific hints; ignored by plain sqlite3 so the same
            # preset remains import-safe even when pysqlcipher3 is absent.
            ("cipher_page_size", 4096),
            ("cipher_kdf_iter", 256_000),
            ("cipher_use_hmac", True),
            ("cipher_default_kdf_iter", 256_000),
            ("cipher_compatibility", 4),
            # WAL + foreign keys still apply.
            ("journal_mode", "wal"),
            ("wal_autocheckpoint", 1000),
            ("synchronous", "FULL"),           # FULL for the encrypted store
            ("temp_store", "MEMORY"),
            ("cache_size", -32768),             # 32 MiB
            ("foreign_keys", True),
            ("busy_timeout", busy_timeout),
            ("recursive_triggers", True),
            ("defer_foreign_keys", False),
        ),
    )


def _build_memory_preset() -> PragmaPreset:
    return PragmaPreset(
        name="memory",
        description="In-memory scratch / test database — zero durability, max throughput.",
        pragmas=(
            ("journal_mode", "memory"),
            ("synchronous", "OFF"),
            ("temp_store", "MEMORY"),
            ("cache_size", -131072),            # 128 MiB
            ("foreign_keys", True),
            ("busy_timeout", 0),               # single-threaded scratch
            ("recursive_triggers", True),
            ("auto_vacuum", "NONE"),
        ),
    )


def _build_integrity_preset() -> PragmaPreset:
    return PragmaPreset(
        name="integrity",
        description="Read-only integrity-check / backup connection.",
        pragmas=(
            ("query_only", True),
            ("defer_foreign_keys", True),
            ("busy_timeout", max(_default_busy_timeout(), 30000)),
            ("cache_size", -16384),
        ),
    )


# ---------------------------------------------------------------------------
# Public constants (built lazily so config bootstrap can run first)
# ---------------------------------------------------------------------------


def _build_all() -> Mapping[str, PragmaPreset]:
    return {
        "default": _build_default_preset(),
        "encrypted": _build_encrypted_preset(),
        "memory": _build_memory_preset(),
        "integrity": _build_integrity_preset(),
    }


_PRAGMAS_CACHE: dict[str, PragmaPreset] = {}

DEFAULT_PRESET: Final[PragmaPreset] = _build_default_preset()


def _ensure_presets() -> dict[str, PragmaPreset]:
    """Return the cached preset dict, building it on first access.

    Cached so that repeated resolution is O(1) — but rebuilt on demand if the
    config manager is later bootstrapped with a different busy timeout.
    """
    if not _PRAGMAS_CACHE:
        _PRAGMAS_CACHE.update(_build_all())
    return _PRAGMAS_CACHE


PRAGMAS_DEFAULT: PragmaPreset = DEFAULT_PRESET
PRAGMAS_ENCRYPTED: PragmaPreset = _build_encrypted_preset()
PRAGMAS_MEMORY: PragmaPreset = _build_memory_preset()
PRAGMAS_INTEGRITY: PragmaPreset = _build_integrity_preset()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def resolve_preset(name: str) -> PragmaPreset:
    """Look up a named :class:`PragmaPreset`.

    Accepted names: ``"default"``, ``"encrypted"``, ``"memory"``,
    ``"integrity"`` (case-insensitive). Raises :class:`KeyError` otherwise —
    the caller (engine) is expected to surface a :class:`DatabaseError`.
    """
    table = _ensure_presets()
    key = name.strip().lower()
    if key not in table:
        raise KeyError(f"Unknown PRAGMA preset: {name!r}")
    return table[key]


def preset_for(encrypted: bool = False, in_memory: bool = False, read_only: bool = False) -> PragmaPreset:
    """Ergonomic resolver mirroring the connection-manager policy.

    Order of precedence: ``read_only`` → ``encrypted`` → ``in_memory`` →
    ``default``. This matches the rules encoded in ``connection_manager.py``
    so callers never have to hand-craft a preset name.
    """
    if read_only:
        return resolve_preset("integrity")
    if encrypted:
        return resolve_preset("encrypted")
    if in_memory:
        return resolve_preset("memory")
    return resolve_preset("default")


def reload_presets() -> None:
    """Rebuild the preset cache after a config reload.

    The busy-timeout config knob is usually loaded once at boot, but tests and
    the dynamic config-reload mechanism may update it; calling this rebuilds the
    cached presets without affecting any already-open connections (which retain
    their published pragmas until they are recycled by the pool).
    """
    global PRAGMAS_DEFAULT, PRAGMAS_ENCRYPTED, PRAGMAS_MEMORY, PRAGMAS_INTEGRITY, DEFAULT_PRESET
    _PRAGMAS_CACHE.clear()
    fresh = _build_all()
    _PRAGMAS_CACHE.update(fresh)
    PRAGMAS_DEFAULT = fresh["default"]
    PRAGMAS_ENCRYPTED = fresh["encrypted"]
    PRAGMAS_MEMORY = fresh["memory"]
    PRAGMAS_INTEGRITY = fresh["integrity"]
    DEFAULT_PRESET = fresh["default"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_value(value: object) -> str:
    """Render a PRAGMA value as a SQL-safe literal fragment."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "NULL"
    text = str(value)
    # PRAGMA values are bare keywords (e.g. WAL, NORMAL) or string literals;
    # the safe default is to quote textual values.
    if text.isupper() or text.islower() and not any(c in text for c in " '\""):
        return text  # looks like a keyword
    return f"'{text.replace(chr(39), chr(39) + chr(39))}'"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ += ["reload_presets"]
