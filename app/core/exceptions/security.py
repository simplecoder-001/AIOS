# app/core/exceptions/security.py
"""
Security-layer exceptions.

Raised by FG6 (authentication, authorization, risk engine, AI firewall,
sandbox, encryption, audit) and the FG2 firewall. Security is the highest-
stakes subsystem, so these exceptions follow a strict FAIL-SECURE philosophy:
when in doubt, deny. Most are non-recoverable at the call site — a denied
action must not be silently retried into success.

IMPORTANT: security exceptions must never embed secrets, raw credentials,
PII, API keys, or the sensitive payload that triggered them. Context is
limited to non-sensitive metadata (role, reason code, category).

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "SecurityError",
    "AuthenticationError",
    "AuthorizationError",
    "PermissionDeniedError",
    "SpeakerVerificationError",
    "RiskThresholdExceededError",
    "FirewallBlockedError",
    "PromptInjectionError",
    "SandboxViolationError",
    "EncryptionError",
    "AuditIntegrityError",
]


class SecurityError(AIOSError):
    """Base class for all security failures. Fail-secure by default."""

    default_category = ErrorCategory.SECURITY
    default_severity = ErrorSeverity.CRITICAL

    def __init__(self, message: str, **kwargs: Any) -> None:
        # Security failures default to non-recoverable: deny, do not retry.
        kwargs.setdefault("recoverable", False)
        super().__init__(message, **kwargs)


class AuthenticationError(SecurityError):
    """Identity could not be established (login/verification failed)."""

    def __init__(self, reason: str = "authentication failed", **kwargs: Any) -> None:
        super().__init__(
            f"Authentication failed: {reason}",
            code="SEC_AUTHENTICATION_FAILED",
            **kwargs,
        )
        self.with_context(reason=reason)


class AuthorizationError(SecurityError):
    """A generic authorization failure (role/policy resolution problem)."""

    def __init__(self, reason: str = "authorization failed", **kwargs: Any) -> None:
        super().__init__(
            f"Authorization failed: {reason}",
            code="SEC_AUTHORIZATION_FAILED",
            **kwargs,
        )
        self.with_context(reason=reason)


class PermissionDeniedError(SecurityError):
    """A specific action was denied for the current role/capability.

    Aligns with the Casbin roles (guest/user/admin/super_admin/system).
    """

    def __init__(self, action: str, role: Optional[str] = None, **kwargs: Any) -> None:
        who = f" for role '{role}'" if role else ""
        super().__init__(
            f"Permission denied: '{action}'{who}",
            code="SEC_PERMISSION_DENIED",
            **kwargs,
        )
        self.with_context(action=action, role=role)


class SpeakerVerificationError(SecurityError):
    """Continuous/initial speaker verification failed or was revoked.

    Supports FG1/FG6 continuous verification: if identity changes mid-session,
    authorization is revoked and the session must de-authorize.
    """

    def __init__(self, reason: str = "voice identity mismatch", **kwargs: Any) -> None:
        super().__init__(
            f"Speaker verification failed: {reason}",
            code="SEC_SPEAKER_VERIFICATION_FAILED",
            **kwargs,
        )
        self.with_context(reason=reason)


class RiskThresholdExceededError(SecurityError):
    """The adaptive risk engine scored an action too high to auto-execute.

    Carries the risk level (Low/Medium/High/Critical) without the underlying
    sensitive command text.
    """

    def __init__(self, risk_level: str, action: Optional[str] = None, **kwargs: Any) -> None:
        what = f" for action '{action}'" if action else ""
        super().__init__(
            f"Risk threshold exceeded (level={risk_level}){what}",
            code="SEC_RISK_THRESHOLD_EXCEEDED",
            **kwargs,
        )
        self.with_context(risk_level=risk_level, action=action)


class FirewallBlockedError(SecurityError):
    """The AI firewall blocked an outbound request or input.

    Reason is a non-sensitive category (e.g. "pii", "secret", "api_key").
    The detected value itself is NEVER stored on the exception.
    """

    def __init__(self, reason_category: str, **kwargs: Any) -> None:
        super().__init__(
            f"Request blocked by AI firewall (category={reason_category})",
            code="SEC_FIREWALL_BLOCKED",
            **kwargs,
        )
        self.with_context(reason_category=reason_category)


class PromptInjectionError(FirewallBlockedError):
    """A prompt-injection attempt was detected and blocked."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(reason_category="prompt_injection", **kwargs)
        self.code = "SEC_PROMPT_INJECTION"
        self.message = "Prompt injection attempt detected and blocked"


class SandboxViolationError(SecurityError):
    """Sandboxed execution attempted a forbidden operation or exceeded limits.

    FATAL: a sandbox breach is a containment failure and must halt the
    offending execution immediately.
    """

    def __init__(self, violation: str, **kwargs: Any) -> None:
        super().__init__(
            f"Sandbox violation: {violation}",
            code="SEC_SANDBOX_VIOLATION",
            severity=ErrorSeverity.FATAL,
            **kwargs,
        )
        self.with_context(violation=violation)


class EncryptionError(SecurityError):
    """An encryption, decryption, signing, or key operation failed.

    Detail is kept generic to avoid leaking cryptographic material or oracle
    information; the specific operation is stored, never the data or key.
    """

    def __init__(self, operation: str = "encryption", cause: Optional[BaseException] = None, **kwargs: Any) -> None:
        super().__init__(
            f"Cryptographic operation '{operation}' failed",
            code="SEC_ENCRYPTION_ERROR",
            cause=cause,
            **kwargs,
        )
        self.with_context(operation=operation)


class AuditIntegrityError(SecurityError):
    """Tamper detection / HMAC / chain-of-trust verification failed.

    FATAL and non-recoverable: a broken audit chain means the security log can
    no longer be trusted, which itself is a critical incident.
    """

    def __init__(self, reason: str = "audit chain verification failed", **kwargs: Any) -> None:
        super().__init__(
            f"Audit integrity failure: {reason}",
            code="SEC_AUDIT_INTEGRITY_ERROR",
            severity=ErrorSeverity.FATAL,
            **kwargs,
        )
        self.with_context(reason=reason)
