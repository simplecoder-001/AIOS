# app/core/configs/config_validator.py
"""
Configuration schema definitions and validation.

Validates the *fully merged* configuration tree (defaults + YAML + env) and
returns a normalized, type-safe representation. Validation runs during Phase 0
bootstrap, before any feature group starts, so misconfiguration fails fast and
loud instead of surfacing as a subtle runtime error deep inside FG1/FG2.

Dependency order
----------------
Depends on ``environment.py`` and ``defaults.py``. No filesystem I/O.

Pydantic
--------
Uses Pydantic v2 when available. If Pydantic is not installed, a minimal
structural fallback validator is used so the system can still boot in a
degraded (but reported) validation mode.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.configs.environment import AppEnvironment

__all__ = [
    "ConfigValidationError",
    "validate_config",
    "PYDANTIC_AVAILABLE",
]


class ConfigValidationError(ValueError):
    """Raised when the merged configuration fails validation.

    Carries a list of human-readable error strings for logging/telemetry.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        joined = "\n  - ".join(errors)
        super().__init__(f"Configuration validation failed:\n  - {joined}")


# Canonical allowed value sets, sourced from the SDD documents.
_VALID_ROLES: Final[frozenset[str]] = frozenset(
    {"guest", "user", "admin", "super_admin", "system"}
)
_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_VALID_LANGUAGE_MODES: Final[frozenset[str]] = frozenset({"smart", "fixed", "follow"})
_KNOWN_LANGUAGES: Final[frozenset[str]] = frozenset(
    {"en", "hi", "hinglish", "or"}  # extendable via language_policy registry
)


