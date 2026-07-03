"""
Centralized state definitions for AIOS.

Every subsystem drives a state machine (see each feature group's
`state/state_machine.py`). This module is the single source of truth for the
state *vocabulary* so that logging, persistence, telemetry, and the GUI status
layer all speak the same language.

Design rules:
    * `str`-based enums → JSON/SQLite/YAML friendly, human-readable in logs.
    * Immutable transition maps frozen with MappingProxyType.
    * No I/O, no logic beyond pure helpers for transition validation.
    * Depends only on the standard library (import-safe, no cycles).
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping, FrozenSet


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class BaseState(str, Enum):
    """Base class for all state enums. Enables uniform helpers."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------------------
# Application Lifecycle State (app/state/lifecycle_states.py)
# ---------------------------------------------------------------------------


class AppState(BaseState):
    """Top-level application lifecycle state."""

    CREATED = "created"
    BOOTSTRAPPING = "bootstrapping"
    INITIALIZING = "initializing"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"       # Running with one or more failed subsystems
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    SHUTDOWN = "shutdown"


APP_STATE_TRANSITIONS: Final[Mapping[AppState, FrozenSet[AppState]]] = MappingProxyType(
    {
        AppState.CREATED: frozenset({AppState.BOOTSTRAPPING, AppState.ERROR}),
        AppState.BOOTSTRAPPING: frozenset({AppState.INITIALIZING, AppState.ERROR}),
        AppState.INITIALIZING: frozenset({AppState.STARTING, AppState.ERROR}),
        AppState.STARTING: frozenset({AppState.RUNNING, AppState.ERROR}),
        AppState.RUNNING: frozenset(
            {AppState.DEGRADED, AppState.PAUSING, AppState.STOPPING, AppState.ERROR}
        ),
        AppState.DEGRADED: frozenset({AppState.RUNNING, AppState.STOPPING, AppState.ERROR}),
        AppState.PAUSING: frozenset({AppState.PAUSED, AppState.ERROR}),
        AppState.PAUSED: frozenset({AppState.RESUMING, AppState.STOPPING}),
        AppState.RESUMING: frozenset({AppState.RUNNING, AppState.ERROR}),
        AppState.STOPPING: frozenset({AppState.STOPPED, AppState.ERROR}),
        AppState.STOPPED: frozenset({AppState.SHUTDOWN}),
        AppState.ERROR: frozenset({AppState.STOPPING, AppState.SHUTDOWN}),
        AppState.SHUTDOWN: frozenset(),  # Terminal
    }
)


# ---------------------------------------------------------------------------
# FG1 — Voice Interaction State
# ---------------------------------------------------------------------------


class VoiceState(BaseState):
    """Voice pipeline state (FG1 Section 7)."""

    IDLE = "idle"
    WAITING_WAKE = "waiting_wake"
    VERIFYING = "verifying"
    AUTHORIZED = "authorized"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    DEAUTHORIZED = "deauthorized"
    SHUTDOWN = "shutdown"


# ---------------------------------------------------------------------------
# FG2 — AI Brain State (FG2 Section 31)
# ---------------------------------------------------------------------------


class BrainState(BaseState):
    """AI Brain orchestration state."""

    OFFLINE = "offline"
    STARTING = "starting"
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING_INPUT = "processing_input"
    VERIFYING_IDENTITY = "verifying_identity"
    CLASSIFYING_INTENT = "classifying_intent"
    BUILDING_CONTEXT = "building_context"
    PLANNING = "planning"
    SEARCHING = "searching"
    EXECUTING_TOOL = "executing_tool"
    WAITING_FOR_TOOL = "waiting_for_tool"
    GENERATING_RESPONSE = "generating_response"
    SPEAKING = "speaking"
    WAITING_FOR_USER = "waiting_for_user"
    ERROR = "error"
    SHUTDOWN = "shutdown"


# ---------------------------------------------------------------------------
# FG6 — Security State (FG6 "State Management")
# ---------------------------------------------------------------------------


