# app/core/configs/__init__.py
"""
AIOS core configuration package.

This package is the authoritative configuration subsystem, initialized during
Phase 0 before any feature group starts. It provides:

* Environment detection and typed env-var access (``environment``).
* Canonical filesystem path resolution (``paths``).
* Hard-coded fail-secure defaults (``defaults``).
* YAML loading + layered merging (``config_loader``).
* Schema validation (``config_validator``).
* A thread-safe manager facade (``config_manager``).

Typical usage
-------------
Bootstrap once, early in application startup::

    from app.core.configs import initialize_configuration

    manager = initialize_configuration()          # Phase 0
    level = manager.get("logging.level", "INFO")

Elsewhere, read config through the shared singleton::

    from app.core.configs import get_config

    if get_config("feature_flags.fg2_brain", False):
        ...
"""

from __future__ import annotations

from typing import Optional

from app.core.configs.config_loader import (
    ConfigLoadError,
    load_merged_config,
    load_yaml_file,
)
from app.core.configs.config_manager import (
    ConfigError,
    ConfigManager,
    get_config,
    get_config_manager,
)
from app.core.configs.config_validator import (
    PYDANTIC_AVAILABLE,
    ConfigValidationError,
    validate_config,
)
from app.core.configs.defaults import get_default_config
from app.core.configs.environment import (
    AppEnvironment,
    EnvVarError,
    get_environment,
    is_development,
    is_production,
    is_testing,
    load_dotenv_if_present,
)
from app.core.configs.paths import ProjectPaths, get_paths, resolve_project_root

__all__ = [
    # Environment
    "AppEnvironment",
    "EnvVarError",
    "get_environment",
    "is_development",
    "is_production",
    "is_testing",
    "load_dotenv_if_present",
    # Paths
    "ProjectPaths",
    "get_paths",
    "resolve_project_root",
    # Defaults
    "get_default_config",
    # Loading
    "ConfigLoadError",
    "load_merged_config",
    "load_yaml_file",
    # Validation
    "ConfigValidationError",
    "PYDANTIC_AVAILABLE",
    "validate_config",
    # Manager
    "ConfigError",
    "ConfigManager",
    "get_config",
    "get_config_manager",
    # Bootstrap helper
    "initialize_configuration",
]


def initialize_configuration(
    *,
    explicit_root: Optional[str] = None,
    environment: Optional[AppEnvironment] = None,
    load_dotenv: bool = True,
    create_directories: bool = True,
    strict: Optional[bool] = None,
) -> ConfigManager:
    """Initialize the entire configuration subsystem (Phase 0 entry point).

    Performs the full ordered startup sequence:

        1. Optionally load a ``.env`` file into the environment.
        2. Resolve project paths (root + writable tree).
        3. Bootstrap the ConfigManager (load + validate + cache), optionally
           creating the writable directory tree.

    Parameters
    ----------
    explicit_root:
        Optional explicit project root (mainly for tests/embedding).
    environment:
        Force a specific environment; otherwise resolved from ``AIOS_ENV``.
    load_dotenv:
        Whether to attempt loading a ``.env`` file (no-op if unavailable).
    create_directories:
        Whether to create the writable ``data/`` and ``logs/`` trees.
    strict:
        If True, missing YAML files raise instead of falling back to defaults.

    Returns
    -------
    ConfigManager
        The bootstrapped, process-wide configuration manager singleton.
    """
    if load_dotenv:
        load_dotenv_if_present()

    from pathlib import Path

    paths = get_paths(
        explicit_root=Path(explicit_root) if explicit_root else None,
        refresh=explicit_root is not None,
    )

    manager = get_config_manager()
    return manager.bootstrap(
        paths=paths,
        environment=environment,
        create_directories=create_directories,
        strict=strict,
    )
