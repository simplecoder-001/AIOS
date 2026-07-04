# app/core/event_bus/events/learning_events.py

"""
Learning events for AIOS.

These events power FG10 Self-Learning System.

Responsibilities:
    - Experience collection
    - Memory formation
    - User preference learning
    - Pattern discovery
    - Knowledge graph updates
    - Benchmark execution
    - Experiment tracking
    - Model adaptation
    - Analytics generation
    - Patch generation and rollback

Design goals:
    - Immutable event objects
    - Event sourcing friendly
    - Replayable learning pipeline
    - Supports online and offline learning
    - Supports analytics and benchmarking
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class LearningEventType(StrEnum):
    """
    Canonical learning event names.
    """

    # Experience
    EXPERIENCE_CAPTURED = "learning.experience.captured"
    EXPERIENCE_STORED = "learning.experience.stored"

    # Memory
    MEMORY_CREATED = "learning.memory.created"
    MEMORY_UPDATED = "learning.memory.updated"
    MEMORY_REMOVED = "learning.memory.removed"

    # Preferences
    PREFERENCE_LEARNED = "learning.preference.learned"
    PREFERENCE_UPDATED = "learning.preference.updated"

    # Pattern Discovery
    PATTERN_DETECTED = "learning.pattern.detected"
    INSIGHT_GENERATED = "learning.insight.generated"

    # Knowledge Graph
    KNOWLEDGE_GRAPH_UPDATED = (
        "learning.knowledge_graph.updated"
    )

    # Benchmark
    BENCHMARK_STARTED = "learning.benchmark.started"
    BENCHMARK_COMPLETED = "learning.benchmark.completed"

    # Experiments
    EXPERIMENT_STARTED = "learning.experiment.started"
    EXPERIMENT_COMPLETED = "learning.experiment.completed"
    EXPERIMENT_FAILED = "learning.experiment.failed"

    # Adaptation
    MODEL_ADAPTATION_STARTED = (
        "learning.model_adaptation.started"
    )
    MODEL_ADAPTATION_COMPLETED = (
        "learning.model_adaptation.completed"
    )

    # Analytics
    ANALYTICS_GENERATED = "learning.analytics.generated"

    # Patches
    PATCH_GENERATED = "learning.patch.generated"
    PATCH_APPLIED = "learning.patch.applied"
    PATCH_ROLLED_BACK = "learning.patch.rolled_back"

    # Errors
    LEARNING_ERROR = "learning.error"


@dataclass(slots=True, kw_only=True)
class LearningEvent(Event):
    """
    Base learning event.

    Shared metadata for all learning events.
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.LEARNING,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None


# ============================================================================
# Experience Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ExperienceCapturedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.EXPERIENCE_CAPTURED,
        init=False,
    )

    experience_id: str
    source: str
    experience_type: str


@dataclass(slots=True, kw_only=True)
class ExperienceStoredEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.EXPERIENCE_STORED,
        init=False,
    )

    experience_id: str


# ============================================================================
# Memory Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class MemoryCreatedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.MEMORY_CREATED,
        init=False,
    )

    memory_id: str
    memory_type: str


@dataclass(slots=True, kw_only=True)
class MemoryUpdatedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.MEMORY_UPDATED,
        init=False,
    )

    memory_id: str


@dataclass(slots=True, kw_only=True)
class MemoryRemovedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.MEMORY_REMOVED,
        init=False,
    )

    memory_id: str


# ============================================================================
# Preference Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class PreferenceLearnedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.PREFERENCE_LEARNED,
        init=False,
    )

    preference_key: str
    confidence: float


@dataclass(slots=True, kw_only=True)
class PreferenceUpdatedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.PREFERENCE_UPDATED,
        init=False,
    )

    preference_key: str
    confidence: float


# ============================================================================
# Pattern Discovery Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class PatternDetectedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.PATTERN_DETECTED,
        init=False,
    )

    pattern_id: str
    pattern_type: str
    confidence: float


@dataclass(slots=True, kw_only=True)
class InsightGeneratedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.INSIGHT_GENERATED,
        init=False,
    )

    insight_id: str
    category_name: str


# ============================================================================
# Knowledge Graph Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class KnowledgeGraphUpdatedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.KNOWLEDGE_GRAPH_UPDATED,
        init=False,
    )

    node_count: int | None = None
    relationship_count: int | None = None


# ============================================================================
# Benchmark Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class BenchmarkStartedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.BENCHMARK_STARTED,
        init=False,
    )

    benchmark_id: str
    benchmark_name: str


@dataclass(slots=True, kw_only=True)
class BenchmarkCompletedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.BENCHMARK_COMPLETED,
        init=False,
    )

    benchmark_id: str
    score: float
    duration_ms: float | None = None


# ============================================================================
# Experiment Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ExperimentStartedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.EXPERIMENT_STARTED,
        init=False,
    )

    experiment_id: str
    experiment_name: str


@dataclass(slots=True, kw_only=True)
class ExperimentCompletedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.EXPERIMENT_COMPLETED,
        init=False,
    )

    experiment_id: str
    success: bool


@dataclass(slots=True, kw_only=True)
class ExperimentFailedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.EXPERIMENT_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    experiment_id: str
    reason: str


# ============================================================================
# Model Adaptation Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ModelAdaptationStartedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.MODEL_ADAPTATION_STARTED,
        init=False,
    )

    adaptation_id: str
    model_name: str


@dataclass(slots=True, kw_only=True)
class ModelAdaptationCompletedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.MODEL_ADAPTATION_COMPLETED,
        init=False,
    )

    adaptation_id: str
    model_name: str
    success: bool


# ============================================================================
# Analytics Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AnalyticsGeneratedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.ANALYTICS_GENERATED,
        init=False,
    )

    report_id: str
    report_type: str


# ============================================================================
# Patch Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class PatchGeneratedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.PATCH_GENERATED,
        init=False,
    )

    patch_id: str
    patch_type: str


@dataclass(slots=True, kw_only=True)
class PatchAppliedEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.PATCH_APPLIED,
        init=False,
    )

    patch_id: str


@dataclass(slots=True, kw_only=True)
class PatchRolledBackEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.PATCH_ROLLED_BACK,
        init=False,
    )

    patch_id: str
    reason: str | None = None


# ============================================================================
# Error Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class LearningErrorEvent(LearningEvent):
    name: str = field(
        default=LearningEventType.LEARNING_ERROR,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    component: str
    message: str
    exception_type: str | None = None
    recoverable: bool = True


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "LearningEventType",
    "LearningEvent",
    "ExperienceCapturedEvent",
    "ExperienceStoredEvent",
    "MemoryCreatedEvent",
    "MemoryUpdatedEvent",
    "MemoryRemovedEvent",
    "PreferenceLearnedEvent",
    "PreferenceUpdatedEvent",
    "PatternDetectedEvent",
    "InsightGeneratedEvent",
    "KnowledgeGraphUpdatedEvent",
    "BenchmarkStartedEvent",
    "BenchmarkCompletedEvent",
    "ExperimentStartedEvent",
    "ExperimentCompletedEvent",
    "ExperimentFailedEvent",
    "ModelAdaptationStartedEvent",
    "ModelAdaptationCompletedEvent",
    "AnalyticsGeneratedEvent",
    "PatchGeneratedEvent",
    "PatchAppliedEvent",
    "PatchRolledBackEvent",
    "LearningErrorEvent",
]