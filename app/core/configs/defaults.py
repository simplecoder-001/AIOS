# app/core/configs/defaults.py
"""
Hard-coded default configuration values.

These defaults form the *base layer* of the configuration merge chain:

    defaults (this file)  ->  YAML files  ->  environment overrides  ->  runtime

They exist so the application can always reach a known-good, fail-secure state
even when a YAML config file is absent or malformed. Values here should be
conservative and safe rather than performance-optimal.

Dependency order
----------------
Depends only on ``environment.py``. Contains no I/O.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

from app.core.configs.environment import AppEnvironment, get_environment

__all__ = ["DEFAULT_CONFIG", "get_default_config", "default_for"]


# --------------------------------------------------------------------------- #
# Base defaults (environment-independent)
# --------------------------------------------------------------------------- #
# NOTE: Keys mirror the YAML files in this package. Anything a YAML file omits
# will fall back to the value declared here.

_BASE_DEFAULTS: Final[dict[str, Any]] = {
    # ---- app_config.yaml ----------------------------------------------------
    "app": {
        "name": "AIOS",
        "display_name": "Personal AI Operating System",
        "version": "0.1.0",
        "environment": "production",          # overridden by resolved env
        "debug": False,
        "high_load_mode": False,              # FG3 monitoring suppression
        "shutdown_grace_seconds": 10,
        "startup_timeout_seconds": 60,
        "hardware": {
            "cpu_target": "Ryzen 7",
            "gpu_target": "RTX 5050",
            "gpu_enabled": True,
            "max_ram_gb": 3.0,                # FG1 RAM target
        },
    },

    # ---- core runtime -------------------------------------------------------
    "core": {
        "event_bus": {
            "max_queue_size": 10000,
            "dispatch_workers": 4,
            "enable_event_store": True,
        },
        "state_manager": {
            "persist_state": True,
            "snapshot_interval_seconds": 30,
        },
        "database": {
            "sqlite_busy_timeout_ms": 5000,
            "enable_wal": True,               # FG6 recovery (SQLite WAL)
            "backup_on_startup": True,
        },
        "cache": {
            "default_ttl_seconds": 3600,
            "search_cache_ttl_days": 3,       # FG2 temporary search cache
            "max_memory_items": 5000,
        },
        "queues": {
            "default_maxsize": 1000,
            "priority_levels": 4,             # FG3: Critical/High/Normal/Low
        },
    },

    # ---- logging.yaml (minimal safe fallback) -------------------------------
    "logging": {
        "level": "INFO",
        "console": True,
        "file": True,
        "json_format": False,
        "rotation_mb": 20,
        "retention_days": 14,
    },

    # ---- feature_flags.yaml -------------------------------------------------
    "feature_flags": {
        "fg1_voice": True,
        "fg2_brain": True,
        "fg3_windows_control": True,
        "fg4_language": True,
        "fg5_gui": True,
        "fg6_security": True,
        "fg7_plugins": False,                 # off by default (untrusted code)
        "fg8_productivity": True,
        "fg9_agents": False,                  # off by default (autonomy)
        "fg10_self_learning": False,          # off by default (self-modifying)
        "online_search": True,
        "cloud_llm": True,
    },

    # ---- model_registry.yaml (safe offline-first subset) --------------------
    "model_registry": {
        "intent_classifier": "all-MiniLM-L6-v2",
        "local_llm": "gemma-4-e2b",
        "cloud_llm": "groq",
        "embedding": "multilingual-e5-small",
        "asr_primary": "whisper-small",
        "translation": "marianmt-int8",
        "tts_primary": "kokoro",
        "tts_fallback": "piper",
        "prefer_local": True,                 # offline-first default
    },

    # ---- permissions.yaml ---------------------------------------------------
    "permissions": {
        "default_role": "guest",              # fail-secure: least privilege
        "roles": ["guest", "user", "admin", "super_admin", "system"],
        "require_auth": True,
        "continuous_verification": True,
    },

    # ---- language_policy.yaml -----------------------------------------------
    "language_policy": {
        "default_language": "en",
        "mode": "smart",
        "supported": ["en", "hi", "hinglish", "or"],
        "preserve_technical_vocabulary": True,
        "avoid_unnecessary_switching": True,
    },

    # ---- paths.yaml (relative overrides; absolute resolved by paths.py) -----
    "paths": {
        # Empty by default: paths.py owns canonical resolution. This section
        # only exists for optional user overrides of specific subdirectories.
    },
}


# --------------------------------------------------------------------------- #
# Environment-conditional overlays
# --------------------------------------------------------------------------- #
# Applied on top of _BASE_DEFAULTS depending on the active environment.

_ENV_OVERLAYS: Final[dict[AppEnvironment, dict[str, Any]]] = {
    AppEnvironment.DEVELOPMENT: {
        "app": {"debug": True},
        "logging": {"level": "DEBUG", "json_format": False},
    },
    AppEnvironment.TESTING: {
        "app": {"debug": True},
        "logging": {"level": "WARNING", "file": False, "console": False},
        "core": {"database": {"backup_on_startup": False}},
        "feature_flags": {"cloud_llm": False, "online_search": False},
    },
    AppEnvironment.STAGING: {
        "logging": {"level": "INFO", "json_format": True},
    },
    AppEnvironment.PRODUCTION: {
        "app": {"debug": False},
        "logging": {"level": "INFO", "json_format": True},
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into a copy of ``base`` (overlay wins).

    Nested dicts are merged key-by-key; every other type (including lists) is
    replaced wholesale by the overlay value.
    """
    result = deepcopy(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = _deep_merge(base_value, overlay_value)
        else:
            result[key] = deepcopy(overlay_value)
    return result


def get_default_config(environment: AppEnvironment | None = None) -> dict[str, Any]:
    """Return a fresh, deep-copied default config for the given environment.

    The active environment string is also written into ``app.environment`` so
    downstream consumers can read it uniformly from the config tree.
    """
    env = environment or get_environment()
    merged = _deep_merge(_BASE_DEFAULTS, _ENV_OVERLAYS.get(env, {}))
    # Stamp the resolved environment into the tree.
    merged.setdefault("app", {})["environment"] = env.value
    return merged


def default_for(section: str, environment: AppEnvironment | None = None) -> dict[str, Any]:
    """Return the default sub-tree for a single top-level ``section``.

    Returns an empty dict if the section has no declared defaults.
    """
    return deepcopy(get_default_config(environment).get(section, {}))


# Convenience module-level snapshot for the *current* environment. Consumers
# that need environment-specific behavior should call get_default_config()
# explicitly rather than relying on import-time evaluation.
DEFAULT_CONFIG: Final[dict[str, Any]] = get_default_config()
