# app/core/constants/settings.py
"""
Default runtime-tunable settings for AIOS.

These are the *baseline defaults* that seed `configs/*.yaml`. Unlike app.py
(compile-time identity) these values are expected to be overridden by
configuration at runtime. Keeping them here gives the config loader a typed,
validated starting point and documents every knob in one place.

Sources:
    * FG1 §12 Performance Targets, FG6 Performance Targets
    * FG2 memory TTLs, importance scale, token budget priorities
    * FG3 execution latency/verification budgets
    * FG4 confidence bands (referenced from languages.py)

Design rules:
    * Frozen dataclasses group related knobs; module-level frozen instances
      act as the canonical defaults.
    * Durations are expressed in explicit units in the field name
      (e.g. *_ms, *_seconds, *_days).
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Mapping

from app.core.constants.app import (
    DEFAULT_ENVIRONMENT,
    DEFAULT_PERFORMANCE_MODE,
    DEFAULT_RUNTIME_MODE,
    Environment,
    PerformanceMode,
    RuntimeMode,
)


# ---------------------------------------------------------------------------
# Voice pipeline settings (FG1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    sample_rate_hz: int = 16_000
    channels: int = 1
    chunk_duration_ms: int = 30
    wake_word: str = "assistant"
    # Performance targets (FG1 §12) — used as SLO thresholds by telemetry.
    wake_detection_target_ms: int = 200
    speaker_verification_target_ms: int = 1_500
    vad_detection_target_ms: int = 50
    stt_final_target_ms: int = 800
    tts_first_audio_target_ms: int = 400
    interrupt_response_target_ms: int = 300
    # Continuous verification cadence.
    continuous_verification_interval_seconds: int = 5
    speaker_similarity_threshold: float = 0.70


VOICE: Final[VoiceSettings] = VoiceSettings()


# ---------------------------------------------------------------------------
# AI Brain settings (FG2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrainSettings:
    intent_latency_target_ms: int = 15
    default_max_search_results: int = 5
    # Token budget: total prompt budget for the local model context window.
    max_context_tokens: int = 8_192
    response_reserve_tokens: int = 1_024
    # Confidence policy (mirrors permissions.py; kept for tuning convenience).
    confidence_auto_execute: float = 0.90
    confidence_ask_user: float = 0.60
    # Local model generation defaults.
    temperature: float = 0.7
    top_p: float = 0.95
    max_output_tokens: int = 1_024
    stream_tokens: bool = True


BRAIN: Final[BrainSettings] = BrainSettings()


# Token Budget Manager priority order (FG2 §13). Lower number = kept longest;
# higher-numbered items are truncated first.
TOKEN_BUDGET_PRIORITY: Final[Mapping[str, int]] = MappingProxyType(
    {
        "system_prompt": 1,
        "current_conversation": 2,
        "personal_memory": 3,
        "knowledge_graph": 4,
        "qdrant_memories": 5,
        "search_results": 6,
        "older_conversation": 7,
    }
)


# ---------------------------------------------------------------------------
# Memory settings (FG2 §14-16)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemorySettings:
    temporary_cache_ttl_days: int = 3          # FG2: temporary cache lifetime
    search_cache_ttl_days: int = 3             # FG2: temporary search cache
    session_memory_ttl_hours: int = 12
    # Memory Importance Score scale (FG2 §16): 0..5
    importance_min: int = 0
    importance_max: int = 5
    importance_discard: int = 0
    importance_permanent: int = 5
    # Default score assigned when the scorer is uncertain.
    importance_default: int = 2
    keep_memory_versions: bool = True          # FG2 §17 versioning
    long_absence_days: int = 3                 # FG5 "Happy Reunion" trigger


MEMORY: Final[MemorySettings] = MemorySettings()


# ---------------------------------------------------------------------------
# Cache settings (core/cache)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheSettings:
    memory_cache_max_entries: int = 4_096
    lru_cache_max_entries: int = 2_048
    disk_cache_max_mb: int = 512
    semantic_cache_enabled: bool = True
    default_ttl_seconds: int = 3_600


CACHE: Final[CacheSettings] = CacheSettings()


# ---------------------------------------------------------------------------
# Windows control settings (FG3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowsControlSettings:
    native_latency_target_ms: int = 15        # Tier 1
    vision_latency_target_ms: int = 80        # Tier 2
    vlm_latency_target_ms: int = 900          # Tier 3
    max_action_retries: int = 2
    vision_retry_limit: int = 1
    action_timeout_seconds: int = 30
    create_recovery_points: bool = True       # Destructive-action rollback


WINDOWS_CONTROL: Final[WindowsControlSettings] = WindowsControlSettings()


# ---------------------------------------------------------------------------
# Security settings (FG6 Performance Targets)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    speaker_verification_target_ms: int = 1_500
    mfa_verification_target_ms: int = 2_000
    permission_validation_target_ms: int = 5
    risk_evaluation_target_ms: int = 20
    firewall_analysis_target_ms: int = 100
    tool_validation_target_ms: int = 5
    sandbox_startup_target_ms: int = 100
    log_write_target_ms: int = 10
    recovery_init_target_ms: int = 2_000
    # Authentication hardening.
    max_auth_attempts: int = 3
    session_ttl_minutes: int = 30
    require_mfa_for_high_risk: bool = True
    fail_secure: bool = True                  # Any failure -> safest state


SECURITY: Final[SecuritySettings] = SecuritySettings()


# ---------------------------------------------------------------------------
# Telemetry / resource thresholds (core/telemetry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    idle_cpu_warn_percent: int = 15           # FG1 idle target
    active_cpu_warn_percent: int = 30         # FG1 active target
    cpu_critical_percent: int = 90
    ram_warn_mb: int = 3_000                  # FG1 RAM target (<3 GB)
    ram_critical_percent: int = 90
    vram_warn_percent: int = 80
    vram_critical_percent: int = 95
    disk_warn_percent: int = 85
    disk_critical_percent: int = 95
    battery_low_percent: int = 20
    heartbeat_interval_seconds: int = 5
    monitor_poll_interval_seconds: int = 2


TELEMETRY: Final[TelemetrySettings] = TelemetrySettings()


# ---------------------------------------------------------------------------
# Search provider settings (FG2 §8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchSettings:
    default_provider: str = "auto"
    allow_fallback: bool = True
    max_results: int = 5
    request_timeout_seconds: int = 10
    # Ordered provider chain (FG2 §8).
    provider_order: tuple[str, ...] = (
        "cache",
        "tavily",
        "brave",
        "duckduckgo",
        "gemini_grounding",
    )
    empty_result_retry_limit: int = 2


SEARCH: Final[SearchSettings] = SearchSettings()


# ---------------------------------------------------------------------------
# Plugin settings (FG7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PluginSettings:
    hot_reload_enabled: bool = True
    verify_signatures: bool = True
    default_network_access: bool = False      # Admin-gated egress
    execution_timeout_seconds: int = 15
    max_concurrent_plugins: int = 16
    rate_limit_per_minute: int = 120


PLUGINS: Final[PluginSettings] = PluginSettings()


# ---------------------------------------------------------------------------
# Retry policy (shared by tools, search, model loading)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_ms: int = 200
    max_delay_ms: int = 5_000
    backoff_multiplier: float = 2.0
    jitter: bool = True


DEFAULT_RETRY: Final[RetryPolicy] = RetryPolicy()


# ---------------------------------------------------------------------------
# Feature flags (defaults for configs/feature_flags.yaml)
# ---------------------------------------------------------------------------

DEFAULT_FEATURE_FLAGS: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "voice_enabled": True,
        "continuous_verification": True,
        "cloud_llm_enabled": True,
        "realtime_search_enabled": True,
        "vision_automation_enabled": True,
        "desktop_companion_enabled": True,
        "smart_cursor_enabled": True,
        "plugins_enabled": True,
        "self_learning_enabled": True,
        "telemetry_enabled": True,
        "high_load_mode_auto": True,
    }
)


# ---------------------------------------------------------------------------
# Runtime defaults (re-exported from app.py for a single settings entrypoint)
# ---------------------------------------------------------------------------

DEFAULT_ENV: Final[Environment] = DEFAULT_ENVIRONMENT
DEFAULT_RUNTIME: Final[RuntimeMode] = DEFAULT_RUNTIME_MODE
DEFAULT_PERFORMANCE: Final[PerformanceMode] = DEFAULT_PERFORMANCE_MODE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "VoiceSettings",
    "VOICE",
    "BrainSettings",
    "BRAIN",
    "TOKEN_BUDGET_PRIORITY",
    "MemorySettings",
    "MEMORY",
    "CacheSettings",
    "CACHE",
    "WindowsControlSettings",
    "WINDOWS_CONTROL",
    "SecuritySettings",
    "SECURITY",
    "TelemetrySettings",
    "TELEMETRY",
    "SearchSettings",
    "SEARCH",
    "PluginSettings",
    "PLUGINS",
    "RetryPolicy",
    "DEFAULT_RETRY",
    "DEFAULT_FEATURE_FLAGS",
    "DEFAULT_ENV",
    "DEFAULT_RUNTIME",
    "DEFAULT_PERFORMANCE",
]
