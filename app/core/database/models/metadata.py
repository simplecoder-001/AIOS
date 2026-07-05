# app/core/database/models/metadata.py
"""
Declarative schema for the AIOS metadata database (SQLite).

The metadata store (``METADATA_DB_FILE``) holds every relational table required
by the assistant. The full table catalogue is defined by FG2 §20:

    admins
    permissions
    memory_logs
    conversation_history
    scheduled_tasks
    tool_history
    assistant_settings
    security_events
    search_cache
    voice_profiles
    installed_models
    plugin_registry
    schema_migrations   (internal — used by the migration manager)

Design rules
------------
* Pure declarative: no sqlite3 imports, no DDL side effects. The migration
  manager emits the DDL through the SQLite engine via ``METADATA_SCHEMA.all_ddl()``.
* Foreign keys are declared explicitly so schema ordering via ``ordered_tables``
  produces parent-before-child DDL on a fresh database.
* Each column declares a Python type and, where helpful, an explicit affinity.
* Timestamps are stored as ISO-8601 TEXT (UTC) — explicit affinity prevents
  accidental NUMERIC coercion.
* JSON columns use TEXT affinity; serialization is the repository's job.

Dependency order
----------------
constants → exceptions → models.base → here.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    "METADATA_SCHEMA_NAME",
    "METADATA_SCHEMA_VERSION",
    "TableNames",
    "ADMINS",
    "PERMISSIONS",
    "MEMORY_LOGS",
    "CONVERSATION_HISTORY",
    "SCHEDULED_TASKS",
    "TOOL_HISTORY",
    "ASSISTANT_SETTINGS",
    "SECURITY_EVENTS",
    "SEARCH_CACHE",
    "VOICE_PROFILES",
    "INSTALLED_MODELS",
    "PLUGIN_REGISTRY",
    "SCHEMA_MIGRATIONS",
    "METADATA_SCHEMA",
]


METADATA_SCHEMA_NAME: str = "aios_metadata"
METADATA_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Identifier catalog (single source of truth, used by repositories)
# ---------------------------------------------------------------------------


class TableNames:
    """Canonical table-name constants used across repositories."""

    ADMINS = "admins"
    PERMISSIONS = "permissions"
    MEMORY_LOGS = "memory_logs"
    CONVERSATION_HISTORY = "conversation_history"
    SCHEDULED_TASKS = "scheduled_tasks"
    TOOL_HISTORY = "tool_history"
    ASSISTANT_SETTINGS = "assistant_settings"
    SECURITY_EVENTS = "security_events"
    SEARCH_CACHE = "search_cache"
    VOICE_PROFILES = "voice_profiles"
    INSTALLED_MODELS = "installed_models"
    PLUGIN_REGISTRY = "plugin_registry"
    SCHEMA_MIGRATIONS = "schema_migrations"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


ADMINS = Table(
    name=TableNames.ADMINS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="username", type=str, affinity=Affinity.TEXT, nullable=False, unique=True, index=True),
        Column(name="role", type=str, affinity=Affinity.TEXT, nullable=False, default="admin"),
        Column(name="password_hash", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="mfa_secret_enc", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="is_active", type=bool, affinity=Affinity.INTEGER, nullable=False, default=False),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="updated_at", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="last_login_at", type=str, affinity=Affinity.TEXT, nullable=True),
    ),
)


PERMISSIONS = Table(
    name=TableNames.PERMISSIONS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="admin_id", type=int, affinity=Affinity.INTEGER, nullable=False,
               foreign_key=ForeignKey(table=TableNames.ADMINS, column="id", on_delete=OnDelete.CASCADE)),
        Column(name="permission", type=str, affinity=Affinity.TEXT, nullable=False, unique=False, index=True),
        Column(name="scope", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="granted_by", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="granted_at", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
    indexes=(
        Index(name="idx_permissions_admin_permission",
              table=TableNames.PERMISSIONS,
              columns=("admin_id", "permission"),
              unique=True),
    ),
)


MEMORY_LOGS = Table(
    name=TableNames.MEMORY_LOGS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="memory_type", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="importance", type=int, affinity=Affinity.INTEGER, nullable=False, default=2),
        Column(name="confidence", type=float, affinity=Affinity.REAL, nullable=False, default=0.0),
        Column(name="version", type=int, affinity=Affinity.INTEGER, nullable=False, default=1),
        Column(name="active", type=bool, affinity=Affinity.INTEGER, nullable=False, default=True),
        Column(name="encrypted", type=bool, affinity=Affinity.INTEGER, nullable=False, default=False),
        Column(name="payload", type=str, affinity=Affinity.TEXT, nullable=False),  # JSON
        Column(name="metadata", type=str, affinity=Affinity.TEXT, nullable=True),   # JSON
        Column(name="source", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="trace_id", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="rollback_id", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="updated_at", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="expires_at", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
    ),
    indexes=(
        Index(name="idx_memory_type_active_importance",
              table=TableNames.MEMORY_LOGS,
              columns=("memory_type", "active", "importance")),
        Index(name="idx_memory_trace_id",
              table=TableNames.MEMORY_LOGS,
              columns=("trace_id",)),
    ),
)


CONVERSATION_HISTORY = Table(
    name=TableNames.CONVERSATION_HISTORY,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="session_id", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="role", type=str, affinity=Affinity.TEXT, nullable=False),  # user/assistant/system
        Column(name="text", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="language", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="intent", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="metadata", type=str, affinity=Affinity.TEXT, nullable=True),  # JSON
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
    ),
    indexes=(
        Index(name="idx_conv_session_created",
              table=TableNames.CONVERSATION_HISTORY,
              columns=("session_id", "created_at")),
    ),
)


SCHEDULED_TASKS = Table(
    name=TableNames.SCHEDULED_TASKS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="name", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="cron", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="next_run_at", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="payload", type=str, affinity=Affinity.TEXT, nullable=False),  # JSON
        Column(name="status", type=str, affinity=Affinity.TEXT, nullable=False, default="queued", index=True),
        Column(name="priority", type=int, affinity=Affinity.INTEGER, nullable=False, default=2),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="updated_at", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
    indexes=(
        Index(name="idx_scheduled_status_nextrun",
              table=TableNames.SCHEDULED_TASKS,
              columns=("status", "next_run_at")),
    ),
)


TOOL_HISTORY = Table(
    name=TableNames.TOOL_HISTORY,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="tool_name", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="request", type=str, affinity=Affinity.TEXT, nullable=False),   # JSON
        Column(name="response", type=str, affinity=Affinity.TEXT, nullable=True),   # JSON
        Column(name="status", type=str, affinity=Affinity.TEXT, nullable=False, default="success", index=True),
        Column(name="confidence", type=float, affinity=Affinity.REAL, nullable=False, default=0.0),
        Column(name="risk_level", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="execution_time_ms", type=float, affinity=Affinity.REAL, nullable=False, default=0.0),
        Column(name="trace_id", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
    ),
)


ASSISTANT_SETTINGS = Table(
    name=TableNames.ASSISTANT_SETTINGS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="key", type=str, affinity=Affinity.TEXT, nullable=False, unique=True, index=True),
        Column(name="value", type=str, affinity=Affinity.TEXT, nullable=False),    # JSON-encoded
        Column(name="category", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="updated_at", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
)


SECURITY_EVENTS = Table(
    name=TableNames.SECURITY_EVENTS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="event_type", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="severity", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="user", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="command", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="risk_level", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="confidence", type=float, affinity=Affinity.REAL, nullable=False, default=0.0),
        Column(name="execution_path", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="rollback_status", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="payload", type=str, affinity=Affinity.TEXT, nullable=True),  # JSON
        Column(name="trace_id", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
    ),
    indexes=(
        Index(name="idx_security_event_type_severity_created",
              table=TableNames.SECURITY_EVENTS,
              columns=("event_type", "severity", "created_at")),
    ),
)


SEARCH_CACHE = Table(
    name=TableNames.SEARCH_CACHE,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="query_hash", type=str, affinity=Affinity.TEXT, nullable=False, unique=True, index=True),
        Column(name="query_normalized", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="provider", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
        Column(name="search_type", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="result", type=str, affinity=Affinity.TEXT, nullable=False),    # JSON
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="last_used_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="expires_at", type=str, affinity=Affinity.TEXT, nullable=True, index=True),
    ),
    indexes=(
        # FG2: 3-day TTL cached lookups.
        Index(name="idx_search_cache_expires",
              table=TableNames.SEARCH_CACHE,
              columns=("expires_at",)),
    ),
)


VOICE_PROFILES = Table(
    name=TableNames.VOICE_PROFILES,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="user_id", type=int, affinity=Affinity.INTEGER, nullable=False, index=True,
               foreign_key=ForeignKey(table=TableNames.ADMINS, column="id", on_delete=OnDelete.CASCADE)),
        Column(name="name", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="embedding", type=bytes, affinity=Affinity.BLOB, nullable=False),  # serialized embedding
        Column(name="embedding_model", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="embedding_dim", type=int, affinity=Affinity.INTEGER, nullable=False, default=0),
        Column(name="active", type=bool, affinity=Affinity.INTEGER, nullable=False, default=True),
        Column(name="created_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="updated_at", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
    indexes=(
        Index(name="idx_voice_profiles_user_active",
              table=TableNames.VOICE_PROFILES,
              columns=("user_id", "active"),
              unique=False),
    ),
)


INSTALLED_MODELS = Table(
    name=TableNames.INSTALLED_MODELS,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="model_id", type=str, affinity=Affinity.TEXT, nullable=False, unique=True, index=True),
        Column(name="kind", type=str, affinity=Affinity.TEXT, nullable=False, index=True),  # stt/tts/embed/llm/...
        Column(name="path", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="size_bytes", type=int, affinity=Affinity.INTEGER, nullable=False, default=0),
        Column(name="quantization", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="version", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="active", type=bool, affinity=Affinity.INTEGER, nullable=False, default=True),
        Column(name="metadata", type=str, affinity=Affinity.TEXT, nullable=True),   # JSON
        Column(name="installed_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
    ),
)


PLUGIN_REGISTRY = Table(
    name=TableNames.PLUGIN_REGISTRY,
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="plugin_id", type=str, affinity=Affinity.TEXT, nullable=False, unique=True, index=True),
        Column(name="name", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="version", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="api_version", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="author", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="description", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="entry_point", type=str, affinity=Affinity.TEXT, nullable=False),
        Column(name="permissions", type=str, affinity=Affinity.TEXT, nullable=False),  # JSON array
        Column(name="capabilities", type=str, affinity=Affinity.TEXT, nullable=False), # JSON array
        Column(name="network_policy", type=str, affinity=Affinity.TEXT, nullable=True), # JSON
        Column(name="signature_hash", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="status", type=str, affinity=Affinity.TEXT, nullable=False, default="disabled", index=True),
        Column(name="venv_path", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="installed_at", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="updated_at", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
    indexes=(
        Index(name="idx_plugin_registry_status",
              table=TableNames.PLUGIN_REGISTRY,
              columns=("status",)),
    ),
)


SCHEMA_MIGRATIONS = Table(
    name=TableNames.SCHEMA_MIGRATIONS,
    # Internal ledger the migration manager uses to track applied versions.
    columns=(
        Column(name="id", type=int, affinity=Affinity.INTEGER, primary_key=True, nullable=False, index=True),
        Column(name="schema_name", type=str, affinity=Affinity.TEXT, nullable=False, index=True),
        Column(name="version", type=int, affinity=Affinity.INTEGER, nullable=False, index=True),
        Column(name="description", type=str, affinity=Affinity.TEXT, nullable=True),
        Column(name="applied_at", type=str, affinity=Affinity.TEXT, nullable=False),
    ),
    indexes=(
        Index(name="idx_schema_migrations_unique",
              table=TableNames.SCHEMA_MIGRATIONS,
              columns=("schema_name", "version"),
              unique=True),
    ),
)


# ---------------------------------------------------------------------------
# Schema bundle
# ---------------------------------------------------------------------------


METADATA_SCHEMA: Schema = Schema(
    name=METADATA_SCHEMA_NAME,
    version=METADATA_SCHEMA_VERSION,
    event_source="app.core.database.metadata",
    tables=(
        SCHEMA_MIGRATIONS,
        ADMINS,
        PERMISSIONS,
        VOICE_PROFILES,
        CONVERSATION_HISTORY,
        MEMORY_LOGS,
        SCHEDULED_TASKS,
        TOOL_HISTORY,
        ASSISTANT_SETTINGS,
        SECURITY_EVENTS,
        SEARCH_CACHE,
        INSTALLED_MODELS,
        PLUGIN_REGISTRY,
    ),
)


def _timestamp_now() -> str:
    # Kept here so repositories that import models can stamp ISO-8601 UTC.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Force lint-time validation on import: any name typo on a Column would raise
# from models.base.__post_init__; we additionally ensure no FK target is
# missing from the schema.
assert {t.name for t in METADATA_SCHEMA.tables}.issuperset(
    {col.foreign_key.table
     for t in METADATA_SCHEMA.tables
     for col in t.columns
     if col.foreign_key is not None}
), "Foreign-key target missing from METADATA_SCHEMA"
