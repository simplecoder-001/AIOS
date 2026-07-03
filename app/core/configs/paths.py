# app/core/configs/paths.py
"""
Filesystem path resolution for the entire AIOS project.

This is the single source of truth for *where things live on disk*. Every
subsystem (voice, brain, security, plugins, logging) should ask this module
for its directories instead of hard-coding relative paths, so the layout can
evolve in one place.

Dependency order
----------------
Depends only on ``environment.py``. Imported during Phase 0 immediately after
environment resolution and before the config loader/manager.

Root resolution strategy (in priority order)
---------------------------------------------
1. ``AIOS_ROOT`` environment variable (explicit override, e.g. for tests).
2. Upward search for a project marker (``pyproject.toml`` / ``main.py``).
3. Fallback: three levels up from this file
   (``app/core/configs/paths.py`` -> project root).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterable, Optional

from app.core.configs.environment import get_path

__all__ = ["ProjectPaths", "get_paths", "resolve_project_root"]

# Files that, when found in a directory, identify it as the project root.
_ROOT_MARKERS: Final[tuple[str, ...]] = ("pyproject.toml", "main.py", "launcher.py")

# This file lives at: <root>/app/core/configs/paths.py  ->  4 parents up == root.
_THIS_FILE_ROOT_DEPTH: Final[int] = 4


def resolve_project_root(explicit: Optional[Path] = None) -> Path:
    """Resolve the absolute project root directory.

    Parameters
    ----------
    explicit:
        An explicit root supplied by the caller (highest priority).

    Returns
    -------
    Path
        Absolute, resolved path to the project root.
    """
    # 1. Explicit argument wins.
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    # 2. Environment override.
    env_root = get_path("AIOS_ROOT")
    if env_root is not None:
        return env_root.resolve()

    # 3. Upward marker search starting from this file's location.
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if candidate.is_dir() and any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate

    # 4. Deterministic fallback based on known file depth.
    try:
        return here.parents[_THIS_FILE_ROOT_DEPTH - 1]
    except IndexError:  # pragma: no cover - only if file is moved shallowly
        return here.parent


@dataclass(frozen=True)
class ProjectPaths:
    """Immutable registry of all canonical project directories and files.

    Constructed once and cached (see :func:`get_paths`). All attributes are
    absolute :class:`~pathlib.Path` objects.
    """

    root: Path

    # --- Top-level directories ------------------------------------------------
    app_dir: Path = field(init=False)
    data_dir: Path = field(init=False)
    resources_dir: Path = field(init=False)
    scripts_dir: Path = field(init=False)
    docs_dir: Path = field(init=False)
    tests_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)

    # --- app/core config location --------------------------------------------
    core_configs_dir: Path = field(init=False)

    # --- data/ subtree --------------------------------------------------------
    data_configs_dir: Path = field(init=False)
    data_cache_dir: Path = field(init=False)
    data_runtime_dir: Path = field(init=False)
    data_temp_dir: Path = field(init=False)
    data_backups_dir: Path = field(init=False)
    data_experience_dir: Path = field(init=False)
    data_learning_dir: Path = field(init=False)
    data_benchmark_dir: Path = field(init=False)
    data_memory_dir: Path = field(init=False)
    data_graphs_dir: Path = field(init=False)
    data_experiments_dir: Path = field(init=False)
    data_patches_dir: Path = field(init=False)
    data_rollback_dir: Path = field(init=False)
    data_analytics_dir: Path = field(init=False)
    data_archive_dir: Path = field(init=False)

    # --- resources/ subtree ---------------------------------------------------
    ai_models_dir: Path = field(init=False)
    prompts_dir: Path = field(init=False)
    templates_dir: Path = field(init=False)
    dictionaries_dir: Path = field(init=False)
    schemas_dir: Path = field(init=False)

    # --- logs/ subtree --------------------------------------------------------
    logs_system_dir: Path = field(init=False)
    logs_startup_dir: Path = field(init=False)
    logs_events_dir: Path = field(init=False)
    logs_errors_dir: Path = field(init=False)
    logs_audit_dir: Path = field(init=False)
    logs_security_dir: Path = field(init=False)
    logs_plugins_dir: Path = field(init=False)
    logs_agents_dir: Path = field(init=False)
    logs_crashes_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        # ``frozen=True`` forbids normal attribute assignment, so we use
        # object.__setattr__ to populate derived paths exactly once.
        root = self.root

        def _set(name: str, path: Path) -> None:
            object.__setattr__(self, name, path)

        # Top-level
        _set("app_dir", root / "app")
        _set("data_dir", root / "data")
        _set("resources_dir", root / "resources")
        _set("scripts_dir", root / "scripts")
        _set("docs_dir", root / "docs")
        _set("tests_dir", root / "tests")
        _set("logs_dir", root / "logs")

        # Core config directory (where this package's YAML files live)
        _set("core_configs_dir", root / "app" / "core" / "configs")

        # data/ subtree
        data = root / "data"
        _set("data_configs_dir", data / "configs")
        _set("data_cache_dir", data / "cache")
        _set("data_runtime_dir", data / "runtime")
        _set("data_temp_dir", data / "temp")
        _set("data_backups_dir", data / "backups")
        _set("data_experience_dir", data / "experience")
        _set("data_learning_dir", data / "learning")
        _set("data_benchmark_dir", data / "benchmark")
        _set("data_memory_dir", data / "memory")
        _set("data_graphs_dir", data / "graphs")
        _set("data_experiments_dir", data / "experiments")
        _set("data_patches_dir", data / "patches")
        _set("data_rollback_dir", data / "rollback")
        _set("data_analytics_dir", data / "analytics")
        _set("data_archive_dir", data / "archive")

        # resources/ subtree
        resources = root / "resources"
        _set("ai_models_dir", resources / "ai_models")
        _set("prompts_dir", resources / "prompts")
        _set("templates_dir", resources / "templates")
        _set("dictionaries_dir", resources / "dictionaries")
        _set("schemas_dir", resources / "schemas")

        # logs/ subtree
        logs = root / "logs"
        _set("logs_system_dir", logs / "system")
        _set("logs_startup_dir", logs / "startup")
        _set("logs_events_dir", logs / "events")
        _set("logs_errors_dir", logs / "errors")
        _set("logs_audit_dir", logs / "audit")
        _set("logs_security_dir", logs / "security")
        _set("logs_plugins_dir", logs / "plugins")
        _set("logs_agents_dir", logs / "agents")
        _set("logs_crashes_dir", logs / "crashes")

    # ------------------------------------------------------------------ helpers

    def core_config_file(self, filename: str) -> Path:
        """Return the absolute path to a YAML file in ``app/core/configs``."""
        return self.core_configs_dir / filename

    def writable_dirs(self) -> tuple[Path, ...]:
        """All directories the application is expected to create/write to.

        Resources are intentionally excluded because they are treated as
        read-only assets shipped with the application.
        """
        return (
            self.data_dir,
            self.data_configs_dir,
            self.data_cache_dir,
            self.data_runtime_dir,
            self.data_temp_dir,
            self.data_backups_dir,
            self.data_experience_dir,
            self.data_learning_dir,
            self.data_benchmark_dir,
            self.data_memory_dir,
            self.data_graphs_dir,
            self.data_experiments_dir,
            self.data_patches_dir,
            self.data_rollback_dir,
            self.data_analytics_dir,
            self.data_archive_dir,
            self.logs_dir,
            self.logs_system_dir,
            self.logs_startup_dir,
            self.logs_events_dir,
            self.logs_errors_dir,
            self.logs_audit_dir,
            self.logs_security_dir,
            self.logs_plugins_dir,
            self.logs_agents_dir,
            self.logs_crashes_dir,
        )

    def ensure_directories(self, extra: Optional[Iterable[Path]] = None) -> None:
        """Create all writable directories (idempotent).

        Called once during Phase 0 bootstrap so downstream components can
        assume their target directories exist.
        """
        targets = list(self.writable_dirs())
        if extra:
            targets.extend(extra)
        for directory in targets:
            directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Cached singleton accessor
# --------------------------------------------------------------------------- #

_paths_lock = threading.Lock()
_paths_instance: Optional[ProjectPaths] = None


def get_paths(explicit_root: Optional[Path] = None, *, refresh: bool = False) -> ProjectPaths:
    """Return the process-wide :class:`ProjectPaths` singleton.

    Thread-safe. Pass ``refresh=True`` (mainly for tests) to rebuild the
    instance, optionally against a different ``explicit_root``.
    """
    global _paths_instance

    if _paths_instance is not None and not refresh and explicit_root is None:
        return _paths_instance

    with _paths_lock:
        if _paths_instance is None or refresh or explicit_root is not None:
            root = resolve_project_root(explicit_root)
            _paths_instance = ProjectPaths(root=root)
        return _paths_instance
