# app/core/constants/permissions.py
"""
Authorization, capability, and risk constants for AIOS.

This module is the source of truth for the security vocabulary used by:
    * FG6 Layer 2 — Authorization & Permission Engine (Casbin roles/policies)
    * FG6 Layer 3 — Dynamic Risk Engine (risk levels & escalation)
    * FG2 Tool Manager — per-tool permission requirements
    * FG3 Permission & Adaptive Risk Evaluation
    * FG7 Plugin Permission Engine — capability-based access control

The concrete, editable policy lives in `configs/permissions.yaml`; the values
here define the *closed set* of roles, capabilities, and risk levels that any
policy is allowed to reference. Keeping this closed set in code lets the
validator reject unknown roles/capabilities at load time.

Design rules:
    * `str`/`IntEnum` enums → serialize cleanly and compare by rank.
    * Immutable grant maps frozen with MappingProxyType.
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Final, FrozenSet, Mapping


# ---------------------------------------------------------------------------
# Roles (FG6 Layer 2)
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """Authorization roles. Ranked via ROLE_RANK below."""

    GUEST = "guest"
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"
    SYSTEM = "system"  # Internal principal; never assigned to a human


# Higher rank = more privilege. Used for hierarchical permission inheritance:
# a role implicitly holds every capability granted to lower-ranked roles.
ROLE_RANK: Final[Mapping[Role, int]] = MappingProxyType(
    {
        Role.GUEST: 0,
        Role.USER: 10,
        Role.ADMIN: 20,
        Role.SUPER_ADMIN: 30,
        Role.SYSTEM: 40,
    }
)

DEFAULT_ROLE: Final[Role] = Role.GUEST


# ---------------------------------------------------------------------------
# Capabilities (fine-grained, capability-based access control)
# ---------------------------------------------------------------------------


class Capability(str, Enum):
    """Fine-grained capabilities gating every privileged action.

    Naming: "<domain>:<action>". Plugins and tools request these explicitly;
    the Permission Engine grants them per role (see ROLE_CAPABILITIES).
    """

    # Conversation / knowledge
    CONVERSE = "conversation:converse"
    KNOWLEDGE_QUERY = "knowledge:query"
    REALTIME_SEARCH = "search:realtime"

    # Memory
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    PERSONAL_MEMORY_READ = "memory:personal_read"     # Encrypted, offline only
    PERSONAL_MEMORY_WRITE = "memory:personal_write"

    # Windows system control (FG3)
    SYSTEM_CONTROL = "system:control"
    FILE_READ = "file:read"
    FILE_WRITE = "file:write"
    FILE_DELETE = "file:delete"
    PROCESS_MANAGE = "process:manage"
    REGISTRY_EDIT = "registry:edit"
    POWER_MANAGE = "power:manage"                     # Shutdown/restart/sleep

    # Automation / scheduling
    AUTOMATION_RUN = "automation:run"
    SCHEDULE_TASK = "schedule:task"

    # Security / administration
    MANAGE_USERS = "security:manage_users"
    MANAGE_PERMISSIONS = "security:manage_permissions"
    VIEW_AUDIT_LOG = "security:view_audit"
    SECURITY_COMMAND = "security:command"

    # Plugins (FG7)
    PLUGIN_INSTALL = "plugin:install"
    PLUGIN_UNINSTALL = "plugin:uninstall"
    PLUGIN_NETWORK = "plugin:network"                 # Admin-gated egress

    # Settings / configuration
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"


# Capabilities considered inherently dangerous. The Risk Engine forces a
# minimum risk level and, typically, an explicit confirmation for these.
HIGH_RISK_CAPABILITIES: Final[FrozenSet[Capability]] = frozenset(
    {
        Capability.FILE_DELETE,
        Capability.REGISTRY_EDIT,
        Capability.PROCESS_MANAGE,
        Capability.POWER_MANAGE,
        Capability.MANAGE_USERS,
        Capability.MANAGE_PERMISSIONS,
        Capability.PLUGIN_INSTALL,
        Capability.PLUGIN_UNINSTALL,
        Capability.SECURITY_COMMAND,
    }
)


# ---------------------------------------------------------------------------
# Default role → capability grants
# ---------------------------------------------------------------------------
# These are the *baseline* grants. `configs/permissions.yaml` may narrow or
# extend them, but may only reference capabilities defined above. Grants are
# additive with role rank: a higher role inherits everything below it via
# `resolve_capabilities()`.

_GUEST_CAPS: Final[FrozenSet[Capability]] = frozenset(
    {
        Capability.CONVERSE,
        Capability.KNOWLEDGE_QUERY,
        Capability.SETTINGS_READ,
    }
)

_USER_CAPS: Final[FrozenSet[Capability]] = frozenset(
    {
        Capability.REALTIME_SEARCH,
        Capability.MEMORY_READ,
        Capability.MEMORY_WRITE,
        Capability.PERSONAL_MEMORY_READ,
        Capability.PERSONAL_MEMORY_WRITE,
        Capability.FILE_READ,
        Capability.AUTOMATION_RUN,
        Capability.SCHEDULE_TASK,
    }
)

_ADMIN_CAPS: Final[FrozenSet[Capability]] = frozenset(
    {
        Capability.SYSTEM_CONTROL,
        Capability.FILE_WRITE,
        Capability.FILE_DELETE,
        Capability.PROCESS_MANAGE,
        Capability.POWER_MANAGE,
        Capability.PLUGIN_INSTALL,
        Capability.PLUGIN_UNINSTALL,
        Capability.PLUGIN_NETWORK,
        Capability.VIEW_AUDIT_LOG,
        Capability.SETTINGS_WRITE,
    }
)

_SUPER_ADMIN_CAPS: Final[FrozenSet[Capability]] = frozenset(
    {
        Capability.REGISTRY_EDIT,
        Capability.MANAGE_USERS,
        Capability.MANAGE_PERMISSIONS,
        Capability.SECURITY_COMMAND,
    }
)

# SYSTEM holds every capability by definition (internal principal only).
_SYSTEM_CAPS: Final[FrozenSet[Capability]] = frozenset(Capability)

# Direct (non-inherited) grants per role.
ROLE_CAPABILITIES: Final[Mapping[Role, FrozenSet[Capability]]] = MappingProxyType(
    {
        Role.GUEST: _GUEST_CAPS,
        Role.USER: _USER_CAPS,
        Role.ADMIN: _ADMIN_CAPS,
        Role.SUPER_ADMIN: _SUPER_ADMIN_CAPS,
        Role.SYSTEM: _SYSTEM_CAPS,
    }
)


# ---------------------------------------------------------------------------
# Risk model (FG6 Layer 3 / FG3 Step 3)
# ---------------------------------------------------------------------------


class RiskLevel(IntEnum):
    """Execution risk. Ranked so comparisons and thresholds are trivial."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


