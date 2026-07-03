# app/core/constants/limits.py
"""
Hard limits and guardrails for AIOS.

These are ENFORCED boundaries, not tunable preferences. Whereas settings.py
provides defaults that configuration overrides freely, the values here define
the outer envelope that configuration may NOT exceed. The config validator
(core/configs/config_validator.py) clamps or rejects any tuned value that
crosses these limits.

Purpose:
    * Prevent resource exhaustion (memory, queues, threads, disk).
    * Bound untrusted input (prompts, audio, files, plugin payloads).
    * Cap retries, timeouts, and rate to guarantee liveness.

Design rules:
    * Only literals and frozen containers; no logic beyond pure clamps.
    * Units are explicit in every name (*_bytes, *_ms, *_seconds, *_count).
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping


# ---------------------------------------------------------------------------
# Size units (for readability)
# ---------------------------------------------------------------------------

_KB: Final[int] = 1024
_MB: Final[int] = 1024 * _KB
_GB: Final[int] = 1024 * _MB


# ---------------------------------------------------------------------------
# Text / prompt limits
# ---------------------------------------------------------------------------

MAX_USER_INPUT_CHARS: Final[int] = 32_000
MAX_PROMPT_TOKENS: Final[int] = 32_768          # Absolute ceiling for any model
MAX_CONTEXT_TOKENS_HARD: Final[int] = 32_768
MAX_RESPONSE_TOKENS_HARD: Final[int] = 8_192
MAX_TRANSCRIPT_CHARS: Final[int] = 16_000
MAX_SEARCH_QUERY_CHARS: Final[int] = 2_048
MAX_SEARCH_RESULTS_HARD: Final[int] = 25


# ---------------------------------------------------------------------------
# Audio limits (FG1)
# ---------------------------------------------------------------------------

MIN_SAMPLE_RATE_HZ: Final[int] = 8_000
MAX_SAMPLE_RATE_HZ: Final[int] = 48_000
MAX_AUDIO_CHUNK_MS: Final[int] = 1_000
MAX_UTTERANCE_SECONDS: Final[int] = 120         # Single continuous utterance
MAX_AUDIO_BUFFER_BYTES: Final[int] = 32 * _MB


# ---------------------------------------------------------------------------
# File / storage limits (FG3 / FG8)
# ---------------------------------------------------------------------------

MAX_FILE_READ_BYTES: Final[int] = 256 * _MB
MAX_FILE_WRITE_BYTES: Final[int] = 256 * _MB
MAX_PATH_LENGTH: Final[int] = 260               # Windows MAX_PATH default
MAX_BACKUP_BYTES: Final[int] = 2 * _GB
MAX_LOG_FILE_BYTES: Final[int] = 50 * _MB       # Rotate beyond this
MAX_LOG_BACKUP_COUNT: Final[int] = 10


# ---------------------------------------------------------------------------
# Queue capacities (core/queues)
# ---------------------------------------------------------------------------

MAX_AUDIO_QUEUE_SIZE: Final[int] = 512
MAX_EVENT_QUEUE_SIZE: Final[int] = 4_096
MAX_TASK_QUEUE_SIZE: Final[int] = 1_024
MAX_INTERRUPT_QUEUE_SIZE: Final[int] = 64
MAX_TTS_QUEUE_SIZE: Final[int] = 128
MAX_RETRY_QUEUE_SIZE: Final[int] = 256
MAX_GENERIC_QUEUE_SIZE: Final[int] = 2_048


# ---------------------------------------------------------------------------
# Concurrency limits
# ---------------------------------------------------------------------------

MAX_WORKER_THREADS: Final[int] = 64
MAX_CONCURRENT_TASKS: Final[int] = 32
MAX_CONCURRENT_TOOLS: Final[int] = 8
MAX_CONCURRENT_SEARCHES: Final[int] = 4
MAX_CONCURRENT_PLUGINS: Final[int] = 32
MAX_LOADED_MODELS: Final[int] = 12              # Model Manager ceiling


# ---------------------------------------------------------------------------
# Timeout ceilings (absolute upper bounds, in milliseconds/seconds)
# ---------------------------------------------------------------------------

MAX_TOOL_TIMEOUT_SECONDS: Final[int] = 120
MAX_SEARCH_TIMEOUT_SECONDS: Final[int] = 30
MAX_MODEL_LOAD_TIMEOUT_SECONDS: Final[int] = 180
MAX_SANDBOX_TIMEOUT_SECONDS: Final[int] = 60
MAX_ACTION_TIMEOUT_SECONDS: Final[int] = 120
MAX_PLUGIN_TIMEOUT_SECONDS: Final[int] = 60
MAX_SHUTDOWN_GRACE_SECONDS: Final[int] = 30
MIN_TIMEOUT_MS: Final[int] = 10


# ---------------------------------------------------------------------------
# Retry limits
# ---------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS: Final[int] = 10
MAX_RETRY_DELAY_MS: Final[int] = 60_000
MAX_AUTH_ATTEMPTS_HARD: Final[int] = 5          # Absolute lockout ceiling


# ---------------------------------------------------------------------------
# Rate limits (per-minute unless noted)
# ---------------------------------------------------------------------------

MAX_CLOUD_REQUESTS_PER_MINUTE: Final[int] = 300
MAX_SEARCH_REQUESTS_PER_MINUTE: Final[int] = 120
MAX_TOOL_CALLS_PER_MINUTE: Final[int] = 240
MAX_PLUGIN_CALLS_PER_MINUTE: Final[int] = 600
MAX_EVENTS_PER_SECOND: Final[int] = 5_000


# ---------------------------------------------------------------------------
# Memory / vector limits (FG2)
# ---------------------------------------------------------------------------

MAX_MEMORY_RECORD_BYTES: Final[int] = 1 * _MB
MAX_MEMORY_RECORDS_PER_QUERY: Final[int] = 100
MAX_EMBEDDING_DIM: Final[int] = 4_096
MAX_KNOWLEDGE_GRAPH_NODES: Final[int] = 1_000_000
MAX_CONVERSATION_HISTORY_TURNS: Final[int] = 500


# ---------------------------------------------------------------------------
# Plugin payload limits (FG7)
# ---------------------------------------------------------------------------

MAX_PLUGIN_PACKAGE_BYTES: Final[int] = 100 * _MB
MAX_PLUGIN_MANIFEST_BYTES: Final[int] = 256 * _KB
MAX_PLUGIN_RAM_BYTES: Final[int] = 512 * _MB
MAX_PLUGIN_CPU_PERCENT: Final[int] = 50


# ---------------------------------------------------------------------------
# Named registry for validator introspection
# ---------------------------------------------------------------------------

LIMIT_REGISTRY: Final[Mapping[str, int]] = MappingProxyType(
    {
        "max_user_input_chars": MAX_USER_INPUT_CHARS,
        "max_prompt_tokens": MAX_PROMPT_TOKENS,
        "max_context_tokens_hard": MAX_CONTEXT_TOKENS_HARD,
        "max_response_tokens_hard": MAX_RESPONSE_TOKENS_HARD,
        "max_search_results_hard": MAX_SEARCH_RESULTS_HARD,
        "max_audio_buffer_bytes": MAX_AUDIO_BUFFER_BYTES,
        "max_file_read_bytes": MAX_FILE_READ_BYTES,
        "max_file_write_bytes": MAX_FILE_WRITE_BYTES,
        "max_event_queue_size": MAX_EVENT_QUEUE_SIZE,
        "max_task_queue_size": MAX_TASK_QUEUE_SIZE,
        "max_worker_threads": MAX_WORKER_THREADS,
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "max_loaded_models": MAX_LOADED_MODELS,
        "max_tool_timeout_seconds": MAX_TOOL_TIMEOUT_SECONDS,
        "max_retry_attempts": MAX_RETRY_ATTEMPTS,
        "max_auth_attempts_hard": MAX_AUTH_ATTEMPTS_HARD,
        "max_cloud_requests_per_minute": MAX_CLOUD_REQUESTS_PER_MINUTE,
        "max_plugin_package_bytes": MAX_PLUGIN_PACKAGE_BYTES,
    }
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def clamp(value: int, low: int, high: int) -> int:
    """Clamp an integer into the inclusive range [low, high]."""
    return max(low, min(value, high))


def within(value: int, high: int, low: int = 0) -> bool:
    """Return True if low <= value <= high."""
    return low <= value <= high


def clamp_tokens(requested: int) -> int:
    """Clamp a requested prompt-token count to the hard ceiling."""
    return clamp(requested, 1, MAX_PROMPT_TOKENS)


def clamp_search_results(requested: int) -> int:
    """Clamp a requested search-result count to the hard ceiling."""
    return clamp(requested, 1, MAX_SEARCH_RESULTS_HARD)


def clamp_retries(requested: int) -> int:
    """Clamp a requested retry count to the hard ceiling."""
    return clamp(requested, 0, MAX_RETRY_ATTEMPTS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # text / prompt
    "MAX_USER_INPUT_CHARS",
    "MAX_PROMPT_TOKENS",
    "MAX_CONTEXT_TOKENS_HARD",
    "MAX_RESPONSE_TOKENS_HARD",
    "MAX_TRANSCRIPT_CHARS",
    "MAX_SEARCH_QUERY_CHARS",
    "MAX_SEARCH_RESULTS_HARD",
    # audio
    "MIN_SAMPLE_RATE_HZ",
    "MAX_SAMPLE_RATE_HZ",
    "MAX_AUDIO_CHUNK_MS",
    "MAX_UTTERANCE_SECONDS",
    "MAX_AUDIO_BUFFER_BYTES",
    # file / storage
    "MAX_FILE_READ_BYTES",
    "MAX_FILE_WRITE_BYTES",
    "MAX_PATH_LENGTH",
    "MAX_BACKUP_BYTES",
    "MAX_LOG_FILE_BYTES",
    "MAX_LOG_BACKUP_COUNT",
    # queues
    "MAX_AUDIO_QUEUE_SIZE",
    "MAX_EVENT_QUEUE_SIZE",
    "MAX_TASK_QUEUE_SIZE",
    "MAX_INTERRUPT_QUEUE_SIZE",
    "MAX_TTS_QUEUE_SIZE",
    "MAX_RETRY_QUEUE_SIZE",
    "MAX_GENERIC_QUEUE_SIZE",
    # concurrency
    "MAX_WORKER_THREADS",
    "MAX_CONCURRENT_TASKS",
    "MAX_CONCURRENT_TOOLS",
    "MAX_CONCURRENT_SEARCHES",
    "MAX_CONCURRENT_PLUGINS",
    "MAX_LOADED_MODELS",
    # timeouts
    "MAX_TOOL_TIMEOUT_SECONDS",
    "MAX_SEARCH_TIMEOUT_SECONDS",
    "MAX_MODEL_LOAD_TIMEOUT_SECONDS",
    "MAX_SANDBOX_TIMEOUT_SECONDS",
    "MAX_ACTION_TIMEOUT_SECONDS",
    "MAX_PLUGIN_TIMEOUT_SECONDS",
    "MAX_SHUTDOWN_GRACE_SECONDS",
    "MIN_TIMEOUT_MS",
    # retries
    "MAX_RETRY_ATTEMPTS",
    "MAX_RETRY_DELAY_MS",
    "MAX_AUTH_ATTEMPTS_HARD",
    # rate
    "MAX_CLOUD_REQUESTS_PER_MINUTE",
    "MAX_SEARCH_REQUESTS_PER_MINUTE",
    "MAX_TOOL_CALLS_PER_MINUTE",
    "MAX_PLUGIN_CALLS_PER_MINUTE",
    "MAX_EVENTS_PER_SECOND",
    # memory / vector
    "MAX_MEMORY_RECORD_BYTES",
    "MAX_MEMORY_RECORDS_PER_QUERY",
    "MAX_EMBEDDING_DIM",
    "MAX_KNOWLEDGE_GRAPH_NODES",
    "MAX_CONVERSATION_HISTORY_TURNS",
    # plugin payloads
    "MAX_PLUGIN_PACKAGE_BYTES",
    "MAX_PLUGIN_MANIFEST_BYTES",
    "MAX_PLUGIN_RAM_BYTES",
    "MAX_PLUGIN_CPU_PERCENT",
    # registry + helpers
    "LIMIT_REGISTRY",
    "clamp",
    "within",
    "clamp_tokens",
    "clamp_search_results",
    "clamp_retries",
]
