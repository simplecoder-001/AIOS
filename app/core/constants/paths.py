# app/core/constants/paths.py
"""
Centralized filesystem path constants for AIOS.

Turns the directory layout defined in the project structure into a single,
import-safe source of truth. No other module should hardcode path strings;
they import from here so that relocating a tree requires one change.

Anchoring:
    * PROJECT_ROOT is resolved relative to this file's location
      (app/core/constants/paths.py -> up 4 levels -> repo root), unless the
      environment variable AIOS_ROOT is set, which takes precedence.

Design rules:
    * Uses pathlib.Path; all paths are absolute and resolved.
    * The ONLY side effect permitted is reading env vars — never mkdir at
      import time. Directory creation is an explicit opt-in via ensure_dirs().
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping, Iterable

from app.core.constants.app import APP_SLUG, FeatureGroup


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

_ENV_ROOT_VAR: Final[str] = "AIOS_ROOT"


def _resolve_project_root() -> Path:
    """Resolve the project root.

    Precedence:
        1. AIOS_ROOT environment variable, if set and non-empty.
        2. Four levels up from this file: constants -> core -> app -> root.
    """
    env_root = os.environ.get(_ENV_ROOT_VAR, "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT: Final[Path] = _resolve_project_root()

# Application package root (app/)
APP_DIR: Final[Path] = PROJECT_ROOT / "app"
CORE_DIR: Final[Path] = APP_DIR / "core"
FEATURE_GROUPS_DIR: Final[Path] = APP_DIR / "feature_groups"


# ---------------------------------------------------------------------------
# Top-level trees (from the project folder structure)
# ---------------------------------------------------------------------------

DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
RESOURCES_DIR: Final[Path] = PROJECT_ROOT / "resources"
SCRIPTS_DIR: Final[Path] = PROJECT_ROOT / "scripts"
DOCS_DIR: Final[Path] = PROJECT_ROOT / "docs"
TESTS_DIR: Final[Path] = PROJECT_ROOT / "tests"
LOGS_DIR: Final[Path] = PROJECT_ROOT / "logs"


# ---------------------------------------------------------------------------
# data/ subtree
# ---------------------------------------------------------------------------

DATA_CONFIGS_DIR: Final[Path] = DATA_DIR / "configs"
CACHE_DIR: Final[Path] = DATA_DIR / "cache"
RUNTIME_DIR: Final[Path] = DATA_DIR / "runtime"
TEMP_DIR: Final[Path] = DATA_DIR / "temp"
BACKUPS_DIR: Final[Path] = DATA_DIR / "backups"
EXPERIENCE_DIR: Final[Path] = DATA_DIR / "experience"
LEARNING_DIR: Final[Path] = DATA_DIR / "learning"
BENCHMARK_DIR: Final[Path] = DATA_DIR / "benchmark"
MEMORY_DIR: Final[Path] = DATA_DIR / "memory"
GRAPHS_DIR: Final[Path] = DATA_DIR / "graphs"
EXPERIMENTS_DIR: Final[Path] = DATA_DIR / "experiments"
PATCHES_DIR: Final[Path] = DATA_DIR / "patches"
ROLLBACK_DIR: Final[Path] = DATA_DIR / "rollback"
ANALYTICS_DIR: Final[Path] = DATA_DIR / "analytics"
ARCHIVE_DIR: Final[Path] = DATA_DIR / "archive"

# Recovery folder used by FG3/FG6 for destructive-action rollback.
RECOVERY_DIR: Final[Path] = BACKUPS_DIR / "recovery"


# ---------------------------------------------------------------------------
# resources/ subtree
# ---------------------------------------------------------------------------

AI_MODELS_DIR: Final[Path] = RESOURCES_DIR / "ai_models"
PROMPTS_DIR: Final[Path] = RESOURCES_DIR / "prompts"
TEMPLATES_DIR: Final[Path] = RESOURCES_DIR / "templates"
DICTIONARIES_DIR: Final[Path] = RESOURCES_DIR / "dictionaries"
SCHEMAS_DIR: Final[Path] = RESOURCES_DIR / "schemas"


# ---------------------------------------------------------------------------
# core/configs/ (YAML configuration lives inside the package)
# ---------------------------------------------------------------------------

CONFIG_DIR: Final[Path] = CORE_DIR / "configs"

APP_CONFIG_FILE: Final[Path] = CONFIG_DIR / "app_config.yaml"
LOGGING_CONFIG_FILE: Final[Path] = CONFIG_DIR / "logging.yaml"
ENVIRONMENT_CONFIG_FILE: Final[Path] = CONFIG_DIR / "environment.yaml"
FEATURE_FLAGS_FILE: Final[Path] = CONFIG_DIR / "feature_flags.yaml"
MODEL_REGISTRY_FILE: Final[Path] = CONFIG_DIR / "model_registry.yaml"
PERMISSIONS_FILE: Final[Path] = CONFIG_DIR / "permissions.yaml"
LANGUAGE_POLICY_FILE: Final[Path] = CONFIG_DIR / "language_policy.yaml"
PATHS_CONFIG_FILE: Final[Path] = CONFIG_DIR / "paths.yaml"


# ---------------------------------------------------------------------------
# Databases (SQLite / SQLCipher / Qdrant / knowledge graph)
# ---------------------------------------------------------------------------

METADATA_DB_FILE: Final[Path] = DATA_DIR / "aios_metadata.db"          # SQLite
SECURE_DB_FILE: Final[Path] = DATA_DIR / "aios_secure.db"              # SQLCipher
SEARCH_CACHE_DB_FILE: Final[Path] = CACHE_DIR / "search_cache.db"
SEMANTIC_CACHE_DB_FILE: Final[Path] = CACHE_DIR / "semantic_cache.db"
QDRANT_DIR: Final[Path] = MEMORY_DIR / "qdrant"
KNOWLEDGE_GRAPH_DIR: Final[Path] = GRAPHS_DIR / "knowledge_graph"


# ---------------------------------------------------------------------------
# logs/ subtree (system-wide; feature groups also keep local logs)
# ---------------------------------------------------------------------------

LOG_SYSTEM_DIR: Final[Path] = LOGS_DIR / "system"
LOG_STARTUP_DIR: Final[Path] = LOGS_DIR / "startup"
LOG_EVENTS_DIR: Final[Path] = LOGS_DIR / "events"
LOG_ERRORS_DIR: Final[Path] = LOGS_DIR / "errors"
LOG_AUDIT_DIR: Final[Path] = LOGS_DIR / "audit"
LOG_SECURITY_DIR: Final[Path] = LOGS_DIR / "security"
LOG_PLUGINS_DIR: Final[Path] = LOGS_DIR / "plugins"
LOG_AGENTS_DIR: Final[Path] = LOGS_DIR / "agents"
LOG_CRASHES_DIR: Final[Path] = LOGS_DIR / "crashes"


# ---------------------------------------------------------------------------
# Registry of directories that ensure_dirs() may create
# ---------------------------------------------------------------------------

_MANAGED_DIRS: Final[tuple[Path, ...]] = (
    DATA_DIR,
    DATA_CONFIGS_DIR,
    CACHE_DIR,
    RUNTIME_DIR,
    TEMP_DIR,
    BACKUPS_DIR,
    RECOVERY_DIR,
    EXPERIENCE_DIR,
    LEARNING_DIR,
    BENCHMARK_DIR,
    MEMORY_DIR,
    QDRANT_DIR,
    GRAPHS_DIR,
    KNOWLEDGE_GRAPH_DIR,
    EXPERIMENTS_DIR,
    PATCHES_DIR,
    ROLLBACK_DIR,
    ANALYTICS_DIR,
    ARCHIVE_DIR,
    RESOURCES_DIR,
    AI_MODELS_DIR,
    PROMPTS_DIR,
    TEMPLATES_DIR,
    DICTIONARIES_DIR,
    SCHEMAS_DIR,
    LOGS_DIR,
    LOG_SYSTEM_DIR,
    LOG_STARTUP_DIR,
    LOG_EVENTS_DIR,
    LOG_ERRORS_DIR,
    LOG_AUDIT_DIR,
    LOG_SECURITY_DIR,
    LOG_PLUGINS_DIR,
    LOG_AGENTS_DIR,
    LOG_CRASHES_DIR,
)

# Immutable named view for external inspection.
MANAGED_DIRECTORIES: Final[tuple[Path, ...]] = _MANAGED_DIRS


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def feature_group_dir(fg: FeatureGroup) -> Path:
    """Return the package directory for a feature group."""
    return FEATURE_GROUPS_DIR / fg.value


def feature_group_log_dir(fg: FeatureGroup) -> Path:
    """Return the per-feature-group log directory under logs/."""
    return LOGS_DIR / fg.value


def model_path(*parts: str) -> Path:
    """Resolve a path under resources/ai_models/."""
    return AI_MODELS_DIR.joinpath(*parts)


def prompt_path(name: str) -> Path:
    """Resolve a prompt markdown file under resources/prompts/."""
    return PROMPTS_DIR / name


def dictionary_path(name: str) -> Path:
    """Resolve a dictionary file under resources/dictionaries/."""
    return DICTIONARIES_DIR / name


def config_path(name: str) -> Path:
    """Resolve a YAML config file under core/configs/."""
    return CONFIG_DIR / name


def temp_path(*parts: str) -> Path:
    """Resolve a path under data/temp/."""
    return TEMP_DIR.joinpath(*parts)


def ensure_dirs(extra: Iterable[Path] | None = None) -> None:
    """Create all managed directories (idempotent).

    Called explicitly during bootstrap — never at import time — so that
    importing this module has no filesystem side effects.
    """
    for directory in _MANAGED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
    if extra:
        for directory in extra:
            Path(directory).mkdir(parents=True, exist_ok=True)


# Convenience mapping for diagnostics / startup banner.
ROOT_PATHS: Final[Mapping[str, Path]] = MappingProxyType(
    {
        "project_root": PROJECT_ROOT,
        "app": APP_DIR,
        "data": DATA_DIR,
        "resources": RESOURCES_DIR,
        "logs": LOGS_DIR,
        "config": CONFIG_DIR,
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PROJECT_ROOT",
    "APP_DIR",
    "CORE_DIR",
    "FEATURE_GROUPS_DIR",
    "DATA_DIR",
    "RESOURCES_DIR",
    "SCRIPTS_DIR",
    "DOCS_DIR",
    "TESTS_DIR",
    "LOGS_DIR",
    # data subtree
    "DATA_CONFIGS_DIR",
    "CACHE_DIR",
    "RUNTIME_DIR",
    "TEMP_DIR",
    "BACKUPS_DIR",
    "RECOVERY_DIR",
    "EXPERIENCE_DIR",
    "LEARNING_DIR",
    "BENCHMARK_DIR",
    "MEMORY_DIR",
    "GRAPHS_DIR",
    "EXPERIMENTS_DIR",
    "PATCHES_DIR",
    "ROLLBACK_DIR",
    "ANALYTICS_DIR",
    "ARCHIVE_DIR",
    # resources subtree
    "AI_MODELS_DIR",
    "PROMPTS_DIR",
    "TEMPLATES_DIR",
    "DICTIONARIES_DIR",
    "SCHEMAS_DIR",
    # configs
    "CONFIG_DIR",
    "APP_CONFIG_FILE",
    "LOGGING_CONFIG_FILE",
    "ENVIRONMENT_CONFIG_FILE",
    "FEATURE_FLAGS_FILE",
    "MODEL_REGISTRY_FILE",
    "PERMISSIONS_FILE",
    "LANGUAGE_POLICY_FILE",
    "PATHS_CONFIG_FILE",
    # databases
    "METADATA_DB_FILE",
    "SECURE_DB_FILE",
    "SEARCH_CACHE_DB_FILE",
    "SEMANTIC_CACHE_DB_FILE",
    "QDRANT_DIR",
    "KNOWLEDGE_GRAPH_DIR",
    # logs subtree
    "LOG_SYSTEM_DIR",
    "LOG_STARTUP_DIR",
    "LOG_EVENTS_DIR",
    "LOG_ERRORS_DIR",
    "LOG_AUDIT_DIR",
    "LOG_SECURITY_DIR",
    "LOG_PLUGINS_DIR",
    "LOG_AGENTS_DIR",
    "LOG_CRASHES_DIR",
    # registries / helpers
    "MANAGED_DIRECTORIES",
    "ROOT_PATHS",
    "feature_group_dir",
    "feature_group_log_dir",
    "model_path",
    "prompt_path",
    "dictionary_path",
    "config_path",
    "temp_path",
    "ensure_dirs",
]
