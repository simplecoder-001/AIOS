# app/core/event_bus/events/security_events.py

"""
Security events for AIOS.

These events coordinate authentication, authorization, permission
checks, AI firewall decisions, sandbox execution, secret protection,
and security incident handling.

Primary consumers:
    - fg6_security
    - fg1_voice_system
    - fg2_ai_brain
    - fg3_windows_control
    - fg7_plugins
    - fg9_agent_system
    - audit logger
    - telemetry system
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class SecurityEventType(StrEnum):
    """
    Canonical security event names.
    """

    # Identity
    AUTHENTICATION_STARTED = "security.authentication.started"
    AUTHENTICATION_SUCCEEDED = "security.authentication.succeeded"
    AUTHENTICATION_FAILED = "security.authentication.failed"

    AUTHORIZATION_GRANTED = "security.authorization.granted"
    AUTHORIZATION_REVOKED = "security.authorization.revoked"

    IDENTITY_VERIFICATION_STARTED = (
        "security.identity_verification.started"
    )
    IDENTITY_VERIFIED = "security.identity_verification.verified"
    IDENTITY_REJECTED = "security.identity_rejection"

    # Permissions
    PERMISSION_CHECK_STARTED = "security.permission.started"
    PERMISSION_GRANTED = "security.permission.granted"
    PERMISSION_DENIED = "security.permission.denied"

    # Risk
    RISK_ASSESSMENT_STARTED = "security.risk.started"
    RISK_ASSESSMENT_COMPLETED = "security.risk.completed"

    # Firewall
    FIREWALL_SCAN_STARTED = "security.firewall.started"
    FIREWALL_SCAN_COMPLETED = "security.firewall.completed"
    FIREWALL_BLOCKED = "security.firewall.blocked"

    # Sandbox
    SANDBOX_STARTED = "security.sandbox.started"
    SANDBOX_COMPLETED = "security.sandbox.completed"
    SANDBOX_VIOLATION = "security.sandbox.violation"

    # Secrets
    SECRET_ACCESS = "security.secret.access"
    SECRET_REDACTED = "security.secret.redacted"

    # Incidents
    SECURITY_ALERT = "security.alert"
    SECURITY_INCIDENT = "security.incident"

    # Audit
    AUDIT_EVENT = "security.audit"


@dataclass(slots=True, kw_only=True)
class SecurityEvent(Event):
    """
    Base security event.

    Shared metadata used by:
        - audit logging
        - event store
        - telemetry
        - security dashboard
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.SECURITY,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None


# ============================================================================
# Authentication Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AuthenticationStartedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.AUTHENTICATION_STARTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    provider: str | None = None


@dataclass(slots=True, kw_only=True)
class AuthenticationSucceededEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.AUTHENTICATION_SUCCEEDED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    identity_id: str
    provider: str | None = None


@dataclass(slots=True, kw_only=True)
class AuthenticationFailedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.AUTHENTICATION_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    reason: str
    provider: str | None = None


# ============================================================================
# Authorization Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AuthorizationGrantedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.AUTHORIZATION_GRANTED,
        init=False,
    )

    identity_id: str
    session_id: str | None = None


@dataclass(slots=True, kw_only=True)
class AuthorizationRevokedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.AUTHORIZATION_REVOKED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    identity_id: str
    reason: str


# ============================================================================
# Identity Verification Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class IdentityVerificationStartedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.IDENTITY_VERIFICATION_STARTED,
        init=False,
    )

    verification_type: str


@dataclass(slots=True, kw_only=True)
class IdentityVerifiedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.IDENTITY_VERIFIED,
        init=False,
    )

    identity_id: str
    confidence: float


@dataclass(slots=True, kw_only=True)
class IdentityRejectedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.IDENTITY_REJECTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    reason: str
    confidence: float | None = None


# ============================================================================
# Permission Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class PermissionCheckStartedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.PERMISSION_CHECK_STARTED,
        init=False,
    )

    permission: str
    resource: str | None = None


@dataclass(slots=True, kw_only=True)
class PermissionGrantedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.PERMISSION_GRANTED,
        init=False,
    )

    permission: str
    resource: str | None = None


@dataclass(slots=True, kw_only=True)
class PermissionDeniedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.PERMISSION_DENIED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    permission: str
    reason: str
    resource: str | None = None


# ============================================================================
# Risk Assessment Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class RiskAssessmentStartedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.RISK_ASSESSMENT_STARTED,
        init=False,
    )

    action: str


@dataclass(slots=True, kw_only=True)
class RiskAssessmentCompletedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.RISK_ASSESSMENT_COMPLETED,
        init=False,
    )

    action: str
    risk_level: str
    confidence: float | None = None


# ============================================================================
# AI Firewall Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class FirewallScanStartedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.FIREWALL_SCAN_STARTED,
        init=False,
    )

    source: str


@dataclass(slots=True, kw_only=True)
class FirewallScanCompletedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.FIREWALL_SCAN_COMPLETED,
        init=False,
    )

    source: str
    threats_detected: int = 0


@dataclass(slots=True, kw_only=True)
class FirewallBlockedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.FIREWALL_BLOCKED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    reason: str
    source: str
    rule: str | None = None


# ============================================================================
# Sandbox Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SandboxStartedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SANDBOX_STARTED,
        init=False,
    )

    operation: str


@dataclass(slots=True, kw_only=True)
class SandboxCompletedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SANDBOX_COMPLETED,
        init=False,
    )

    operation: str
    success: bool


@dataclass(slots=True, kw_only=True)
class SandboxViolationEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SANDBOX_VIOLATION,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    operation: str
    reason: str


# ============================================================================
# Secret Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SecretAccessEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SECRET_ACCESS,
        init=False,
    )

    secret_type: str
    accessor: str


@dataclass(slots=True, kw_only=True)
class SecretRedactedEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SECRET_REDACTED,
        init=False,
    )

    secret_type: str
    source: str


# ============================================================================
# Security Incident Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SecurityAlertEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SECURITY_ALERT,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    title: str
    message: str
    severity: str


@dataclass(slots=True, kw_only=True)
class SecurityIncidentEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.SECURITY_INCIDENT,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    incident_id: str
    title: str
    description: str
    severity: str
    recoverable: bool = True


# ============================================================================
# Audit Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AuditEvent(SecurityEvent):
    name: str = field(
        default=SecurityEventType.AUDIT_EVENT,
        init=False,
    )

    action: str
    actor: str
    outcome: str


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "SecurityEventType",
    "SecurityEvent",
    "AuthenticationStartedEvent",
    "AuthenticationSucceededEvent",
    "AuthenticationFailedEvent",
    "AuthorizationGrantedEvent",
    "AuthorizationRevokedEvent",
    "IdentityVerificationStartedEvent",
    "IdentityVerifiedEvent",
    "IdentityRejectedEvent",
    "PermissionCheckStartedEvent",
    "PermissionGrantedEvent",
    "PermissionDeniedEvent",
    "RiskAssessmentStartedEvent",
    "RiskAssessmentCompletedEvent",
    "FirewallScanStartedEvent",
    "FirewallScanCompletedEvent",
    "FirewallBlockedEvent",
    "SandboxStartedEvent",
    "SandboxCompletedEvent",
    "SandboxViolationEvent",
    "SecretAccessEvent",
    "SecretRedactedEvent",
    "SecurityAlertEvent",
    "SecurityIncidentEvent",
    "AuditEvent",
]