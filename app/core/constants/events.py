# app/core/constants/events.py
"""
Canonical event catalog for the AIOS Event Bus.

The Event Bus (app/core/event_bus/) is the backbone of the event-driven
architecture. Every subsystem publishes and subscribes to the events defined
here — never to raw string literals — so that names stay consistent across the
publisher, dispatcher, event store, and every subscriber.

Design rules:
    * `str`-based enums grouped by domain, mirroring event_bus/events/*.py.
    * A stable, hierarchical naming scheme: "<domain>.<subject>.<action>".
    * Priority and delivery-mode enums live here so middleware can reason about
      routing without importing feature-group code.
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Final, Mapping


# ---------------------------------------------------------------------------
# Event delivery semantics
# ---------------------------------------------------------------------------


class EventPriority(IntEnum):
    """Dispatch priority. Higher value = handled first by the dispatcher."""

    LOW = 10
    NORMAL = 20
    HIGH = 30
    CRITICAL = 40
    EMERGENCY = 50  # Interrupts, emergency stop, tamper detection


class EventDeliveryMode(str, Enum):
    """How the bus delivers an event to its subscribers."""

    SYNC = "sync"                # Blocking, in-order (used for state gates)
    ASYNC = "async"              # Fire-and-forget via asyncio
    BROADCAST = "broadcast"      # All subscribers, order not guaranteed
    QUEUED = "queued"            # Buffered through a priority queue


class EventCategory(str, Enum):
    """Top-level grouping used for filtering, routing, and audit."""

    SYSTEM = "system"
    LIFECYCLE = "lifecycle"
    VOICE = "voice"
    BRAIN = "brain"
    SECURITY = "security"
    GUI = "gui"
    AGENT = "agent"
    LEARNING = "learning"
    PLUGIN = "plugin"


# ---------------------------------------------------------------------------
# System events (core/telemetry, core/health_manager)
# ---------------------------------------------------------------------------


class SystemEvent(str, Enum):
    """Process-wide system and telemetry events."""

    HEARTBEAT = "system.core.heartbeat"
    HEALTH_CHECK = "system.health.check"
    HEALTH_DEGRADED = "system.health.degraded"
    HEALTH_RESTORED = "system.health.restored"
    RESOURCE_WARNING = "system.resource.warning"       # CPU/RAM/VRAM/disk
    RESOURCE_CRITICAL = "system.resource.critical"
    CONNECTIVITY_CHANGED = "system.network.connectivity_changed"
    CONFIG_RELOADED = "system.config.reloaded"
    HIGH_LOAD_MODE_ENABLED = "system.performance.high_load_enabled"
    HIGH_LOAD_MODE_DISABLED = "system.performance.high_load_disabled"


# ---------------------------------------------------------------------------
# Lifecycle events (app/bootstrap/*)
# ---------------------------------------------------------------------------


class LifecycleEvent(str, Enum):
    """Application and feature-group lifecycle events."""

    APP_BOOTSTRAP_STARTED = "lifecycle.app.bootstrap_started"
    APP_INITIALIZED = "lifecycle.app.initialized"
    APP_STARTED = "lifecycle.app.started"
    APP_PAUSED = "lifecycle.app.paused"
    APP_RESUMED = "lifecycle.app.resumed"
    APP_STOPPING = "lifecycle.app.stopping"
    APP_STOPPED = "lifecycle.app.stopped"
    SHUTDOWN_EVENT = "lifecycle.app.shutdown"

    FEATURE_GROUP_LOADING = "lifecycle.fg.loading"
    FEATURE_GROUP_READY = "lifecycle.fg.ready"
    FEATURE_GROUP_FAILED = "lifecycle.fg.failed"
    FEATURE_GROUP_STOPPED = "lifecycle.fg.stopped"

    STATE_CHANGED = "lifecycle.state.changed"


# ---------------------------------------------------------------------------
# FG1 — Voice events
# ---------------------------------------------------------------------------


class VoiceEvent(str, Enum):
    """Voice interaction events (FG1 Section 6)."""

    WAKE_TRIGGERED = "voice.wakeword.triggered"
    AUTHORIZED_EVENT = "voice.speaker.authorized"
    DEAUTHORIZED_EVENT = "voice.speaker.deauthorized"
    SPEECH_STARTED = "voice.vad.speech_started"
    SPEECH_ENDED = "voice.vad.speech_ended"
    TRANSCRIPTION_PARTIAL = "voice.stt.partial"
    TRANSCRIPTION_FINAL = "voice.stt.final"
    INTERRUPT_EVENT = "voice.interrupt.triggered"
    TTS_STARTED = "voice.tts.started"
    TTS_FINISHED = "voice.tts.finished"
    VOICE_STATE_CHANGED = "voice.state.changed"


# ---------------------------------------------------------------------------
# FG2 — AI Brain events
# ---------------------------------------------------------------------------


class BrainEvent(str, Enum):
    """AI Brain orchestration events (FG2)."""

    INTENT_CLASSIFIED = "brain.intent.classified"
    ROUTED = "brain.router.routed"
    CONTEXT_BUILT = "brain.context.built"
    SEARCH_REQUESTED = "brain.search.requested"
    SEARCH_COMPLETED = "brain.search.completed"
    PLAN_CREATED = "brain.planner.plan_created"
    TOOL_REQUESTED = "brain.tool.requested"
    TOOL_COMPLETED = "brain.tool.completed"
    TOOL_FAILED = "brain.tool.failed"
    EXECUTION_VERIFIED = "brain.verification.verified"
    MEMORY_UPDATED = "brain.memory.updated"
    RESPONSE_GENERATED = "brain.response.generated"
    BRAIN_STATE_CHANGED = "brain.state.changed"


# ---------------------------------------------------------------------------
# FG6 — Security events
# ---------------------------------------------------------------------------


class SecurityEvent(str, Enum):
    """Security and permission events (FG6 Event Architecture)."""

    AUTH_SUCCESS = "security.auth.success"
    AUTH_FAILURE = "security.auth.failure"
    PERMISSION_DENIED = "security.authz.permission_denied"
    RISK_ESCALATION = "security.risk.escalation"
    PROMPT_BLOCKED = "security.firewall.prompt_blocked"
    TOOL_VALIDATION_FAILED = "security.validation.tool_failed"
    SANDBOX_TIMEOUT = "security.sandbox.timeout"
    SANDBOX_VIOLATION = "security.sandbox.violation"
    ENCRYPTION_FAILURE = "security.encryption.failure"
    TAMPER_DETECTED = "security.audit.tamper_detected"
    DEVICE_VERIFICATION_FAILED = "security.hardware.device_failed"
    RECOVERY_STARTED = "security.recovery.started"
    RECOVERY_COMPLETED = "security.recovery.completed"


# ---------------------------------------------------------------------------
# FG5 — GUI events
# ---------------------------------------------------------------------------


class GuiEvent(str, Enum):
    """GUI and companion events (FG5 Event Architecture)."""

    VOICE_STATE_CHANGED = "gui.voice.state_changed"
    LANGUAGE_CHANGED = "gui.language.changed"
    SECURITY_ALERT = "gui.security.alert"
    SYSTEM_NOTIFICATION = "gui.notification.system"
    MODE_SWITCHED = "gui.mode.switched"
    CHARACTER_EVENT = "gui.character.event"
    CURSOR_EVENT = "gui.cursor.event"
    TUTORIAL_STARTED = "gui.tutorial.started"
    TUTORIAL_STOPPED = "gui.tutorial.stopped"
    EMERGENCY_EVENT = "gui.emergency.triggered"


# ---------------------------------------------------------------------------
# FG9 — Agent events
# ---------------------------------------------------------------------------


class AgentEvent(str, Enum):
    """Agent system events (FG9)."""

    AGENT_STARTED = "agent.task.started"
    AGENT_STEP_COMPLETED = "agent.task.step_completed"
    AGENT_PAUSED = "agent.task.paused"
    AGENT_RESUMED = "agent.task.resumed"
    AGENT_COMPLETED = "agent.task.completed"
    AGENT_CANCELLED = "agent.task.cancelled"
    AGENT_FAILED = "agent.task.failed"


# ---------------------------------------------------------------------------
# FG10 — Learning events
# ---------------------------------------------------------------------------


class LearningEvent(str, Enum):
    """Self-learning system events (FG10)."""

    EXPERIENCE_RECORDED = "learning.experience.recorded"
    PATTERN_DETECTED = "learning.pattern.detected"
    PROFILE_UPDATED = "learning.profile.updated"
    PATCH_PROPOSED = "learning.patch.proposed"
    PATCH_APPLIED = "learning.patch.applied"
    PATCH_ROLLED_BACK = "learning.patch.rolled_back"


# ---------------------------------------------------------------------------
# FG7 — Plugin events
# ---------------------------------------------------------------------------


class PluginEvent(str, Enum):
    """Plugin and extension events (FG7)."""

    PLUGIN_DISCOVERED = "plugin.lifecycle.discovered"
    PLUGIN_LOADED = "plugin.lifecycle.loaded"
    PLUGIN_UNLOADED = "plugin.lifecycle.unloaded"
    PLUGIN_RELOADED = "plugin.lifecycle.reloaded"
    PLUGIN_ERROR = "plugin.lifecycle.error"
    PLUGIN_KILLED = "plugin.security.killed"  # Emergency kill switch


# ---------------------------------------------------------------------------
# Category mapping + default priorities
# ---------------------------------------------------------------------------

EVENT_ENUM_BY_CATEGORY: Final[Mapping[EventCategory, type[Enum]]] = MappingProxyType(
    {
        EventCategory.SYSTEM: SystemEvent,
        EventCategory.LIFECYCLE: LifecycleEvent,
        EventCategory.VOICE: VoiceEvent,
        EventCategory.BRAIN: BrainEvent,
        EventCategory.SECURITY: SecurityEvent,
        EventCategory.GUI: GuiEvent,
        EventCategory.AGENT: AgentEvent,
        EventCategory.LEARNING: LearningEvent,
        EventCategory.PLUGIN: PluginEvent,
    }
)

# Events that must always be dispatched at EMERGENCY priority regardless of
# their category default. The bus middleware consults this set first.
EMERGENCY_EVENTS: Final[frozenset[str]] = frozenset(
    {
        VoiceEvent.INTERRUPT_EVENT.value,
        SecurityEvent.TAMPER_DETECTED.value,
        SecurityEvent.RISK_ESCALATION.value,
        GuiEvent.EMERGENCY_EVENT.value,
        PluginEvent.PLUGIN_KILLED.value,
        LifecycleEvent.SHUTDOWN_EVENT.value,
    }
)


def default_priority(event_value: str) -> EventPriority:
    """Return the recommended dispatch priority for an event string."""
    if event_value in EMERGENCY_EVENTS:
        return EventPriority.EMERGENCY
    if event_value.startswith("security."):
        return EventPriority.HIGH
    if event_value.startswith("system.resource"):
        return EventPriority.HIGH
    return EventPriority.NORMAL


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "EventPriority",
    "EventDeliveryMode",
    "EventCategory",
    "SystemEvent",
    "LifecycleEvent",
    "VoiceEvent",
    "BrainEvent",
    "SecurityEvent",
    "GuiEvent",
    "AgentEvent",
    "LearningEvent",
    "PluginEvent",
    "EVENT_ENUM_BY_CATEGORY",
    "EMERGENCY_EVENTS",
    "default_priority",
]
