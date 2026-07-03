"""
Application-level constants for AIOS (Personal AI Operating System).

This module defines immutable, process-wide identity, versioning, runtime,
and hardware-target metadata. It has ZERO internal dependencies so it can be
safely imported by any layer (bootstrap, feature groups, tests) without risk
of circular imports.

Rules for this module:
    * Only literals, enums, and frozen containers — no runtime logic that
      performs I/O, spawns threads, or reads mutable global state.
    * Everything is effectively read-only. Mutable defaults are frozen.
    * Values here represent *compile-time* facts about the application, not
      user-configurable settings (those live in `app/core/configs/`).
"""

from __future__ import annotations

import sys
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping

# ---------------------------------------------------------------------------
# Application Identity
# ---------------------------------------------------------------------------

APP_NAME: Final[str] = "AIOS"
APP_FULL_NAME: Final[str] = "Personal AI Operating System"
APP_SLUG: Final[str] = "aios"
APP_VENDOR: Final[str] = "AIOS Team"
APP_DESCRIPTION: Final[str] = (
    "Offline-first, privacy-first, voice-driven personal AI operating system "
    "with modular feature groups and plugin-based extensibility."
)

# ---------------------------------------------------------------------------
# Versioning (Semantic Versioning 2.0.0)
# ---------------------------------------------------------------------------

VERSION_MAJOR: Final[int] = 1
VERSION_MINOR: Final[int] = 0
VERSION_PATCH: Final[int] = 0
VERSION_STAGE: Final[str] = "alpha"  # alpha | beta | rc | stable

APP_VERSION: Final[str] = f"{VERSION_MAJOR}.{VERSION_MINOR}.{VERSION_PATCH}"
APP_VERSION_FULL: Final[str] = (
    APP_VERSION if VERSION_STAGE == "stable" else f"{APP_VERSION}-{VERSION_STAGE}"
)
APP_VERSION_TUPLE: Final[tuple[int, int, int]] = (
    VERSION_MAJOR,
    VERSION_MINOR,
    VERSION_PATCH,
)

# Schema / on-disk data versions. Bump independently from APP_VERSION so that
# migrations (see scripts/migrate_database.py) can be triggered precisely.
DATABASE_SCHEMA_VERSION: Final[int] = 1
CONFIG_SCHEMA_VERSION: Final[int] = 1
PLUGIN_API_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------
# Runtime Environment
# ---------------------------------------------------------------------------


