# app/core/event_bus/events/agent_events.py

"""
Agent events for AIOS.

These events power FG9 Agent System.

Responsibilities:
    - Goal management
    - Task planning
    - Task execution
    - Tool invocation
    - Approval workflows
    - Background execution
    - Rollback and recovery
    - Agent lifecycle management
    - Inter-agent communication

Design goals:
    - Immutable event objects
    - Event sourcing friendly
    - Correlation-aware execution
    - Supports multi-agent orchestration
    - Supports approval and recovery workflows
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class AgentEventType(StrEnum):
    """
    Canonical agent event names.
    """

    # Agent lifecycle
    AGENT_STARTED = "agent.started"
    AGENT_STOPPED = "agent.stopped"
    AGENT_PAUSED = "agent.paused"
    AGENT_RESUMED = "agent.resumed"

    # Goals
    GOAL_CREATED = "agent.goal.created"
    GOAL_UPDATED = "agent.goal.updated"
    GOAL_COMPLETED = "agent.goal.completed"
    GOAL_CANCELLED = "agent.goal.cancelled"

    # Planning
    PLAN_STARTED = "agent.plan.started"
    PLAN_GENERATED = "agent.plan.generated"
    PLAN_UPDATED = "agent.plan.updated"
    PLAN_FAILED = "agent.plan.failed"

    # Tasks
    TASK_CREATED = "agent.task.created"
    TASK_STARTED = "agent.task.started"
    TASK_COMPLETED = "agent.task.completed"
    TASK_FAILED = "agent.task.failed"
    TASK_CANCELLED = "agent.task.cancelled"

    # Tools
    TOOL_REQUESTED = "agent.tool.requested"
    TOOL_STARTED = "agent.tool.started"
    TOOL_COMPLETED = "agent.tool.completed"
    TOOL_FAILED = "agent.tool.failed"

    # Approval
    APPROVAL_REQUESTED = "agent.approval.requested"
    APPROVAL_GRANTED = "agent.approval.granted"
    APPROVAL_DENIED = "agent.approval.denied"

    # Background execution
    BACKGROUND_TASK_STARTED = "agent.background.started"
    BACKGROUND_TASK_COMPLETED = "agent.background.completed"
    BACKGROUND_TASK_FAILED = "agent.background.failed"

    # Recovery
    ROLLBACK_STARTED = "agent.rollback.started"
    ROLLBACK_COMPLETED = "agent.rollback.completed"
    RECOVERY_STARTED = "agent.recovery.started"
    RECOVERY_COMPLETED = "agent.recovery.completed"

    # Communication
    MESSAGE_SENT = "agent.message.sent"
    MESSAGE_RECEIVED = "agent.message.received"

    # Errors
    AGENT_ERROR = "agent.error"


@dataclass(slots=True, kw_only=True)
class AgentEvent(Event):
    """
    Base agent event.

    Shared metadata for all agent events.
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.AGENT,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None

    agent_id: str
    session_id: str | None = None


# ============================================================================
# Agent Lifecycle Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AgentStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.AGENT_STARTED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class AgentStoppedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.AGENT_STOPPED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class AgentPausedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.AGENT_PAUSED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class AgentResumedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.AGENT_RESUMED,
        init=False,
    )


# ============================================================================
# Goal Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class GoalCreatedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.GOAL_CREATED,
        init=False,
    )

    goal_id: str
    goal: str


@dataclass(slots=True, kw_only=True)
class GoalUpdatedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.GOAL_UPDATED,
        init=False,
    )

    goal_id: str
    goal: str


@dataclass(slots=True, kw_only=True)
class GoalCompletedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.GOAL_COMPLETED,
        init=False,
    )

    goal_id: str


@dataclass(slots=True, kw_only=True)
class GoalCancelledEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.GOAL_CANCELLED,
        init=False,
    )

    goal_id: str
    reason: str | None = None


# ============================================================================
# Planning Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class PlanStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.PLAN_STARTED,
        init=False,
    )

    goal_id: str


@dataclass(slots=True, kw_only=True)
class PlanGeneratedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.PLAN_GENERATED,
        init=False,
    )

    goal_id: str
    plan_id: str
    step_count: int


@dataclass(slots=True, kw_only=True)
class PlanUpdatedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.PLAN_UPDATED,
        init=False,
    )

    plan_id: str


@dataclass(slots=True, kw_only=True)
class PlanFailedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.PLAN_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    goal_id: str
    reason: str


# ============================================================================
# Task Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class TaskCreatedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TASK_CREATED,
        init=False,
    )

    task_id: str
    task_name: str


