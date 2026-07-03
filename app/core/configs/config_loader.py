# app/core/configs/config_loader.py
"""
Configuration file loading and layer merging.

Reads the YAML files in ``app/core/configs`` and merges them, on top of the
hard-coded defaults, into a single raw configuration dictionary. Environment
variable overrides and ``${VAR:default}`` interpolation are applied last.

Merge chain (lowest to highest priority)
-----------------------------------------
    1. defaults.get_default_config(env)
    2. app_config.yaml               (top-level app/core settings)
    3. domain YAML files             (logging, feature_flags, model_registry,
                                      permissions, language_policy, paths)
    4. environment.yaml              (per-environment overlay block)
    5. AIOS_CFG__* environment variables (double-underscore path override)

Dependency order
----------------
Depends on ``environment.py``, ``paths.py``, and ``defaults.py``.
Does NOT depend on the validator or manager (kept one-directional).
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Final, Mapping, Optional

from app.core.configs.defaults import get_default_config
from app.core.configs.environment import (
    AppEnvironment,
    get_bool,
    get_environment,
)
from app.core.configs.paths import ProjectPaths, get_paths

__all__ = [
    "ConfigLoadError",
    "load_yaml_file",
    "load_merged_config",
    "ENV_OVERRIDE_PREFIX",
]

# Environment variables beginning with this prefix override config keys.
# Example: AIOS_CFG__logging__level=DEBUG  ->  config["logging"]["level"]="DEBUG"
ENV_OVERRIDE_PREFIX: Final[str] = "AIOS_CFG__"
_ENV_PATH_SEPARATOR: Final[str] = "__"

# Maps each domain YAML file to the top-level config section it populates.
# app_config.yaml is handled specially (merged at the root).
_DOMAIN_FILES: Final[dict[str, str]] = {
    "logging.yaml": "logging",
    "feature_flags.yaml": "feature_flags",
    "model_registry.yaml": "model_registry",
    "permissions.yaml": "permissions",
    "language_policy.yaml": "language_policy",
    "paths.yaml": "paths",
}

# Matches ${VAR} or ${VAR:default} for interpolation inside YAML string values.
_INTERP_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


class ConfigLoadError(RuntimeError):
    """Raised only for unrecoverable load failures (e.g. malformed override)."""


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #

def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into a deep copy of ``base``."""
    result = deepcopy(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, Mapping):
            result[key] = _deep_merge(base_value, dict(overlay_value))
        else:
            result[key] = deepcopy(overlay_value)
    return result


def _interpolate_value(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:default}`` in string values."""
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            var_name, default = match.group(1), match.group(2)
            return os.environ.get(var_name, default if default is not None else match.group(0))

        return _INTERP_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_value(item) for item in value]
    return value


def _coerce_scalar(raw: str) -> Any:
    """Best-effort coercion of an env-override string into a typed scalar."""
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", ""}:
        return None
    try:
        if raw.strip().isdigit() or (raw.strip().startswith("-") and raw.strip()[1:].isdigit()):
            return int(raw.strip())
        return float(raw.strip()) if _looks_like_float(raw) else raw
    except ValueError:
        return raw


def _looks_like_float(raw: str) -> bool:
    try:
        float(raw.strip())
        return "." in raw or "e" in raw.lower()
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# YAML loading
# --------------------------------------------------------------------------- #

def load_yaml_file(path: Path, *, required: bool = False) -> dict[str, Any]:
    """Safely load a single YAML file into a dict.

    Missing files return an empty dict unless ``required`` is True. Parse
    errors are raised as :class:`ConfigLoadError`. An empty document also
    returns an empty dict.
    """
    if not path.exists():
        if required:
            raise ConfigLoadError(f"Required config file not found: {path}")
        return {}

    try:
        import yaml  # PyYAML
    except ImportError as exc:  # pragma: no cover
        raise ConfigLoadError(
            "PyYAML is required to load configuration files but is not installed."
        ) from exc

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"Failed to parse YAML file '{path}': {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigLoadError(
            f"Top-level YAML content in '{path}' must be a mapping, got {type(data).__name__}."
        )
    return data


# --------------------------------------------------------------------------- #
# Environment-variable overrides
# --------------------------------------------------------------------------- #

def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Apply ``AIOS_CFG__section__key=value`` overrides onto the config tree."""
    result = deepcopy(config)
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(ENV_OVERRIDE_PREFIX):
            continue
        path_str = env_key[len(ENV_OVERRIDE_PREFIX):]
        parts = [segment for segment in path_str.split(_ENV_PATH_SEPARATOR) if segment]
        if not parts:
            continue
        cursor: dict[str, Any] = result
        for segment in parts[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                existing = {}
                cursor[segment] = existing
            cursor = existing
        cursor[parts[-1]] = _coerce_scalar(raw_value)
    return result


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def load_merged_config(
    paths: Optional[ProjectPaths] = None,
    environment: Optional[AppEnvironment] = None,
    *,
    strict: Optional[bool] = None,
) -> dict[str, Any]:
    """Load and merge the complete raw configuration.

    Parameters
    ----------
    paths:
        Project paths registry. Resolved via :func:`get_paths` if omitted.
    environment:
        Active environment. Resolved via :func:`get_environment` if omitted.
    strict:
        When True, missing YAML files raise instead of falling back to
        defaults. Defaults to the ``AIOS_CFG_STRICT`` env flag, else False
        (fail-secure boot: prefer defaults over crashing).

    Returns
    -------
    dict
        The fully merged, interpolated, override-applied raw config. This dict
        is NOT yet validated; pass it to ``config_validator.validate_config``.
    """
    resolved_paths = paths or get_paths()
    env = environment or get_environment()
    is_strict = strict if strict is not None else bool(get_bool("AIOS_CFG_STRICT", False))

    # Layer 1: defaults
    config = get_default_config(env)

    # Layer 2: app_config.yaml merged at the root level
    app_config = load_yaml_file(
        resolved_paths.core_config_file("app_config.yaml"), required=is_strict
    )
    if app_config:
        config = _deep_merge(config, app_config)

    # Layer 3: domain files merged into their designated section
    for filename, section in _DOMAIN_FILES.items():
        file_data = load_yaml_file(
            resolved_paths.core_config_file(filename), required=is_strict
        )
        if not file_data:
            continue
        # Allow either a bare section body or a wrapped { section: {...} } doc.
        body = file_data.get(section, file_data)
        config[section] = _deep_merge(config.get(section, {}), body)

    # Layer 4: environment.yaml per-environment overlay
    env_doc = load_yaml_file(
        resolved_paths.core_config_file("environment.yaml"), required=is_strict
    )
    if env_doc:
        # Support both a flat overlay and an {environments: {prod: {...}}} shape.
        overlay = env_doc.get("environments", {}).get(env.value, {}) or env_doc.get(env.value, {})
        if overlay:
            config = _deep_merge(config, overlay)

    # Layer 5: environment-variable interpolation, then explicit overrides
    config = _interpolate_value(config)
    config = _apply_env_overrides(config)

    # Ensure the resolved environment is always authoritative.
    config.setdefault("app", {})["environment"] = env.value
    return config
