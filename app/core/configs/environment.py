# app/core/configs/environment.py
"""
Environment detection and typed environment-variable access.

This module is the lowest layer of the configuration system. It has ZERO
dependencies on other config modules so it can be imported first during
Phase 0 bootstrap. Everything else (paths, loader, manager) builds on top
of the `Environment` value resolved here.

Responsibilities
----------------
* Detect the active runtime environment (development / testing / staging /
  production) from the `AIOS_ENV` variable, with a safe default.
* Provide typed, validated access to environment variables (str/int/float/
  bool/path/list) with defaults and required-key enforcement.
* Load a `.env` file if `python-dotenv` is available, without making it a
  hard dependency.

Design notes
------------
* Fail-secure: unknown environment strings fall back to PRODUCTION, which is
  the most restrictive mode (aligns with FG6 "Fail Secure" principle).
* Pure standard library except for the optional dotenv loader.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Callable, Final, Optional, Sequence, TypeVar

__all__ = [
    "AppEnvironment",
    "EnvVarError",
    "load_dotenv_if_present",
    "get_environment",
    "get_str",
    "get_int",
    "get_float",
    "get_bool",
    "get_path",
    "get_list",
    "is_production",
    "is_development",
    "is_testing",
]

T = TypeVar("T")

# Canonical name of the variable that selects the environment.
ENV_VAR_NAME: Final[str] = "AIOS_ENV"

# Truthy / falsy tokens accepted for boolean coercion (case-insensitive).
_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on", "enabled"})
_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off", "disabled"})


class EnvVarError(RuntimeError):
    """Raised when a required environment variable is missing or malformed."""


class AppEnvironment(str, Enum):
    """The supported runtime environments.

    Inherits from ``str`` so it serializes cleanly to YAML/JSON and compares
    naturally against raw strings.
    """

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"

    @classmethod
    def from_string(cls, value: Optional[str]) -> "AppEnvironment":
        """Resolve a raw string to an environment, failing secure.

        Accepts common aliases (e.g. ``dev``, ``prod``, ``test``). Any
        unrecognized or empty value resolves to PRODUCTION, the safest mode.
        """
        if not value:
            return cls.PRODUCTION

        normalized = value.strip().lower()
        aliases = {
            "dev": cls.DEVELOPMENT,
            "develop": cls.DEVELOPMENT,
            "development": cls.DEVELOPMENT,
            "test": cls.TESTING,
            "testing": cls.TESTING,
            "ci": cls.TESTING,
            "stage": cls.STAGING,
            "staging": cls.STAGING,
            "prod": cls.PRODUCTION,
            "production": cls.PRODUCTION,
        }
        return aliases.get(normalized, cls.PRODUCTION)


def load_dotenv_if_present(dotenv_path: Optional[Path] = None) -> bool:
    """Load a ``.env`` file into ``os.environ`` if python-dotenv is installed.

    This is intentionally soft: the project must run even when dotenv is not
    installed (production images may inject env vars directly). Existing
    process variables are never overridden.

    Returns
    -------
    bool
        True if a dotenv file was found and loaded, otherwise False.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return False

    target = dotenv_path
    if target is None:
        # Walk upward from CWD looking for a .env; default to CWD/.env.
        candidate = Path.cwd() / ".env"
        target = candidate if candidate.exists() else None

    if target is None or not Path(target).exists():
        return False

    return bool(load_dotenv(dotenv_path=str(target), override=False))


def get_environment() -> AppEnvironment:
    """Return the currently active :class:`AppEnvironment`."""
    return AppEnvironment.from_string(os.environ.get(ENV_VAR_NAME))


def _coerce(
    key: str,
    caster: Callable[[str], T],
    default: Optional[T],
    required: bool,
) -> Optional[T]:
    """Internal helper that reads, validates, and casts an env var."""
    raw = os.environ.get(key)
    if raw is None or raw == "":
        if required:
            raise EnvVarError(f"Required environment variable '{key}' is not set.")
        return default
    try:
        return caster(raw)
    except (ValueError, TypeError) as exc:
        raise EnvVarError(
            f"Environment variable '{key}'={raw!r} could not be parsed: {exc}"
        ) from exc


def get_str(key: str, default: Optional[str] = None, *, required: bool = False) -> Optional[str]:
    """Read a string environment variable."""
    return _coerce(key, str, default, required)


def get_int(key: str, default: Optional[int] = None, *, required: bool = False) -> Optional[int]:
    """Read an integer environment variable."""
    return _coerce(key, lambda v: int(v.strip()), default, required)


def get_float(key: str, default: Optional[float] = None, *, required: bool = False) -> Optional[float]:
    """Read a float environment variable."""
    return _coerce(key, lambda v: float(v.strip()), default, required)


def get_bool(key: str, default: Optional[bool] = None, *, required: bool = False) -> Optional[bool]:
    """Read a boolean environment variable using a permissive token set."""

    def _to_bool(value: str) -> bool:
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise ValueError(f"'{value}' is not a recognized boolean token")

    return _coerce(key, _to_bool, default, required)


def get_path(key: str, default: Optional[Path] = None, *, required: bool = False) -> Optional[Path]:
    """Read a filesystem path environment variable as an expanded ``Path``."""
    return _coerce(
        key,
        lambda v: Path(v.strip()).expanduser(),
        default,
        required,
    )


def get_list(
    key: str,
    default: Optional[Sequence[str]] = None,
    *,
    separator: str = ",",
    required: bool = False,
) -> Optional[list[str]]:
    """Read a delimited list environment variable (default separator ``,``)."""

    def _to_list(value: str) -> list[str]:
        return [item.strip() for item in value.split(separator) if item.strip()]

    result = _coerce(key, _to_list, list(default) if default is not None else None, required)
    return result


def is_production() -> bool:
    """Convenience predicate: is the active environment PRODUCTION?"""
    return get_environment() is AppEnvironment.PRODUCTION


def is_development() -> bool:
    """Convenience predicate: is the active environment DEVELOPMENT?"""
    return get_environment() is AppEnvironment.DEVELOPMENT


def is_testing() -> bool:
    """Convenience predicate: is the active environment TESTING?"""
    return get_environment() is AppEnvironment.TESTING
