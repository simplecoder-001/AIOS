"""
Tamper-evident audit logger with HMAC-SHA256 hash chaining.

This module implements the audit logging layer referenced in
Feature Group 6 (Security & Permission System), Layer 9 — Audit &
Tamper Protection.

Design Goals
------------
1. **Integrity**   : Every entry is cryptographically signed. Any
   deletion, insertion, or modification is detectable.
2. **Chaining**    : Each entry's HMAC incorporates the previous
   entry's HMAC, forming a hash chain. Removing or reordering
   entries breaks the chain.
3. **Offline**     : No external services required. Uses only the
   Python standard library (hmac, hashlib, json, logging).
4. **Performance** : HMAC-SHA256 of a short message is <0.1 ms.
   Meets the <10 ms log-write target from the FG6 performance table.
5. **Rotation-safe**: The chain resets on rotation but the previous
   chain's final HMAC is carried forward as the genesis seed.

Entry Format
------------
Each audit log line is a JSON object on a single line:

    {
        "seq": 1,
        "timestamp": "2026-07-03T02:19:11.123Z",
        "level": "INFO",
        "logger": "security.audit",
        "event": "AUTH_SUCCESS",
        "user": "admin",
        "action": "login",
        "result": "success",
        "risk_level": "low",
        "details": {...},
        "prev_hmac": "0000...0000",
        "hmac": "a3f2...e91c"
    }

Verification
------------
Call AuditLogger.verify_log_file(path, key) to validate the entire
chain. Returns a VerificationResult with details of any tampering.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.logging.formatters import BaseFormatter
from app.logging.logger import LogLevel
from app.logging.rotation import RotationConfig, RotationType, create_rotating_handler


# ──────────────────────────────────────────────────────────────────────
#  Data Models
# ──────────────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    """
    Structured representation of a single audit log entry.

    Attributes:
        seq        : monotonically increasing sequence number
        timestamp  : ISO 8601 UTC timestamp with milliseconds
        level      : log level name (INFO, WARNING, ERROR, CRITICAL)
        logger     : logger name
        event      : event type (e.g., AUTH_SUCCESS, PERMISSION_DENIED)
        user       : user identifier (or "system")
        action     : action that was performed or attempted
        result     : outcome ("success", "failure", "denied", "error")
        risk_level : risk assessment at time of event
        details    : arbitrary additional context
        prev_hmac  : HMAC of the previous entry (hex string)
        hmac       : HMAC of this entry (hex string, computed on signing)
    """
    seq: int
    timestamp: str
    level: str
    logger: str
    event: str
    user: str = "system"
    action: str = ""
    result: str = ""
    risk_level: str = "low"
    details: Dict[str, Any] = field(default_factory=dict)
    prev_hmac: str = "0" * 64
    hmac: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to an ordered dict for JSON line output."""
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "event": self.event,
            "user": self.user,
            "action": self.action,
            "result": self.result,
            "risk_level": self.risk_level,
            "details": self.details,
            "prev_hmac": self.prev_hmac,
            "hmac": self.hmac,
        }

    def to_json_line(self) -> str:
        """Serialize to a single-line JSON string."""
        return json.dumps(self.to_dict(), default=str, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditEntry":
        """Deserialize from a dict (parsed JSON line)."""
        return cls(
            seq=data.get("seq", 0),
            timestamp=data.get("timestamp", ""),
            level=data.get("level", "INFO"),
            logger=data.get("logger", ""),
            event=data.get("event", ""),
            user=data.get("user", "system"),
            action=data.get("action", ""),
            result=data.get("result", ""),
            risk_level=data.get("risk_level", "low"),
            details=data.get("details", {}),
            prev_hmac=data.get("prev_hmac", "0" * 64),
            hmac=data.get("hmac", ""),
        )


@dataclass
class VerificationResult:
    """
    Result of verifying an audit log file's integrity.

    Attributes:
        is_valid         : True if the entire chain is intact
        total_entries    : number of entries checked
        verified_entries : number of entries with valid HMACs
        broken_at        : sequence number where the chain broke (None if valid)
        broken_reason    : description of why the chain broke
        errors           : list of per-entry errors
    """
    is_valid: bool = True
    total_entries: int = 0
    verified_entries: int = 0
    broken_at: Optional[int] = None
    broken_reason: str = ""
    errors: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
#  Audit Formatter
# ──────────────────────────────────────────────────────────────────────

class AuditFormatter(BaseFormatter):
    """
    Formatter that serializes AuditEntry objects as signed JSON lines.

    This formatter expects the LogRecord to have an `audit_entry`
    attribute (set via the `extra` parameter). It extracts the entry
    and renders it as a single-line JSON string.
    """

    def __init__(self, use_colors: bool = False) -> None:
        super().__init__(use_colors=False)

    def format(self, record: logging.LogRecord) -> str:
        entry: Optional[AuditEntry] = getattr(record, "audit_entry", None)
        if entry is not None:
            return entry.to_json_line()

        # Fallback: format as a standard JSON log line
        payload = {
            "timestamp": self._format_timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = self._extract_extras(record)
        if extras:
            payload["details"] = extras
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
#  Audit Logger
# ──────────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Tamper-evident audit logger with HMAC-SHA256 hash chaining.

    Each log entry is signed with HMAC-SHA256 using a secret key.
    The signature incorporates the previous entry's signature, creating
    a chain where any tampering (deletion, insertion, modification)
    is detectable during verification.

    Thread Safety
    -------------
    All signing and writing operations are protected by a lock to
    ensure sequence numbers and HMAC chains remain consistent under
    concurrent access from multiple threads (e.g., the voice pipeline,
    security engine, and task manager all auditing simultaneously).

    Usage
    -----
        key = os.urandom(32)  # Generate once, store via DPAPI
        audit = AuditLogger(
            name="security.audit",
            file_path="logs/audit/audit.log",
            hmac_key=key,
        )

        audit.log(
            event="AUTH_SUCCESS",
            user="admin",
            action="voice_login",
            result="success",
            risk_level="low",
            details={"method": "speaker_verification", "confidence": 0.97},
        )

        # Verify integrity later
        result = AuditLogger.verify_log_file("logs/audit/audit.log", key)
        if not result.is_valid:
            print(f"TAMPER DETECTED at entry {result.broken_at}: {result.broken_reason}")
    """

    # Genesis HMAC for the first entry in a chain
    _GENESIS_HMAC = "0" * 64

    def __init__(
        self,
        name: str,
        file_path: str,
        hmac_key: bytes,
        level: LogLevel = LogLevel.INFO,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
        compress: bool = True,
    ) -> None:
        """
        Initialize the audit logger.

        Args:
            name        : logger name
            file_path   : path to the audit log file
            hmac_key    : secret key for HMAC-SHA256 signing
            level       : minimum log level
            max_bytes   : rotation threshold (default 10 MB)
            backup_count: rotated archives to keep (default 10)
            compress    : gzip rotated files
        """
        if not hmac_key or len(hmac_key) < 16:
            raise ValueError(
                "hmac_key must be at least 16 bytes for adequate security"
            )

        self._name = name
        self._file_path = file_path
        self._hmac_key = hmac_key
        self._level = int(level)
        self._closed = False

        # Thread-safe signing and writing
        self._lock = threading.Lock()

        # Sequence counter and chain state
        self._seq: int = 0
        self._last_hmac: str = self._GENESIS_HMAC

        # Initialize the chain from existing log file (if present)
        self._init_chain_from_existing()

        # Create the underlying logger and handler
        self._logger = logging.getLogger(name)
        self._logger.setLevel(self._level)
        self._logger.propagate = False
        self._logger.handlers.clear()

        rotation_config = RotationConfig(
            rotation_type=RotationType.SIZE,
            max_bytes=max_bytes,
            backup_count=backup_count,
            compress=compress,
        )

        handler = create_rotating_handler(
            file_path=file_path,
            config=rotation_config,
            formatter=AuditFormatter(),
        )
        self._logger.addHandler(handler)

    # ── chain initialization ─────────────────────────────────────────

    def _init_chain_from_existing(self) -> None:
        """
        Recover chain state from an existing log file.

        If the file already exists and contains entries, read the last
        valid entry to restore the sequence counter and HMAC chain.
        This ensures continuity across application restarts.
        """
        path = Path(self._file_path)
        if not path.exists() or path.stat().st_size == 0:
            return

        last_seq = 0
        last_hmac = self._GENESIS_HMAC

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        last_seq = data.get("seq", last_seq)
                        last_hmac = data.get("hmac", last_hmac)
                    except (json.JSONDecodeError, KeyError):
                        # Skip malformed lines
                        continue
        except OSError:
            pass

        self._seq = last_seq
        self._last_hmac = last_hmac

    # ── HMAC computation ──────────────────────────────────────────────

    def _compute_hmac(self, entry: AuditEntry) -> str:
        """
        Compute the HMAC-SHA256 signature for an audit entry.

        The signed payload is a deterministic JSON string containing
        all fields EXCEPT the hmac field itself, concatenated with
        the previous entry's HMAC.

        This ensures:
            - Any field modification changes the HMAC
            - Reordering breaks the prev_hmac chain
            - Deletion breaks the chain at the gap
        """
        # Build the signing payload: all fields except hmac
        payload_dict = {
            "seq": entry.seq,
            "timestamp": entry.timestamp,
            "level": entry.level,
            "logger": entry.logger,
            "event": entry.event,
            "user": entry.user,
            "action": entry.action,
            "result": entry.result,
            "risk_level": entry.risk_level,
            "details": entry.details,
            "prev_hmac": entry.prev_hmac,
        }
        payload = json.dumps(
            payload_dict,
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hmac.new(
            self._hmac_key,
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ── public logging API ────────────────────────────────────────────

    def log(
        self,
        event: str,
        user: str = "system",
        action: str = "",
        result: str = "",
        risk_level: str = "low",
        details: Optional[Dict[str, Any]] = None,
        level: LogLevel = LogLevel.INFO,
    ) -> AuditEntry:
        """
        Write a signed audit entry to the log file.

        Args:
            event      : event type (e.g., AUTH_SUCCESS, PERMISSION_DENIED,
                         RISK_ESCALATION, TOOL_VALIDATION_FAILED)
            user       : user identifier
            action     : action performed or attempted
            result     : outcome (success, failure, denied, error)
            risk_level : risk level at time of event (low, medium, high, critical)
            details    : additional context (dict, JSON-serializable)
            level      : log level for this entry

        Returns:
            The signed AuditEntry that was written.
        """
        if self._closed:
            raise RuntimeError("AuditLogger is closed")

        with self._lock:
            # Build the entry
            self._seq += 1
            now = datetime.now(timezone.utc)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            entry = AuditEntry(
                seq=self._seq,
                timestamp=timestamp,
                level=level.name,
                logger=self._name,
                event=event,
                user=user,
                action=action,
                result=result,
                risk_level=risk_level,
                details=details or {},
                prev_hmac=self._last_hmac,
            )

            # Sign the entry
            entry.hmac = self._compute_hmac(entry)

            # Update chain state
            self._last_hmac = entry.hmac

            # Write to the underlying logger
            self._logger.info(
                entry.to_json_line(),
                extra={"audit_entry": entry},
            )

            return entry

    # Convenience methods for common audit event types

    def auth_success(
        self,
        user: str,
        method: str = "",
        confidence: float = 0.0,
        **extra: Any,
    ) -> AuditEntry:
        """Log a successful authentication event."""
        details = {"method": method, "confidence": confidence, **extra}
        return self.log(
            event="AUTH_SUCCESS",
            user=user,
            action="authenticate",
            result="success",
            risk_level="low",
            details=details,
        )

    def auth_failure(
        self,
        user: str,
        method: str = "",
        reason: str = "",
        **extra: Any,
    ) -> AuditEntry:
        """Log a failed authentication event."""
        details = {"method": method, "reason": reason, **extra}
        return self.log(
            event="AUTH_FAILURE",
            user=user,
            action="authenticate",
            result="failure",
            risk_level="medium",
            details=details,
            level=LogLevel.WARNING,
        )

    def permission_denied(
        self,
        user: str,
        resource: str = "",
        permission: str = "",
        **extra: Any,
    ) -> AuditEntry:
        """Log a permission denial event."""
        details = {
            "resource": resource,
            "permission": permission,
            **extra,
        }
        return self.log(
            event="PERMISSION_DENIED",
            user=user,
            action="access_resource",
            result="denied",
            risk_level="medium",
            details=details,
            level=LogLevel.WARNING,
        )

    def risk_escalation(
        self,
        user: str,
        command: str = "",
        risk_level: str = "high",
        **extra: Any,
    ) -> AuditEntry:
        """Log a risk escalation event."""
        details = {"command": command, **extra}
        return self.log(
            event="RISK_ESCALATION",
            user=user,
            action="evaluate_risk",
            result="escalated",
            risk_level=risk_level,
            details=details,
            level=LogLevel.WARNING,
        )

    def prompt_blocked(
        self,
        user: str,
        reason: str = "",
        **extra: Any,
    ) -> AuditEntry:
        """Log a blocked prompt injection or malicious input."""
        details = {"reason": reason, **extra}
        return self.log(
            event="PROMPT_BLOCKED",
            user=user,
            action="firewall_check",
            result="blocked",
            risk_level="high",
            details=details,
            level=LogLevel.WARNING,
        )

    def tool_execution(
        self,
        user: str,
        tool: str = "",
        status: str = "",
        execution_time: float = 0.0,
        **extra: Any,
    ) -> AuditEntry:
        """Log a tool execution event."""
        details = {
            "tool": tool,
            "status": status,
            "execution_time": execution_time,
            **extra,
        }
        return self.log(
            event="TOOL_EXECUTED",
            user=user,
            action="execute_tool",
            result=status,
            risk_level="low",
            details=details,
        )

    def rollback(
        self,
        user: str,
        action_id: str = "",
        reason: str = "",
        **extra: Any,
    ) -> AuditEntry:
        """Log a rollback event."""
        details = {"action_id": action_id, "reason": reason, **extra}
        return self.log(
            event="ROLLBACK_EXECUTED",
            user=user,
            action="rollback",
            result="success",
            risk_level="medium",
            details=details,
        )

    # ── verification ──────────────────────────────────────────────────

    @staticmethod
    def verify_log_file(
        file_path: str,
        hmac_key: bytes,
    ) -> VerificationResult:
        """
        Verify the integrity of an audit log file.

        Reads every entry, recomputes each HMAC, and checks that the
        chain is unbroken (each entry's prev_hmac matches the previous
        entry's hmac).

        Args:
            file_path : path to the audit log file
            hmac_key  : the same key used to sign the entries

        Returns:
            VerificationResult with details of any tampering found.
        """
        result = VerificationResult()
        path = Path(file_path)

        if not path.exists():
            result.is_valid = False
            result.broken_reason = f"File not found: {file_path}"
            return result

        expected_prev = AuditLogger._GENESIS_HMAC
        expected_seq = 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    result.total_entries += 1

                    # Parse the JSON line
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as e:
                        result.is_valid = False
                        result.broken_at = expected_seq + 1
                        result.broken_reason = (
                            f"Line {line_num}: JSON parse error: {e}"
                        )
                        result.errors.append(
                            f"Line {line_num}: malformed JSON"
                        )
                        return result

                    entry = AuditEntry.from_dict(data)

                    # Check sequence continuity
                    expected_seq += 1
                    if entry.seq != expected_seq:
                        result.is_valid = False
                        result.broken_at = entry.seq
                        result.broken_reason = (
                            f"Sequence gap: expected {expected_seq}, "
                            f"got {entry.seq}"
                        )
                        result.errors.append(
                            f"Entry seq={entry.seq}: sequence discontinuity"
                        )
                        return result

                    # Check chain linkage
                    if entry.prev_hmac != expected_prev:
                        result.is_valid = False
                        result.broken_at = entry.seq
                        result.broken_reason = (
                            f"Chain broken at seq={entry.seq}: "
                            f"prev_hmac mismatch (expected {expected_prev[:16]}..., "
                            f"got {entry.prev_hmac[:16]}...)"
                        )
                        result.errors.append(
                            f"Entry seq={entry.seq}: prev_hmac does not match "
                            f"previous entry's hmac"
                        )
                        return result

                    # Recompute and verify HMAC
                    recomputed = AuditLogger._compute_hmac_static(
                        entry, hmac_key
                    )
                    if not hmac.compare_digest(recomputed, entry.hmac):
                        result.is_valid = False
                        result.broken_at = entry.seq
                        result.broken_reason = (
                            f"HMAC verification failed at seq={entry.seq}: "
                            f"entry may have been modified"
                        )
                        result.errors.append(
                            f"Entry seq={entry.seq}: HMAC mismatch "
                            f"(expected {recomputed[:16]}..., "
                            f"got {entry.hmac[:16]}...)"
                        )
                        return result

                    # Advance chain
                    expected_prev = entry.hmac
                    result.verified_entries += 1

        except OSError as e:
            result.is_valid = False
            result.broken_reason = f"File read error: {e}"
            return result

        return result

    @staticmethod
    def _compute_hmac_static(entry: AuditEntry, hmac_key: bytes) -> str:
        """
        Static HMAC computation for verification (no instance state).

        Must produce identical output to AuditLogger._compute_hmac().
        """
        payload_dict = {
            "seq": entry.seq,
            "timestamp": entry.timestamp,
            "level": entry.level,
            "logger": entry.logger,
            "event": entry.event,
            "user": entry.user,
            "action": entry.action,
            "result": entry.result,
            "risk_level": entry.risk_level,
            "details": entry.details,
            "prev_hmac": entry.prev_hmac,
        }
        payload = json.dumps(
            payload_dict,
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hmac.new(
            hmac_key,
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ── properties ────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def sequence(self) -> int:
        """Current sequence number (last written entry)."""
        with self._lock:
            return self._seq

    @property
    def last_hmac(self) -> str:
        """HMAC of the most recently written entry."""
        with self._lock:
            return self._last_hmac

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ── lifecycle ─────────────────────────────────────────────────────

    def flush(self) -> None:
        """Flush all handlers' buffers to disk."""
        for handler in self._logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass

    def close(self) -> None:
        """Flush and close all handlers. Call during shutdown."""
        if self._closed:
            return
        self._closed = True
        for handler in self._logger.handlers[:]:
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
            self._logger.removeHandler(handler)

    def __repr__(self) -> str:
        return (
            f"<AuditLogger name={self._name!r} "
            f"seq={self._seq} closed={self._closed}>"
        )