# Confidence policy from FG2 Section 26 / FG3 Step 4. Above HIGH → execute;
# in the band → permission check; below LOW → ask the user.
CONFIDENCE_AUTO_EXECUTE: Final[float] = 0.90
CONFIDENCE_ASK_USER: Final[float] = 0.60


class PermissionDecision(str, Enum):
    """Outcome of an authorization + risk evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REQUIRE_ELEVATION = "require_elevation"     # Step up to higher role/MFA


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def resolve_capabilities(role: Role) -> FrozenSet[Capability]:
    """Return the full capability set for a role, including inherited grants.

    A role inherits every capability granted to all lower-ranked roles.
    """
    rank = ROLE_RANK[role]
    resolved: set[Capability] = set()
    for candidate, caps in ROLE_CAPABILITIES.items():
        if ROLE_RANK[candidate] <= rank:
            resolved |= caps
    return frozenset(resolved)


def role_has_capability(role: Role, capability: Capability) -> bool:
    """Return True if `role` (with inheritance) holds `capability`."""
    return capability in resolve_capabilities(role)


def is_high_risk(capability: Capability) -> bool:
    """Return True if the capability is inherently high risk."""
    return capability in HIGH_RISK_CAPABILITIES


def role_at_least(role: Role, minimum: Role) -> bool:
    """Return True if `role` ranks at or above `minimum`."""
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "Role",
    "ROLE_RANK",
    "DEFAULT_ROLE",
    "Capability",
    "HIGH_RISK_CAPABILITIES",
    "ROLE_CAPABILITIES",
    "RiskLevel",
    "CONFIDENCE_AUTO_EXECUTE",
    "CONFIDENCE_ASK_USER",
    "PermissionDecision",
    "resolve_capabilities",
    "role_has_capability",
    "is_high_risk",
    "role_at_least",
]