class Environment(str, Enum):
    """Deployment environment. Selected at startup via configuration."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


DEFAULT_ENVIRONMENT: Final[Environment] = Environment.DEVELOPMENT


class RuntimeMode(str, Enum):
    """High-level operating mode for the assistant runtime."""

    ONLINE = "online"        # Cloud LLM + realtime search available
    OFFLINE = "offline"      # Fully local (Gemma, local models only)
    HYBRID = "hybrid"        # Prefer local, fall back to cloud when allowed


DEFAULT_RUNTIME_MODE: Final[RuntimeMode] = RuntimeMode.HYBRID


class PerformanceMode(str, Enum):
    """User-selectable performance profile surfaced in the GUI status bar."""

    POWER_SAVER = "power_saver"
    BALANCED = "balanced"
    HIGH_PERFORMANCE = "high_performance"
    HIGH_LOAD = "high_load"  # Suppresses non-essential monitoring (FG3 Step 13)


DEFAULT_PERFORMANCE_MODE: Final[PerformanceMode] = PerformanceMode.BALANCED

# ---------------------------------------------------------------------------
# Platform Constraints
# ---------------------------------------------------------------------------

# AIOS targets Windows first (native automation via pywin32/pywinauto, TPM 2.0,
# DPAPI, AppContainer). Other platforms are unsupported for the core control layer.
SUPPORTED_PLATFORMS: Final[frozenset[str]] = frozenset({"win32"})
CURRENT_PLATFORM: Final[str] = sys.platform
IS_WINDOWS: Final[bool] = sys.platform == "win32"

MIN_PYTHON_VERSION: Final[tuple[int, int]] = (3, 11)
CURRENT_PYTHON_VERSION: Final[tuple[int, int, int]] = (
    sys.version_info.major,
    sys.version_info.minor,
    sys.version_info.micro,
)

# ---------------------------------------------------------------------------
# Reference Hardware Target
# ---------------------------------------------------------------------------
# The architecture is tuned for a Ryzen 7 CPU + RTX 5050 GPU. These are targets
# used by the Model Manager (FG2) and telemetry thresholds (core/telemetry).

REFERENCE_CPU: Final[str] = "AMD Ryzen 7"
REFERENCE_GPU: Final[str] = "NVIDIA RTX 5050"
REFERENCE_GPU_VRAM_GB: Final[int] = 8

HARDWARE_TARGETS: Final[Mapping[str, object]] = MappingProxyType(
    {
        "cpu": REFERENCE_CPU,
        "gpu": REFERENCE_GPU,
        "gpu_vram_gb": REFERENCE_GPU_VRAM_GB,
        "cuda_required": False,       # Degrades gracefully to CPU when absent
        "gpu_accelerated": True,
    }
)

# ---------------------------------------------------------------------------
# Feature Group Registry (identity only; wiring lives in the DI container)
# ---------------------------------------------------------------------------


class FeatureGroup(str, Enum):
    """Canonical identifiers for the ten AIOS feature groups."""

    FG1_VOICE = "fg1_voice_system"
    FG2_BRAIN = "fg2_ai_brain"
    FG3_WINDOWS_CONTROL = "fg3_windows_control"
    FG4_LANGUAGE = "fg4_language_intelligence"
    FG5_GUI = "fg5_gui"
    FG6_SECURITY = "fg6_security"
    FG7_PLUGINS = "fg7_plugins"
    FG8_PRODUCTIVITY = "fg8_productivity"
    FG9_AGENTS = "fg9_agent_system"
    FG10_SELF_LEARNING = "fg10_self_learning"


# Human-readable labels for logs and the GUI.
FEATURE_GROUP_LABELS: Final[Mapping[FeatureGroup, str]] = MappingProxyType(
    {
        FeatureGroup.FG1_VOICE: "Voice Interaction System",
        FeatureGroup.FG2_BRAIN: "AI Brain & Intelligence",
        FeatureGroup.FG3_WINDOWS_CONTROL: "Windows System Control",
        FeatureGroup.FG4_LANGUAGE: "Language Intelligence & Speech Adaptation",
        FeatureGroup.FG5_GUI: "GUI & User Experience",
        FeatureGroup.FG6_SECURITY: "Security & Permission System",
        FeatureGroup.FG7_PLUGINS: "Plugin & Extension System",
        FeatureGroup.FG8_PRODUCTIVITY: "Productivity System",
        FeatureGroup.FG9_AGENTS: "Agent System",
        FeatureGroup.FG10_SELF_LEARNING: "Self-Learning System",
    }
)

# Startup ordering derived from Phase 0 → Phase 4 (see Phase 0.txt).
# The bootstrap sequencer initializes feature groups in this order.
FEATURE_GROUP_STARTUP_ORDER: Final[tuple[FeatureGroup, ...]] = (
    FeatureGroup.FG1_VOICE,
    FeatureGroup.FG2_BRAIN,
    FeatureGroup.FG6_SECURITY,
    FeatureGroup.FG3_WINDOWS_CONTROL,
    FeatureGroup.FG4_LANGUAGE,
    FeatureGroup.FG8_PRODUCTIVITY,
    FeatureGroup.FG7_PLUGINS,
    FeatureGroup.FG9_AGENTS,
    FeatureGroup.FG5_GUI,
    FeatureGroup.FG10_SELF_LEARNING,
)

# ---------------------------------------------------------------------------
# Encoding / Locale Defaults
# ---------------------------------------------------------------------------

DEFAULT_ENCODING: Final[str] = "utf-8"
DEFAULT_TIMEZONE: Final[str] = "UTC"
DEFAULT_LOCALE: Final[str] = "en_US"

# ---------------------------------------------------------------------------
# Branding / User-facing strings
# ---------------------------------------------------------------------------

WAKE_WORD_DEFAULT: Final[str] = "assistant"
STARTUP_BANNER: Final[str] = (
    f"{APP_NAME} v{APP_VERSION_FULL} — {APP_FULL_NAME}"
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Identity
    "APP_NAME",
    "APP_FULL_NAME",
    "APP_SLUG",
    "APP_VENDOR",
    "APP_DESCRIPTION",
    # Version
    "VERSION_MAJOR",
    "VERSION_MINOR",
    "VERSION_PATCH",
    "VERSION_STAGE",
    "APP_VERSION",
    "APP_VERSION_FULL",
    "APP_VERSION_TUPLE",
    "DATABASE_SCHEMA_VERSION",
    "CONFIG_SCHEMA_VERSION",
    "PLUGIN_API_VERSION",
    # Runtime
    "Environment",
    "DEFAULT_ENVIRONMENT",
    "RuntimeMode",
    "DEFAULT_RUNTIME_MODE",
    "PerformanceMode",
    "DEFAULT_PERFORMANCE_MODE",
    # Platform
    "SUPPORTED_PLATFORMS",
    "CURRENT_PLATFORM",
    "IS_WINDOWS",
    "MIN_PYTHON_VERSION",
    "CURRENT_PYTHON_VERSION",
    # Hardware
    "REFERENCE_CPU",
    "REFERENCE_GPU",
    "REFERENCE_GPU_VRAM_GB",
    "HARDWARE_TARGETS",
    # Feature groups
    "FeatureGroup",
    "FEATURE_GROUP_LABELS",
    "FEATURE_GROUP_STARTUP_ORDER",
    # Locale / encoding
    "DEFAULT_ENCODING",
    "DEFAULT_TIMEZONE",
    "DEFAULT_LOCALE",
    # Branding
    "WAKE_WORD_DEFAULT",
    "STARTUP_BANNER",
]
