# app/core/exceptions/database.py
"""
Database-layer exceptions.

Raised by ``app/core/database`` across all backends: SQLite (metadata),
SQLCipher (encrypted personal memory), Qdrant (vector store), and the
knowledge graph. Also covers connection pooling, transactions, migrations,
and backups.

These errors range from recoverable (transient connection loss, retryable
transaction) to critical (encryption key failure on the encrypted store, which
must fail secure per FG6). Severity is chosen accordingly per subclass.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "DatabaseError",
    "ConnectionError",
    "TransactionError",
    "MigrationError",
    "QueryError",
    "IntegrityError",
    "BackupError",
    "EncryptionKeyError",
    "VectorStoreError",
    "KnowledgeGraphError",
]


class DatabaseError(AIOSError):
    """Base class for all database failures."""

    default_category = ErrorCategory.DATABASE
    default_severity = ErrorSeverity.ERROR


class ConnectionError(DatabaseError):
    """Failed to open, acquire, or maintain a database connection.

    Recoverable by default: connection loss is often transient and the
    connection/health manager may retry or re-establish the pool.
    """

    def __init__(self, backend: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Database connection failed for backend '{backend}'",
            code="DB_CONNECTION_ERROR",
            severity=ErrorSeverity.CRITICAL,
            cause=cause,
            **kwargs,
        )
        self.with_context(backend=backend)


class TransactionError(DatabaseError):
    """A transaction failed to commit and was (or must be) rolled back.

    Feeds the FG3/FG2 transaction + rollback machinery.
    """

    def __init__(self, operation: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Transaction failed during '{operation}'",
            code="DB_TRANSACTION_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation)


class MigrationError(DatabaseError):
    """A schema migration failed to apply.

    Non-recoverable: an incomplete migration can leave the schema in an
    inconsistent state, so the system must halt rather than run against a
    half-migrated database.
    """

    def __init__(self, version: Any, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Database migration failed at version: {version}",
            code="DB_MIGRATION_ERROR",
            severity=ErrorSeverity.CRITICAL,
            recoverable=False,
            cause=cause,
            **kwargs,
        )
        self.with_context(version=str(version))


class QueryError(DatabaseError):
    """A query/statement failed to execute."""

    def __init__(self, statement: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            "Database query execution failed",
            code="DB_QUERY_ERROR",
            cause=cause,
            **kwargs,
        )
        # Store a truncated statement only; never assume it is secret-free.
        if statement:
            self.with_context(statement=statement[:500])


class IntegrityError(DatabaseError):
    """A constraint (unique, foreign key, not-null, check) was violated."""

    def __init__(self, constraint: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        detail = f": {constraint}" if constraint else ""
        super().__init__(
            f"Database integrity constraint violated{detail}",
            code="DB_INTEGRITY_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(constraint=constraint)


class BackupError(DatabaseError):
    """A backup or restore operation failed."""

    def __init__(self, operation: str = "backup", cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Database {operation} operation failed",
            code="DB_BACKUP_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation)


class EncryptionKeyError(DatabaseError):
    """The SQLCipher encryption key is missing, wrong, or could not be bound.

    FATAL and non-recoverable: per FG6 fail-secure, the encrypted personal
    memory store must never fall back to an unencrypted or unauthenticated
    state. The system must refuse to proceed.
    """

    def __init__(self, reason: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Encrypted database key error{suffix}",
            code="DB_ENCRYPTION_KEY_ERROR",
            severity=ErrorSeverity.FATAL,
            recoverable=False,
            cause=cause,
            **kwargs,
        )
        # Deliberately do NOT store the key or reason detail that could leak it.
        self.with_context(backend="sqlcipher")


class VectorStoreError(DatabaseError):
    """A Qdrant vector-store operation failed (upsert, search, indexing)."""

    def __init__(self, operation: str, collection: Optional[str] = None, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Vector store operation '{operation}' failed",
            code="DB_VECTOR_STORE_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation, collection=collection)


class KnowledgeGraphError(DatabaseError):
    """A knowledge-graph query or mutation failed."""

    def __init__(self, operation: str, cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Knowledge graph operation '{operation}' failed",
            code="DB_KNOWLEDGE_GRAPH_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation)