class SecurityState(BaseState):
    """Security subsystem state."""

    STARTING = "starting"
    VERIFYING_IDENTITY = "verifying_identity"
    AUTHORIZED = "authorized"
    UNAUTHORIZED = "unauthorized"
    CHECKING_PERMISSIONS = "checking_permissions"
    EVALUATING_RISK = "evaluating_risk"
    RUNNING_FIREWALL = "running_firewall"
    VALIDATING_TOOL = "validating_tool"
    EXECUTING_SANDBOX = "executing_sandbox"
    ACCESSING_STORAGE = "accessing_storage"
    LOGGING = "logging"
    RECOVERING = "recovering"
    SHUTDOWN = "shutdown"


# ---------------------------------------------------------------------------
# FG3 / FG9 — Task & Action State
# ---------------------------------------------------------------------------


class TaskState(BaseState):
    """Task lifecycle (FG2 Task Manager / FG9 agents)."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


# Terminal task states — no further transition allowed.
TERMINAL_TASK_STATES: Final[FrozenSet[TaskState]] = frozenset(
    {TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED}
)


class ActionState(BaseState):
    """Windows Control action execution state (FG3)."""

    PENDING = "pending"
    QUEUED = "queued"
    EXECUTING_NATIVE = "executing_native"     # Tier 1: pywin32 / pywinauto
    EXECUTING_VISION = "executing_vision"     # Tier 2: YOLO + OCR
    EXECUTING_VLM = "executing_vlm"           # Tier 3: GoClick / Florence-2
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    ROLLING_BACK = "rolling_back"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# FG5 — GUI: Companion & Cursor State
# ---------------------------------------------------------------------------


class GuiMode(BaseState):
    """Active GUI mode (FG5)."""

    DASHBOARD = "dashboard"
    COMPANION_2D = "companion_2d"
    COMPANION_3D = "companion_3d"
    SMART_CURSOR = "smart_cursor"


class CharacterState(BaseState):
    """Desktop companion character state (FG5)."""

    IDLE = "idle"
    PLAYING = "playing"
    WALKING = "walking"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ALERT = "alert"
    SLEEPING = "sleeping"


# ---------------------------------------------------------------------------
# Connectivity State (drives online/offline routing in FG2/FG4)
# ---------------------------------------------------------------------------


class ConnectivityState(BaseState):
    """Network availability, used by Router and Search Manager."""

    ONLINE = "online"
    OFFLINE = "offline"
    LIMITED = "limited"     # Reachable but degraded / metered
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Registry of all state machines with their initial states
# ---------------------------------------------------------------------------

INITIAL_STATES: Final[Mapping[str, BaseState]] = MappingProxyType(
    {
        "app": AppState.CREATED,
        "voice": VoiceState.IDLE,
        "brain": BrainState.OFFLINE,
        "security": SecurityState.STARTING,
        "task": TaskState.QUEUED,
        "action": ActionState.PENDING,
        "gui": GuiMode.DASHBOARD,
        "character": CharacterState.IDLE,
        "connectivity": ConnectivityState.UNKNOWN,
    }
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def can_transition(current: AppState, target: AppState) -> bool:
    """Return True if `target` is a legal successor of `current` for AppState.

    Only the application lifecycle exposes a hard transition table here; other
    subsystems validate transitions inside their own `transitions.py` where the
    rules are richer and event-driven.
    """
    return target in APP_STATE_TRANSITIONS.get(current, frozenset())


def is_terminal_task_state(state: TaskState) -> bool:
    """Return True if a task can no longer change state."""
    return state in TERMINAL_TASK_STATES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "BaseState",
    "AppState",
    "APP_STATE_TRANSITIONS",
    "VoiceState",
    "BrainState",
    "SecurityState",
    "TaskState",
    "TERMINAL_TASK_STATES",
    "ActionState",
    "GuiMode",
    "CharacterState",
    "ConnectivityState",
    "INITIAL_STATES",
    "can_transition",
    "is_terminal_task_state",
]
