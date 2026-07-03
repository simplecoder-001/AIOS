# app/core/configs/config_manager.py
"""
Central configuration manager (thread-safe singleton facade).

This is the public entry point every subsystem uses to read configuration.
It composes the loader and validator, caches the validated result, and offers
ergonomic, read-mostly access with dotted keys, typed getters, feature-flag
helpers, and change notifications.

Lifecycle
---------
* ``ConfigManager.bootstrap()`` is called once during Phase 0, after paths are
  established, to load + validate config and (optionally) create directories.
* All later reads go through the cached snapshot; ``reload()`` rebuilds it
  atomically and notifies subscribers.

Concurrency
-----------
The multi-threaded voice pipeline (FG1) and the async brain (FG2) read config
concurrently. Reads are lock-free against an immutable snapshot; writes
(bootstrap/reload) swap the snapshot under a lock. Returned values are deep
copies so callers cannot mutate shared state.

Dependency order
----------------
Top of the config package: depends on environment, paths, defaults, loader,
and validator.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any, Callable, Optional

from app.core.configs.config_loader import ConfigLoadError, load_merged_config
from app.core.configs.config_validator import ConfigValidationError, validate_config
from app.core.configs.environment import AppEnvironment, get_environment
from app.core.configs.paths import ProjectPaths, get_paths

__all__ = [
    "ConfigManager",
    "ConfigError",
    "get_config",
    "get_config_manager",
]

# Sentinel distinguishing "key absent" from an explicit ``None`` default.
_MISSING = object()

# Type alias for change subscribers: called with (old_snapshot, new_snapshot).
ChangeListener = Callable[[dict[str, Any], dict[str, Any]], None]


class ConfigError(RuntimeError):
    """Umbrella error for configuration failures surfaced by the manager."""


class ConfigManager:
    """Thread-safe accessor for the validated application configuration."""

    _instance: Optional["ConfigManager"] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        # Instances are normally obtained via ``instance()``. Direct
        # construction is allowed (useful for isolated tests).
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._paths: Optional[ProjectPaths] = None
        self._environment: AppEnvironment = get_environment()
        self._loaded: bool = False
        self._listeners: list[ChangeListener] = []

    # ------------------------------------------------------------------ singleton

    @classmethod
    def instance(cls) -> "ConfigManager":
        """Return the process-wide singleton, constructing it if needed."""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Drop the singleton (test-only hook)."""
        with cls._singleton_lock:
            cls._instance = None

    # ------------------------------------------------------------------ lifecycle

    def bootstrap(
        self,
        paths: Optional[ProjectPaths] = None,
        environment: Optional[AppEnvironment] = None,
        *,
        create_directories: bool = True,
        strict: Optional[bool] = None,
    ) -> "ConfigManager":
        """Load, validate, and cache configuration. Call once in Phase 0.

        Raises
        ------
        ConfigError
            If loading or validation fails.
        """
        with self._lock:
            self._paths = paths or get_paths()
            self._environment = environment or get_environment()

            if create_directories:
                # Guarantee the writable tree exists before anything writes to it.
                self._paths.ensure_directories()

            snapshot = self._build_snapshot(strict=strict)
            old = self._config
            self._config = snapshot
            self._loaded = True
            self._notify(old, snapshot)
            return self

    def reload(self, *, strict: Optional[bool] = None) -> dict[str, Any]:
        """Rebuild the config snapshot atomically and notify subscribers.

        On failure the previous snapshot is preserved (fail-secure) and the
        error is raised to the caller.
        """
        with self._lock:
            if self._paths is None:
                self._paths = get_paths()
            new_snapshot = self._build_snapshot(strict=strict)
            old = self._config
            self._config = new_snapshot
            self._loaded = True
            self._notify(old, new_snapshot)
            return deepcopy(new_snapshot)

    def _build_snapshot(self, *, strict: Optional[bool]) -> dict[str, Any]:
        """Run the load + validate pipeline and return an immutable-ish dict."""
        try:
            raw = load_merged_config(
                paths=self._paths, environment=self._environment, strict=strict
            )
        except ConfigLoadError as exc:
            raise ConfigError(f"Configuration load failed: {exc}") from exc

        try:
            validated = validate_config(raw)
        except ConfigValidationError as exc:
            # Preserve the structured error list for logging/telemetry.
            raise ConfigError(str(exc)) from exc
        return validated

    # ------------------------------------------------------------------ access

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            # Lazy bootstrap keeps early accidental reads working, but Phase 0
            # should call bootstrap() explicitly.
            self.bootstrap()

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Return a value by dotted path (e.g. ``"logging.level"``).

        Returns a deep copy so callers cannot mutate the cached snapshot.
        """
        self._ensure_loaded()
        with self._lock:
            cursor: Any = self._config
            for segment in dotted_key.split("."):
                if isinstance(cursor, dict) and segment in cursor:
                    cursor = cursor[segment]
                else:
                    return deepcopy(default)
            return deepcopy(cursor)

    def require(self, dotted_key: str) -> Any:
        """Like :meth:`get` but raises if the key is absent."""
        value = self.get(dotted_key, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"Required configuration key missing: '{dotted_key}'")
        return value

    def get_str(self, dotted_key: str, default: Optional[str] = None) -> Optional[str]:
        value = self.get(dotted_key, default)
        return None if value is None else str(value)

    def get_int(self, dotted_key: str, default: Optional[int] = None) -> Optional[int]:
        value = self.get(dotted_key, default)
        return None if value is None else int(value)

    def get_float(self, dotted_key: str, default: Optional[float] = None) -> Optional[float]:
        value = self.get(dotted_key, default)
        return None if value is None else float(value)

    def get_bool(self, dotted_key: str, default: Optional[bool] = None) -> Optional[bool]:
        value = self.get(dotted_key, default)
        if isinstance(value, bool) or value is None:
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}

    def get_section(self, section: str) -> dict[str, Any]:
        """Return an entire top-level section as a deep-copied dict."""
        value = self.get(section, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        """Return a deep copy of the entire validated configuration."""
        self._ensure_loaded()
        with self._lock:
            return deepcopy(self._config)

    # ------------------------------------------------------------------ feature flags

    def is_feature_enabled(self, flag: str, default: bool = False) -> bool:
        """Return whether a feature flag under ``feature_flags`` is enabled."""
        self._ensure_loaded()
        with self._lock:
            flags = self._config.get("feature_flags", {})
            value = flags.get(flag, default)
        return bool(value)

    # ------------------------------------------------------------------ metadata

    @property
    def environment(self) -> AppEnvironment:
        return self._environment

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def paths(self) -> ProjectPaths:
        self._ensure_loaded()
        assert self._paths is not None  # populated during bootstrap
        return self._paths

    # ------------------------------------------------------------------ subscriptions

    def subscribe(self, listener: ChangeListener) -> Callable[[], None]:
        """Register a change listener; returns an unsubscribe callable.

        Listeners are invoked (old, new) after every bootstrap/reload. Listener
        exceptions are swallowed to protect the reload path; subscribers are
        responsible for their own error handling.
        """
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsubscribe

    def _notify(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        # Copy the listener list under lock, invoke outside to avoid deadlocks.
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(deepcopy(old), deepcopy(new))
            except Exception:  # noqa: BLE001 - never let a listener break reload
                continue


# --------------------------------------------------------------------------- #
# Module-level convenience accessors
# --------------------------------------------------------------------------- #

def get_config_manager() -> ConfigManager:
    """Return the singleton :class:`ConfigManager`."""
    return ConfigManager.instance()


def get_config(dotted_key: Optional[str] = None, default: Any = None) -> Any:
    """Shortcut: fetch one dotted key, or the whole config if ``key`` is None."""
    manager = ConfigManager.instance()
    if dotted_key is None:
        return manager.as_dict()
    return manager.get(dotted_key, default)