@dataclass(slots=True, kw_only=True)
class TaskStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TASK_STARTED,
        init=False,
    )

    task_id: str


@dataclass(slots=True, kw_only=True)
class TaskCompletedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TASK_COMPLETED,
        init=False,
    )

    task_id: str
    duration_ms: float | None = None


@dataclass(slots=True, kw_only=True)
class TaskFailedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TASK_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    task_id: str
    reason: str
    recoverable: bool = True


@dataclass(slots=True, kw_only=True)
class TaskCancelledEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TASK_CANCELLED,
        init=False,
    )

    task_id: str
    reason: str | None = None


# ============================================================================
# Tool Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ToolRequestedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TOOL_REQUESTED,
        init=False,
    )

    tool_name: str


@dataclass(slots=True, kw_only=True)
class ToolStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TOOL_STARTED,
        init=False,
    )

    tool_name: str


@dataclass(slots=True, kw_only=True)
class ToolCompletedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TOOL_COMPLETED,
        init=False,
    )

    tool_name: str
    duration_ms: float | None = None


@dataclass(slots=True, kw_only=True)
class ToolFailedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.TOOL_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    tool_name: str
    reason: str
    recoverable: bool = True


# ============================================================================
# Approval Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ApprovalRequestedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.APPROVAL_REQUESTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    action: str
    risk_level: str


@dataclass(slots=True, kw_only=True)
class ApprovalGrantedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.APPROVAL_GRANTED,
        init=False,
    )

    action: str


@dataclass(slots=True, kw_only=True)
class ApprovalDeniedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.APPROVAL_DENIED,
        init=False,
    )

    action: str
    reason: str | None = None


# ============================================================================
# Background Execution Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class BackgroundTaskStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.BACKGROUND_TASK_STARTED,
        init=False,
    )

    task_id: str


@dataclass(slots=True, kw_only=True)
class BackgroundTaskCompletedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.BACKGROUND_TASK_COMPLETED,
        init=False,
    )

    task_id: str


@dataclass(slots=True, kw_only=True)
class BackgroundTaskFailedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.BACKGROUND_TASK_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    task_id: str
    reason: str


# ============================================================================
# Recovery Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class RollbackStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.ROLLBACK_STARTED,
        init=False,
    )

    operation_id: str


@dataclass(slots=True, kw_only=True)
class RollbackCompletedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.ROLLBACK_COMPLETED,
        init=False,
    )

    operation_id: str
    success: bool


@dataclass(slots=True, kw_only=True)
class RecoveryStartedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.RECOVERY_STARTED,
        init=False,
    )

    reason: str


@dataclass(slots=True, kw_only=True)
class RecoveryCompletedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.RECOVERY_COMPLETED,
        init=False,
    )

    success: bool


# ============================================================================
# Communication Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class MessageSentEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.MESSAGE_SENT,
        init=False,
    )

    target_agent_id: str
    message_type: str


@dataclass(slots=True, kw_only=True)
class MessageReceivedEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.MESSAGE_RECEIVED,
        init=False,
    )

    source_agent_id: str
    message_type: str


# ============================================================================
# Error Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AgentErrorEvent(AgentEvent):
    name: str = field(
        default=AgentEventType.AGENT_ERROR,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
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
    "AgentEventType",
    "AgentEvent",
    "AgentStartedEvent",
    "AgentStoppedEvent",
    "AgentPausedEvent",
    "AgentResumedEvent",
    "GoalCreatedEvent",
    "GoalUpdatedEvent",
    "GoalCompletedEvent",
    "GoalCancelledEvent",
    "PlanStartedEvent",
    "PlanGeneratedEvent",
    "PlanUpdatedEvent",
    "PlanFailedEvent",
    "TaskCreatedEvent",
    "TaskStartedEvent",
    "TaskCompletedEvent",
    "TaskFailedEvent",
    "TaskCancelledEvent",
    "ToolRequestedEvent",
    "ToolStartedEvent",
    "ToolCompletedEvent",
    "ToolFailedEvent",
    "ApprovalRequestedEvent",
    "ApprovalGrantedEvent",
    "ApprovalDeniedEvent",
    "BackgroundTaskStartedEvent",
    "BackgroundTaskCompletedEvent",
    "BackgroundTaskFailedEvent",
    "RollbackStartedEvent",
    "RollbackCompletedEvent",
    "RecoveryStartedEvent",
    "RecoveryCompletedEvent",
    "MessageSentEvent",
    "MessageReceivedEvent",
    "AgentErrorEvent",
]