# app/core/database/models/audit.py
"""
Declarative schema for the AIOS audit database (SQLite — shared metadata store).

FG6 §9 Audit & Tamper Protection requires complete accountability. Every
security-sensitive action — auth events, permission checks, risk assessments,
firewall blocks, sandbox executions, tamper detections, recovery operations —
is recorded in an append-only audit ledger whose integrity is verified via
SHA-256 hash chaining and HMAC (see ``app/logging/audit_logger``).

Tables defined here live in the metadata database (single shared ``*.db`` file)
but are grouped into an explicit ``AUDIT_SCHEMA`` so the migration manager can
order their creation before the metadata tables, and so the audit subsystem
(FG6) can opt-in to a separate encrypted store when required.

Tables
------
    audit_records          — append-only structured audit trail
    audit_integrity        — chain-of-trust HMAC checkpoints
    recovery_operations    — recovery manager decisions (FG6 §10)
    forensic_exports       — export metadata for offline forensic review

Every record carries:
    * ``id``                  — monotonic INTEGER PRIMARY KEY
    * ``timestamp``           — ISO-8601 UTC, indexed for time-range queries
    * ``actor``               — user/subsystem that initiated the action
    * ``trace_id``            — cross-correlation id into the event bus
    * ``hash_prev`` / ``hash_curr`` — SHA-256 chain fields
    * ``hmac``                — message authentication over the canonical row
    * ``payload``             — JSON witness data (filtered by FG6 firewall)
"""

from __future__ import annotations

from app.core.database.models.base import (
    Affinity,
    Column,
    ForeignKey,
    Index,
    OnDelete,
    Schema,
    Table,
)

__all__ = [
    "AUDIT_SCHEMA_NAME",
    "AUDIT_SCHEMA_VERSION",
    "AuditTableNames",
    "AUDIT_RECORDS",
    "AUDIT_INTEGRITY",
    "RECOVERY_OPERATIONS",
    "FORENSIC_EXPORTS",
    "AUDIT_SCHEMA",
]


AUDIT_SCHEMA_NAME: str = "aios_audit"
AUDIT_SCHEMA_VERSION: int = 1


class AuditTableNames:
    """Canonical audit-table-name constants used by FG6 and the audit logger."""

    AUDIT_RECORDS = "audit_records"
    AUDIT_INTEGRITY = "audit_integrity"
    RECOVERY_OPERATIONS = "recovery_operations"
    FORENSIC_EXPORTS = "forensic_exports"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


AUDIT_RECORDS = Table(
    name=AuditTableNames.AUDIT_RECORDS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="timestamp", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="category", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="event_type", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="severity", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="actor", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="actor_role", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="source", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="command", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="risk_level", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="confidence", type=float, affinity=Affinity.REAL, nullable=False, default=0.0),
        Column(name="execution_path", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="rollback_status", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="result_status", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="trace_id", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="correlation_id", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="payload", type=str, affinity=Affinity.TEXT, nullable=False),  # JSON witness
        Column(name="context", type=str, affinity=Affinity.TEXT, nullable=True),   # JSON
        Column(name="hash_prev", type=str, affinity=Affinity.TEXT, nullable=False, default="GENESIS"),
        Column(name="hash_curr", type=str, affinity=Affinity.TEXT, nullable=False, unique=True, index=True),
        Column(name="hmac", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
    indexes=(
        # FG6 audit queries are dominated by (category, timestamp) and
        # (event_type, severity, timestamp) range scans.
        Index(name="idx_audit_category_timestamp",
              table=AuditTableNames.AUDIT_RECORDS,
              columns=("category", "timestamp")),
        Index(name="idx_audit_event_severity_timestamp",
              table=AuditTableNames.AUDIT_RECORDS,
              columns=("event_type", "severity", "timestamp")),
        Index(name="idx_audit_actor_timestamp",
              table=AuditTableNames.AUDIT_RECORDS,
              columns=("actor", "timestamp")),
    ),
)


AUDIT_INTEGRITY = Table(
    name=AuditTableNames.AUDIT_INTEGRITY,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="checkpoint_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="last_audit_id", type=int, affinity=Affinity.INTEGER, nullable=False,
               foreign_key=ForeignKey(
                   table=AuditTableNames.AUDIT_RECORDS,
                   column="id",
                   on_delete=OnDelete.RESTRICT,
               )),
        Column(name="last_hash_curr", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="chain_hash", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="hmac", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="verified", type=bool, affinity=Affinity.INTEGER, nullable=False, default=True, index=True),
        Column(name="verification_error", type=str, affinity=Affinity.TEXT, nullable=True),
    ),
    indexes=(
        Index(name="idx_audit_integrity_verified",
              table=AuditTableNames.AUDIT_INTEGRITY,
              columns=("verified",)),
    ),
)


RECOVERY_OPERATIONS = Table(
    name=AuditTableNames.RECOVERY_OPERATIONS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="operation", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="trigger", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="previous_state", type=str, affinity=Affinity.TEXT, nullable=True),  # JSON snapshot id
        Column(name="restored_state", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="status", type=str, affinity=Affinity.TEXT, nullable=False, default="started", index=True),
        Column(name="rollback_id", type=str, affinity=Affinity.TEXT, nullable=True, unique=True, index=True),
        Column(name="actor", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="trace_id", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="started_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="completed_at", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="metadata", type=str, affinity=Affinity.TEXT, nullable=True),  # JSON
    ),
    indexes=(
        Index(name="idx_recovery_status_started",
              table=AuditTableNames.RECOVERY_OPERATIONS,
              columns=("status", "started_at")),
    ),
)


FORENSIC_EXPORTS = Table(
    name=AuditTableNames.FORENSIC_EXPORTS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="requested_by", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="format", type=str, affinity=Affinity.TEXT, nullable=False, default="json"),
        Column(name="range_start", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="range_end", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="record_count", type=int, affinity=Affinity.INTEGER, nullable=False, default=0),
        Column(name="file_path", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="file_hash", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="signature", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="trace_id", type=str, affinity=Affinity.TEXT, nullable=True),
    ),
)


# ---------------------------------------------------------------------------
# Schema bundle
# ---------------------------------------------------------------------------


AUDIT_SCHEMA: Schema = Schema(
    name=AUDIT_SCHEMA_NAME,
    version=AUDIT_SCHEMA_VERSION,
    event_source="app.core.database.audit",
    tables=(
        AUDIT_RECORDS,
        AUDIT_INTEGRITY,
        RECOVERY_OPERATIONS,
        FORENSIC_EXPORTS,
    ),
)


# ---------------------------------------------------------------------------
# Import-time integrity: ensure every FK target exists in the schema.
# ---------------------------------------------------------------------------


assert {t.name for t in AUDIT_SCHEMA.tables}.issuperset(
    {col.foreign_key.table
     for t in AUDIT_SCHEMA.tables
     for col in t.columns
     if col.foreign_key is not None}
), "Foreign-key target missing from AUDIT_SCHEMA"
