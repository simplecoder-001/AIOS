# app/core/constants/errors.py
"""
Error-code catalog and taxonomy for AIOS.

This module defines stable, machine-readable error CODES and their metadata.
The exception CLASSES live in `app/core/exceptions/`; they carry an ErrorCode
so that logging, audit, recovery, and the GUI can react by code without
importing the exception hierarchy (avoiding cycles and keeping codes stable
across refactors).

Code scheme:
    "AIOS-<DOMAIN><NNN>"  e.g. "AIOS-SEC001"
    Domains map to subsystems; numbers are stable once published.

Design rules:
    * `str`-based ErrorCode enum; immutable metadata frozen with MappingProxyType.
    * Standard library only; import-safe; no cycles.
    * Depends only on constants.app (severity is local) — no exception imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Final, Mapping


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class ErrorSeverity(IntEnum):
    """Severity ranking used by logging and escalation."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50
    FATAL = 60      # Triggers fail-secure shutdown


# ---------------------------------------------------------------------------
# Error domains (align with core/exceptions/*.py)
# ---------------------------------------------------------------------------


class ErrorDomain(str, Enum):
    """Subsystem that owns the error."""

    GENERAL = "GEN"
    CONFIG = "CFG"
    DEPENDENCY = "DEP"
    DATABASE = "DB"
    STATE = "STATE"
    EVENT = "EVT"
    QUEUE = "QUE"
    SECURITY = "SEC"
    VALIDATION = "VAL"
    STARTUP = "START"
    RUNTIME = "RUN"
    VOICE = "VOICE"
    BRAIN = "BRAIN"
    MODEL = "MODEL"
    SEARCH = "SEARCH"
    TOOL = "TOOL"
    WINDOWS = "WIN"
    LANGUAGE = "LANG"
    PLUGIN = "PLUG"
    NETWORK = "NET"


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Canonical, stable error codes. Never renumber a published code."""

    # General
    UNKNOWN = "AIOS-GEN000"
    NOT_IMPLEMENTED = "AIOS-GEN001"
    INVALID_ARGUMENT = "AIOS-GEN002"
    OPERATION_CANCELLED = "AIOS-GEN003"
    TIMEOUT = "AIOS-GEN004"

    # Config
    CONFIG_NOT_FOUND = "AIOS-CFG001"
    CONFIG_INVALID = "AIOS-CFG002"
    CONFIG_SCHEMA_MISMATCH = "AIOS-CFG003"
    CONFIG_LIMIT_EXCEEDED = "AIOS-CFG004"

    # Dependency injection
    DEPENDENCY_MISSING = "AIOS-DEP001"
    DEPENDENCY_CYCLE = "AIOS-DEP002"
    PROVIDER_FAILED = "AIOS-DEP003"

    # Database
    DB_CONNECTION_FAILED = "AIOS-DB001"
    DB_QUERY_FAILED = "AIOS-DB002"
    DB_MIGRATION_FAILED = "AIOS-DB003"
    DB_INTEGRITY_ERROR = "AIOS-DB004"
    DB_ENCRYPTION_ERROR = "AIOS-DB005"

    # State machine
    INVALID_STATE_TRANSITION = "AIOS-STATE001"
    STATE_NOT_FOUND = "AIOS-STATE002"

    # Event bus
    EVENT_DISPATCH_FAILED = "AIOS-EVT001"
    EVENT_HANDLER_ERROR = "AIOS-EVT002"
    EVENT_UNKNOWN = "AIOS-EVT003"

    # Queue
    QUEUE_FULL = "AIOS-QUE001"
    QUEUE_EMPTY = "AIOS-QUE002"
    QUEUE_OVERFLOW = "AIOS-QUE003"

    # Security
    AUTH_FAILED = "AIOS-SEC001"
    UNAUTHORIZED = "AIOS-SEC002"
    PERMISSION_DENIED = "AIOS-SEC003"
    RISK_TOO_HIGH = "AIOS-SEC004"
    FIREWALL_BLOCKED = "AIOS-SEC005"
    SANDBOX_VIOLATION = "AIOS-SEC006"
    SANDBOX_TIMEOUT = "AIOS-SEC007"
    TAMPER_DETECTED = "AIOS-SEC008"
    DEVICE_UNVERIFIED = "AIOS-SEC009"
    ENCRYPTION_FAILED = "AIOS-SEC010"
    MFA_REQUIRED = "AIOS-SEC011"

    # Validation
    VALIDATION_FAILED = "AIOS-VAL001"
    SCHEMA_VALIDATION_FAILED = "AIOS-VAL002"
    PARAMETER_INVALID = "AIOS-VAL003"

    # Startup
    STARTUP_FAILED = "AIOS-START001"
    UNSUPPORTED_PLATFORM = "AIOS-START002"
    MISSING_DEPENDENCY = "AIOS-START003"

    # Runtime
    RUNTIME_ERROR = "AIOS-RUN001"
    RESOURCE_EXHAUSTED = "AIOS-RUN002"
    RECOVERY_FAILED = "AIOS-RUN003"

    # Voice (FG1)
    AUDIO_DEVICE_ERROR = "AIOS-VOICE001"
    WAKEWORD_ERROR = "AIOS-VOICE002"
    STT_FAILED = "AIOS-VOICE003"
    TTS_FAILED = "AIOS-VOICE004"
    SPEAKER_VERIFICATION_FAILED = "AIOS-VOICE005"

    # Brain (FG2)
    INTENT_CLASSIFICATION_FAILED = "AIOS-BRAIN001"
    ROUTING_FAILED = "AIOS-BRAIN002"
    CONTEXT_BUILD_FAILED = "AIOS-BRAIN003"
    PLANNING_FAILED = "AIOS-BRAIN004"
    TOKEN_BUDGET_EXCEEDED = "AIOS-BRAIN005"

    # Model
    MODEL_NOT_FOUND = "AIOS-MODEL001"
    MODEL_LOAD_FAILED = "AIOS-MODEL002"
    MODEL_INFERENCE_FAILED = "AIOS-MODEL003"
    MODEL_OOM = "AIOS-MODEL004"           # Out of VRAM/RAM

    # Search
    SEARCH_FAILED = "AIOS-SEARCH001"
    SEARCH_NO_RESULTS = "AIOS-SEARCH002"
    SEARCH_PROVIDER_UNAVAILABLE = "AIOS-SEARCH003"

    # Tool
    TOOL_NOT_FOUND = "AIOS-TOOL001"
    TOOL_EXECUTION_FAILED = "AIOS-TOOL002"
    TOOL_VALIDATION_FAILED = "AIOS-TOOL003"
    TOOL_LOW_CONFIDENCE = "AIOS-TOOL004"

    # Windows control (FG3)
    NATIVE_AUTOMATION_FAILED = "AIOS-WIN001"
    VISION_AUTOMATION_FAILED = "AIOS-WIN002"
    VLM_AUTOMATION_FAILED = "AIOS-WIN003"
    ACTION_VERIFICATION_FAILED = "AIOS-WIN004"
    ROLLBACK_FAILED = "AIOS-WIN005"

    # Language (FG4)
    LANGUAGE_DETECTION_FAILED = "AIOS-LANG001"
    TRANSLATION_FAILED = "AIOS-LANG002"
    UNSUPPORTED_LANGUAGE = "AIOS-LANG003"

    # Plugin (FG7)
    PLUGIN_LOAD_FAILED = "AIOS-PLUG001"
    PLUGIN_SIGNATURE_INVALID = "AIOS-PLUG002"
    PLUGIN_MANIFEST_INVALID = "AIOS-PLUG003"
    PLUGIN_PERMISSION_DENIED = "AIOS-PLUG004"
    PLUGIN_KILLED = "AIOS-PLUG005"

    # Network
    NETWORK_UNAVAILABLE = "AIOS-NET001"
    REQUEST_FAILED = "AIOS-NET002"
    RATE_LIMITED = "AIOS-NET003"


# ---------------------------------------------------------------------------
# Error metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Immutable metadata attached to an error code."""

    code: ErrorCode
    domain: ErrorDomain
    severity: ErrorSeverity
    recoverable: bool
    message: str