# --------------------------------------------------------------------------- #
# Pydantic path (preferred)
# --------------------------------------------------------------------------- #
try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

    PYDANTIC_AVAILABLE = True

    class _HardwareModel(BaseModel):
        model_config = ConfigDict(extra="allow")
        gpu_enabled: bool = True
        max_ram_gb: float = Field(default=3.0, gt=0, le=256)

    class _AppModel(BaseModel):
        model_config = ConfigDict(extra="allow")
        name: str = Field(min_length=1)
        version: str = Field(min_length=1)
        environment: str
        debug: bool = False
        high_load_mode: bool = False
        shutdown_grace_seconds: int = Field(default=10, ge=0, le=300)
        startup_timeout_seconds: int = Field(default=60, ge=1, le=3600)
        hardware: _HardwareModel = Field(default_factory=_HardwareModel)

        @field_validator("environment")
        @classmethod
        def _env_must_be_known(cls, value: str) -> str:
            valid = {e.value for e in AppEnvironment}
            if value not in valid:
                raise ValueError(f"environment '{value}' not in {sorted(valid)}")
            return value

    class _LoggingModel(BaseModel):
        model_config = ConfigDict(extra="allow")
        level: str = "INFO"
        console: bool = True
        file: bool = True
        json_format: bool = False
        rotation_mb: int = Field(default=20, ge=1, le=1024)
        retention_days: int = Field(default=14, ge=1, le=3650)

        @field_validator("level")
        @classmethod
        def _level_must_be_valid(cls, value: str) -> str:
            upper = value.upper()
            if upper not in _VALID_LOG_LEVELS:
                raise ValueError(f"log level '{value}' not in {sorted(_VALID_LOG_LEVELS)}")
            return upper

    class _PermissionsModel(BaseModel):
        model_config = ConfigDict(extra="allow")
        default_role: str = "guest"
        roles: list[str] = Field(default_factory=lambda: list(_VALID_ROLES))
        require_auth: bool = True
        continuous_verification: bool = True

        @field_validator("roles")
        @classmethod
        def _roles_known(cls, value: list[str]) -> list[str]:
            unknown = set(value) - _VALID_ROLES
            if unknown:
                raise ValueError(f"unknown roles {sorted(unknown)}")
            return value

        @field_validator("default_role")
        @classmethod
        def _default_role_known(cls, value: str) -> str:
            if value not in _VALID_ROLES:
                raise ValueError(f"default_role '{value}' not in {sorted(_VALID_ROLES)}")
            return value

    class _LanguagePolicyModel(BaseModel):
        model_config = ConfigDict(extra="allow")
        default_language: str = "en"
        mode: str = "smart"
        supported: list[str] = Field(default_factory=lambda: ["en", "hi", "hinglish", "or"])
        preserve_technical_vocabulary: bool = True
        avoid_unnecessary_switching: bool = True

        @field_validator("mode")
        @classmethod
        def _mode_known(cls, value: str) -> str:
            if value not in _VALID_LANGUAGE_MODES:
                raise ValueError(f"language mode '{value}' not in {sorted(_VALID_LANGUAGE_MODES)}")
            return value

        @field_validator("default_language")
        @classmethod
        def _default_language_supported(cls, value: str) -> str:
            # Warn-level constraint kept soft: unknown codes allowed but flagged
            # by the registry layer; here we only reject empties.
            if not value.strip():
                raise ValueError("default_language must not be empty")
            return value

    class _ModelRegistryModel(BaseModel):
        model_config = ConfigDict(extra="allow")
        intent_classifier: str = Field(min_length=1)
        local_llm: str = Field(min_length=1)
        cloud_llm: str = Field(min_length=1)
        embedding: str = Field(min_length=1)
        asr_primary: str = Field(min_length=1)
        translation: str = Field(min_length=1)
        tts_primary: str = Field(min_length=1)
        tts_fallback: str = Field(min_length=1)
        prefer_local: bool = True

    class _FeatureFlagsModel(BaseModel):
        # Feature flags are open-ended booleans; allow extras but coerce types.
        model_config = ConfigDict(extra="allow")

    class _RootConfigModel(BaseModel):
        """Top-level validated configuration document."""

        model_config = ConfigDict(extra="allow")

        app: _AppModel
        logging: _LoggingModel = Field(default_factory=_LoggingModel)
        permissions: _PermissionsModel = Field(default_factory=_PermissionsModel)
        language_policy: _LanguagePolicyModel = Field(default_factory=_LanguagePolicyModel)
        model_registry: _ModelRegistryModel
        feature_flags: _FeatureFlagsModel = Field(default_factory=_FeatureFlagsModel)

    def validate_config(config: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize a merged config dict via Pydantic.

        Returns
        -------
        dict
            The validated configuration, re-serialized to plain dicts.

        Raises
        ------
        ConfigValidationError
            If any section fails validation.
        """
        try:
            model = _RootConfigModel.model_validate(config)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            ]
            raise ConfigValidationError(errors) from exc
        return model.model_dump(mode="python")

except ImportError:  # pragma: no cover - exercised only without pydantic
    PYDANTIC_AVAILABLE = False

    def validate_config(config: dict[str, Any]) -> dict[str, Any]:
        """Structural fallback validator used when Pydantic is unavailable.

        Performs the essential invariant checks that protect the boot path,
        without full schema coverage.
        """
        errors: list[str] = []

        app = config.get("app")
        if not isinstance(app, dict):
            errors.append("app: missing or not a mapping")
        else:
            if not app.get("name"):
                errors.append("app.name: required and non-empty")
            if not app.get("version"):
                errors.append("app.version: required and non-empty")
            env = app.get("environment")
            valid_envs = {e.value for e in AppEnvironment}
            if env not in valid_envs:
                errors.append(f"app.environment: '{env}' not in {sorted(valid_envs)}")

        logging_cfg = config.get("logging", {})
        if isinstance(logging_cfg, dict):
            level = str(logging_cfg.get("level", "INFO")).upper()
            if level not in _VALID_LOG_LEVELS:
                errors.append(f"logging.level: '{level}' invalid")

        perms = config.get("permissions", {})
        if isinstance(perms, dict):
            default_role = perms.get("default_role", "guest")
            if default_role not in _VALID_ROLES:
                errors.append(f"permissions.default_role: '{default_role}' invalid")
            unknown = set(perms.get("roles", [])) - _VALID_ROLES
            if unknown:
                errors.append(f"permissions.roles: unknown {sorted(unknown)}")

        registry = config.get("model_registry")
        if not isinstance(registry, dict):
            errors.append("model_registry: missing or not a mapping")
        else:
            for required_key in ("intent_classifier", "local_llm", "embedding", "tts_primary"):
                if not registry.get(required_key):
                    errors.append(f"model_registry.{required_key}: required")

        if errors:
            raise ConfigValidationError(errors)
        return config