def _e(
    code: ErrorCode,
    domain: ErrorDomain,
    severity: ErrorSeverity,
    recoverable: bool,
    message: str,
) -> ErrorInfo:
    return ErrorInfo(code, domain, severity, recoverable, message)


ERROR_CATALOG: Final[Mapping[ErrorCode, ErrorInfo]] = MappingProxyType(
    {
        # General
        ErrorCode.UNKNOWN: _e(ErrorCode.UNKNOWN, ErrorDomain.GENERAL, ErrorSeverity.ERROR, False, "An unknown error occurred."),
        ErrorCode.NOT_IMPLEMENTED: _e(ErrorCode.NOT_IMPLEMENTED, ErrorDomain.GENERAL, ErrorSeverity.ERROR, False, "Feature not implemented."),
        ErrorCode.INVALID_ARGUMENT: _e(ErrorCode.INVALID_ARGUMENT, ErrorDomain.GENERAL, ErrorSeverity.ERROR, False, "Invalid argument supplied."),
        ErrorCode.OPERATION_CANCELLED: _e(ErrorCode.OPERATION_CANCELLED, ErrorDomain.GENERAL, ErrorSeverity.INFO, True, "Operation was cancelled."),
        ErrorCode.TIMEOUT: _e(ErrorCode.TIMEOUT, ErrorDomain.GENERAL, ErrorSeverity.WARNING, True, "Operation timed out."),
        # Config
        ErrorCode.CONFIG_NOT_FOUND: _e(ErrorCode.CONFIG_NOT_FOUND, ErrorDomain.CONFIG, ErrorSeverity.CRITICAL, False, "Configuration file not found."),
        ErrorCode.CONFIG_INVALID: _e(ErrorCode.CONFIG_INVALID, ErrorDomain.CONFIG, ErrorSeverity.CRITICAL, False, "Configuration is invalid."),
        ErrorCode.CONFIG_SCHEMA_MISMATCH: _e(ErrorCode.CONFIG_SCHEMA_MISMATCH, ErrorDomain.CONFIG, ErrorSeverity.CRITICAL, False, "Configuration schema mismatch."),
        ErrorCode.CONFIG_LIMIT_EXCEEDED: _e(ErrorCode.CONFIG_LIMIT_EXCEEDED, ErrorDomain.CONFIG, ErrorSeverity.WARNING, True, "Configuration value exceeds a hard limit and was clamped."),
        # Dependency
        ErrorCode.DEPENDENCY_MISSING: _e(ErrorCode.DEPENDENCY_MISSING, ErrorDomain.DEPENDENCY, ErrorSeverity.CRITICAL, False, "A required dependency is missing."),
        ErrorCode.DEPENDENCY_CYCLE: _e(ErrorCode.DEPENDENCY_CYCLE, ErrorDomain.DEPENDENCY, ErrorSeverity.FATAL, False, "Dependency cycle detected."),
        ErrorCode.PROVIDER_FAILED: _e(ErrorCode.PROVIDER_FAILED, ErrorDomain.DEPENDENCY, ErrorSeverity.ERROR, True, "A DI provider failed to construct a service."),
        # Database
        ErrorCode.DB_CONNECTION_FAILED: _e(ErrorCode.DB_CONNECTION_FAILED, ErrorDomain.DATABASE, ErrorSeverity.CRITICAL, True, "Database connection failed."),
        ErrorCode.DB_QUERY_FAILED: _e(ErrorCode.DB_QUERY_FAILED, ErrorDomain.DATABASE, ErrorSeverity.ERROR, True, "Database query failed."),
        ErrorCode.DB_MIGRATION_FAILED: _e(ErrorCode.DB_MIGRATION_FAILED, ErrorDomain.DATABASE, ErrorSeverity.CRITICAL, False, "Database migration failed."),
        ErrorCode.DB_INTEGRITY_ERROR: _e(ErrorCode.DB_INTEGRITY_ERROR, ErrorDomain.DATABASE, ErrorSeverity.CRITICAL, False, "Database integrity error."),
        ErrorCode.DB_ENCRYPTION_ERROR: _e(ErrorCode.DB_ENCRYPTION_ERROR, ErrorDomain.DATABASE, ErrorSeverity.CRITICAL, False, "Encrypted database access failed."),
        # State
        ErrorCode.INVALID_STATE_TRANSITION: _e(ErrorCode.INVALID_STATE_TRANSITION, ErrorDomain.STATE, ErrorSeverity.ERROR, True, "Invalid state transition requested."),
        ErrorCode.STATE_NOT_FOUND: _e(ErrorCode.STATE_NOT_FOUND, ErrorDomain.STATE, ErrorSeverity.ERROR, False, "Requested state not found."),
        # Event
        ErrorCode.EVENT_DISPATCH_FAILED: _e(ErrorCode.EVENT_DISPATCH_FAILED, ErrorDomain.EVENT, ErrorSeverity.ERROR, True, "Event dispatch failed."),
        ErrorCode.EVENT_HANDLER_ERROR: _e(ErrorCode.EVENT_HANDLER_ERROR, ErrorDomain.EVENT, ErrorSeverity.ERROR, True, "An event handler raised an error."),
        ErrorCode.EVENT_UNKNOWN: _e(ErrorCode.EVENT_UNKNOWN, ErrorDomain.EVENT, ErrorSeverity.WARNING, True, "Unknown event type."),
        # Queue
        ErrorCode.QUEUE_FULL: _e(ErrorCode.QUEUE_FULL, ErrorDomain.QUEUE, ErrorSeverity.WARNING, True, "Queue is full."),
        ErrorCode.QUEUE_EMPTY: _e(ErrorCode.QUEUE_EMPTY, ErrorDomain.QUEUE, ErrorSeverity.DEBUG, True, "Queue is empty."),
        ErrorCode.QUEUE_OVERFLOW: _e(ErrorCode.QUEUE_OVERFLOW, ErrorDomain.QUEUE, ErrorSeverity.ERROR, True, "Queue overflow; items dropped."),
        # Security
        ErrorCode.AUTH_FAILED: _e(ErrorCode.AUTH_FAILED, ErrorDomain.SECURITY, ErrorSeverity.WARNING, True, "Authentication failed."),
        ErrorCode.UNAUTHORIZED: _e(ErrorCode.UNAUTHORIZED, ErrorDomain.SECURITY, ErrorSeverity.WARNING, True, "Unauthorized."),
        ErrorCode.PERMISSION_DENIED: _e(ErrorCode.PERMISSION_DENIED, ErrorDomain.SECURITY, ErrorSeverity.WARNING, True, "Permission denied."),
        ErrorCode.RISK_TOO_HIGH: _e(ErrorCode.RISK_TOO_HIGH, ErrorDomain.SECURITY, ErrorSeverity.ERROR, True, "Action blocked: risk too high."),
        ErrorCode.FIREWALL_BLOCKED: _e(ErrorCode.FIREWALL_BLOCKED, ErrorDomain.SECURITY, ErrorSeverity.WARNING, True, "Input blocked by AI firewall."),
        ErrorCode.SANDBOX_VIOLATION: _e(ErrorCode.SANDBOX_VIOLATION, ErrorDomain.SECURITY, ErrorSeverity.ERROR, True, "Sandbox restriction violated."),
        ErrorCode.SANDBOX_TIMEOUT: _e(ErrorCode.SANDBOX_TIMEOUT, ErrorDomain.SECURITY, ErrorSeverity.WARNING, True, "Sandbox execution timed out."),
        ErrorCode.TAMPER_DETECTED: _e(ErrorCode.TAMPER_DETECTED, ErrorDomain.SECURITY, ErrorSeverity.FATAL, False, "Tamper detected; failing secure."),
        ErrorCode.DEVICE_UNVERIFIED: _e(ErrorCode.DEVICE_UNVERIFIED, ErrorDomain.SECURITY, ErrorSeverity.FATAL, False, "Hardware device could not be verified."),
        ErrorCode.ENCRYPTION_FAILED: _e(ErrorCode.ENCRYPTION_FAILED, ErrorDomain.SECURITY, ErrorSeverity.CRITICAL, False, "Encryption operation failed."),
        ErrorCode.MFA_REQUIRED: _e(ErrorCode.MFA_REQUIRED, ErrorDomain.SECURITY, ErrorSeverity.INFO, True, "Multi-factor authentication required."),
        # Validation
        ErrorCode.VALIDATION_FAILED: _e(ErrorCode.VALIDATION_FAILED, ErrorDomain.VALIDATION, ErrorSeverity.ERROR, True, "Validation failed."),
        ErrorCode.SCHEMA_VALIDATION_FAILED: _e(ErrorCode.SCHEMA_VALIDATION_FAILED, ErrorDomain.VALIDATION, ErrorSeverity.ERROR, True, "Schema validation failed."),
        ErrorCode.PARAMETER_INVALID: _e(ErrorCode.PARAMETER_INVALID, ErrorDomain.VALIDATION, ErrorSeverity.ERROR, True, "Invalid parameter."),
        # Startup
        ErrorCode.STARTUP_FAILED: _e(ErrorCode.STARTUP_FAILED, ErrorDomain.STARTUP, ErrorSeverity.FATAL, False, "Startup failed."),
        ErrorCode.UNSUPPORTED_PLATFORM: _e(ErrorCode.UNSUPPORTED_PLATFORM, ErrorDomain.STARTUP, ErrorSeverity.FATAL, False, "Unsupported platform."),
        ErrorCode.MISSING_DEPENDENCY: _e(ErrorCode.MISSING_DEPENDENCY, ErrorDomain.STARTUP, ErrorSeverity.FATAL, False, "A required runtime dependency is missing."),
        # Runtime
        ErrorCode.RUNTIME_ERROR: _e(ErrorCode.RUNTIME_ERROR, ErrorDomain.RUNTIME, ErrorSeverity.ERROR, True, "Runtime error."),
        ErrorCode.RESOURCE_EXHAUSTED: _e(ErrorCode.RESOURCE_EXHAUSTED, ErrorDomain.RUNTIME, ErrorSeverity.CRITICAL, True, "System resource exhausted."),
        ErrorCode.RECOVERY_FAILED: _e(ErrorCode.RECOVERY_FAILED, ErrorDomain.RUNTIME, ErrorSeverity.FATAL, False, "Recovery attempt failed."),
        # Voice
        ErrorCode.AUDIO_DEVICE_ERROR: _e(ErrorCode.AUDIO_DEVICE_ERROR, ErrorDomain.VOICE, ErrorSeverity.ERROR, True, "Audio device error."),
        ErrorCode.WAKEWORD_ERROR: _e(ErrorCode.WAKEWORD_ERROR, ErrorDomain.VOICE, ErrorSeverity.WARNING, True, "Wake-word engine error."),
        ErrorCode.STT_FAILED: _e(ErrorCode.STT_FAILED, ErrorDomain.VOICE, ErrorSeverity.ERROR, True, "Speech-to-text failed."),
        ErrorCode.TTS_FAILED: _e(ErrorCode.TTS_FAILED, ErrorDomain.VOICE, ErrorSeverity.ERROR, True, "Text-to-speech failed."),
        ErrorCode.SPEAKER_VERIFICATION_FAILED: _e(ErrorCode.SPEAKER_VERIFICATION_FAILED, ErrorDomain.VOICE, ErrorSeverity.WARNING, True, "Speaker verification failed."),
        # Brain
        ErrorCode.INTENT_CLASSIFICATION_FAILED: _e(ErrorCode.INTENT_CLASSIFICATION_FAILED, ErrorDomain.BRAIN, ErrorSeverity.ERROR, True, "Intent classification failed."),
        ErrorCode.ROUTING_FAILED: _e(ErrorCode.ROUTING_FAILED, ErrorDomain.BRAIN, ErrorSeverity.ERROR, True, "Request routing failed."),
        ErrorCode.CONTEXT_BUILD_FAILED: _e(ErrorCode.CONTEXT_BUILD_FAILED, ErrorDomain.BRAIN, ErrorSeverity.ERROR, True, "Context building failed."),
        ErrorCode.PLANNING_FAILED: _e(ErrorCode.PLANNING_FAILED, ErrorDomain.BRAIN, ErrorSeverity.ERROR, True, "Planning failed."),
        ErrorCode.TOKEN_BUDGET_EXCEEDED: _e(ErrorCode.TOKEN_BUDGET_EXCEEDED, ErrorDomain.BRAIN, ErrorSeverity.WARNING, True, "Token budget exceeded; context truncated."),
        # Model
        ErrorCode.MODEL_NOT_FOUND: _e(ErrorCode.MODEL_NOT_FOUND, ErrorDomain.MODEL, ErrorSeverity.CRITICAL, False, "Model not found."),
        ErrorCode.MODEL_LOAD_FAILED: _e(ErrorCode.MODEL_LOAD_FAILED, ErrorDomain.MODEL, ErrorSeverity.CRITICAL, True, "Model failed to load."),
        ErrorCode.MODEL_INFERENCE_FAILED: _e(ErrorCode.MODEL_INFERENCE_FAILED, ErrorDomain.MODEL, ErrorSeverity.ERROR, True, "Model inference failed."),
        ErrorCode.MODEL_OOM: _e(ErrorCode.MODEL_OOM, ErrorDomain.MODEL, ErrorSeverity.CRITICAL, True, "Model out of memory; fallback required."),
        # Search
        ErrorCode.SEARCH_FAILED: _e(ErrorCode.SEARCH_FAILED, ErrorDomain.SEARCH, ErrorSeverity.WARNING, True, "Search failed."),
        ErrorCode.SEARCH_NO_RESULTS: _e(ErrorCode.SEARCH_NO_RESULTS, ErrorDomain.SEARCH, ErrorSeverity.INFO, True, "Search returned no results."),
        ErrorCode.SEARCH_PROVIDER_UNAVAILABLE: _e(ErrorCode.SEARCH_PROVIDER_UNAVAILABLE, ErrorDomain.SEARCH, ErrorSeverity.WARNING, True, "Search provider unavailable; falling back."),
        # Tool
        ErrorCode.TOOL_NOT_FOUND: _e(ErrorCode.TOOL_NOT_FOUND, ErrorDomain.TOOL, ErrorSeverity.ERROR, False, "Tool not found."),
        ErrorCode.TOOL_EXECUTION_FAILED: _e(ErrorCode.TOOL_EXECUTION_FAILED, ErrorDomain.TOOL, ErrorSeverity.ERROR, True, "Tool execution failed."),
        ErrorCode.TOOL_VALIDATION_FAILED: _e(ErrorCode.TOOL_VALIDATION_FAILED, ErrorDomain.TOOL, ErrorSeverity.ERROR, True, "Tool request validation failed."),
        ErrorCode.TOOL_LOW_CONFIDENCE: _e(ErrorCode.TOOL_LOW_CONFIDENCE, ErrorDomain.TOOL, ErrorSeverity.INFO, True, "Tool confidence below threshold; asking user."),
        # Windows control
        ErrorCode.NATIVE_AUTOMATION_FAILED: _e(ErrorCode.NATIVE_AUTOMATION_FAILED, ErrorDomain.WINDOWS, ErrorSeverity.WARNING, True, "Native automation failed; escalating tier."),
        ErrorCode.VISION_AUTOMATION_FAILED: _e(ErrorCode.VISION_AUTOMATION_FAILED, ErrorDomain.WINDOWS, ErrorSeverity.WARNING, True, "Vision automation failed; escalating tier."),
        ErrorCode.VLM_AUTOMATION_FAILED: _e(ErrorCode.VLM_AUTOMATION_FAILED, ErrorDomain.WINDOWS, ErrorSeverity.ERROR, True, "Vision-language automation failed."),
        ErrorCode.ACTION_VERIFICATION_FAILED: _e(ErrorCode.ACTION_VERIFICATION_FAILED, ErrorDomain.WINDOWS, ErrorSeverity.ERROR, True, "Action verification failed."),
        ErrorCode.ROLLBACK_FAILED: _e(ErrorCode.ROLLBACK_FAILED, ErrorDomain.WINDOWS, ErrorSeverity.CRITICAL, False, "Rollback failed."),
        # Language
        ErrorCode.LANGUAGE_DETECTION_FAILED: _e(ErrorCode.LANGUAGE_DETECTION_FAILED, ErrorDomain.LANGUAGE, ErrorSeverity.WARNING, True, "Language detection failed."),
        ErrorCode.TRANSLATION_FAILED: _e(ErrorCode.TRANSLATION_FAILED, ErrorDomain.LANGUAGE, ErrorSeverity.WARNING, True, "Translation failed."),
        ErrorCode.UNSUPPORTED_LANGUAGE: _e(ErrorCode.UNSUPPORTED_LANGUAGE, ErrorDomain.LANGUAGE, ErrorSeverity.WARNING, True, "Unsupported language."),
        # Plugin
        ErrorCode.PLUGIN_LOAD_FAILED: _e(ErrorCode.PLUGIN_LOAD_FAILED, ErrorDomain.PLUGIN, ErrorSeverity.ERROR, True, "Plugin failed to load."),
        ErrorCode.PLUGIN_SIGNATURE_INVALID: _e(ErrorCode.PLUGIN_SIGNATURE_INVALID, ErrorDomain.PLUGIN, ErrorSeverity.CRITICAL, False, "Plugin signature invalid."),
        ErrorCode.PLUGIN_MANIFEST_INVALID: _e(ErrorCode.PLUGIN_MANIFEST_INVALID, ErrorDomain.PLUGIN, ErrorSeverity.ERROR, False, "Plugin manifest invalid."),
        ErrorCode.PLUGIN_PERMISSION_DENIED: _e(ErrorCode.PLUGIN_PERMISSION_DENIED, ErrorDomain.PLUGIN, ErrorSeverity.WARNING, True, "Plugin permission denied."),
        ErrorCode.PLUGIN_KILLED: _e(ErrorCode.PLUGIN_KILLED, ErrorDomain.PLUGIN, ErrorSeverity.CRITICAL, True, "Plugin terminated by kill switch."),
        # Network
        ErrorCode.NETWORK_UNAVAILABLE: _e(ErrorCode.NETWORK_UNAVAILABLE, ErrorDomain.NETWORK, ErrorSeverity.WARNING, True, "Network unavailable; switching to offline mode."),
        ErrorCode.REQUEST_FAILED: _e(ErrorCode.REQUEST_FAILED, ErrorDomain.NETWORK, ErrorSeverity.WARNING, True, "Network request failed."),
        ErrorCode.RATE_LIMITED: _e(ErrorCode.RATE_LIMITED, ErrorDomain.NETWORK, ErrorSeverity.WARNING, True, "Rate limit exceeded."),
    }
)


# Codes that must trigger the FG6 fail-secure path (immediate safe shutdown).
FATAL_ERROR_CODES: Final[frozenset[ErrorCode]] = frozenset(
    code for code, info in ERROR_CATALOG.items() if info.severity is ErrorSeverity.FATAL
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def get_error_info(code: ErrorCode) -> ErrorInfo:
    """Return metadata for a code, falling back to UNKNOWN if unregistered."""
    return ERROR_CATALOG.get(code, ERROR_CATALOG[ErrorCode.UNKNOWN])


def is_recoverable(code: ErrorCode) -> bool:
    """Return True if the error is considered recoverable."""
    return get_error_info(code).recoverable


def is_fatal(code: ErrorCode) -> bool:
    """Return True if the error must trigger a fail-secure shutdown."""
    return code in FATAL_ERROR_CODES


def severity_of(code: ErrorCode) -> ErrorSeverity:
    """Return the severity of a code."""
    return get_error_info(code).severity


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ErrorSeverity",
    "ErrorDomain",
    "ErrorCode",
    "ErrorInfo",
    "ERROR_CATALOG",
    "FATAL_ERROR_CODES",
    "get_error_info",
    "is_recoverable",
    "is_fatal",
    "severity_of",
]
