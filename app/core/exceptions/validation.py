# app/core/exceptions/validation.py
"""
Validation exceptions.

Raised whenever untrusted or structured input fails validation: Pydantic
schema checks, tool-parameter validation (FG2 tool manager / FG6 tool
validator), JSON-schema conformance, type coercion, and range/constraint
checks. Distinct from ``configuration.py`` (which validates the config tree at
boot) — this module covers *runtime* data flowing through tools, LLM tool
calls, and user/agent inputs.

Validation failures are generally recoverable: the caller can reject the input
and ask again (FG2 clarification, FG3 confidence-based re-prompt) rather than
crash. Severity therefore defaults to WARNING.

Dependency order
----------------
Depends only on ``base.py``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from app.core.exceptions.base import AIOSError, ErrorCategory, ErrorSeverity

__all__ = [
    "ValidationError",
    "SchemaValidationError",
    "ParameterValidationError",
    "TypeCoercionError",
    "ConstraintViolationError",
    "MissingFieldError",
    "ToolValidationError",
]


class ValidationError(AIOSError):
    """Base class for all runtime validation failures."""

    default_category = ErrorCategory.VALIDATION
    default_severity = ErrorSeverity.WARNING


class SchemaValidationError(ValidationError):
    """A payload failed schema validation (Pydantic / JSON Schema).

    Carries the list of individual field errors so the caller can report them
    all at once instead of one-at-a-time.
    """

    def __init__(self, errors: Iterable[str], *, schema: Optional[str] = None, **kwargs: Any) -> None:
        error_list = list(errors)
        joined = "; ".join(error_list) if error_list else "unknown validation error"
        where = f" against schema '{schema}'" if schema else ""
        super().__init__(
            f"Schema validation failed{where}: {joined}",
            code="VALIDATION_SCHEMA_ERROR",
            **kwargs,
        )
        self.errors = error_list
        self.with_context(errors=error_list, schema=schema)


class ParameterValidationError(ValidationError):
    """A named parameter had an invalid value."""

    def __init__(
        self,
        parameter: str,
        value: Any,
        *,
        expected: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        detail = f" (expected {expected})" if expected else ""
        super().__init__(
            f"Invalid value for parameter '{parameter}'{detail}",
            code="VALIDATION_PARAMETER_ERROR",
            **kwargs,
        )
        # value may be user data; store a repr, truncated defensively.
        self.with_context(parameter=parameter, value=repr(value)[:200], expected=expected)


class TypeCoercionError(ValidationError):
    """A value could not be coerced to the expected type."""

    def __init__(self, field: str, value: Any, target_type: str, **kwargs: Any) -> None:
        super().__init__(
            f"Cannot coerce field '{field}' to {target_type}",
            code="VALIDATION_TYPE_COERCION_ERROR",
            **kwargs,
        )
        self.with_context(field=field, value=repr(value)[:200], target_type=target_type)


class ConstraintViolationError(ValidationError):
    """A value violated a constraint (range, length, pattern, enum)."""

    def __init__(self, field: str, constraint: str, **kwargs: Any) -> None:
        super().__init__(
            f"Constraint violated on '{field}': {constraint}",
            code="VALIDATION_CONSTRAINT_ERROR",
            **kwargs,
        )
        self.with_context(field=field, constraint=constraint)


class MissingFieldError(ValidationError):
    """A required field was absent from the payload."""

    def __init__(self, field: str, **kwargs: Any) -> None:
        super().__init__(
            f"Required field is missing: '{field}'",
            code="VALIDATION_MISSING_FIELD",
            **kwargs,
        )
        self.with_context(field=field)


class ToolValidationError(ValidationError):
    """A tool request failed validation before execution.

    Bridges FG2 (tool schemas / Pydantic) and FG6 (safety/tool validator).
    Elevated to ERROR because an invalid tool call must block execution, not
    merely warn — it feeds the confidence/clarification loop.
    """

    def __init__(self, tool: str, reason: str, **kwargs: Any) -> None:
        super().__init__(
            f"Tool '{tool}' request validation failed: {reason}",
            code="VALIDATION_TOOL_ERROR",
            severity=ErrorSeverity.ERROR,
            **kwargs,
        )
        self.with_context(tool=tool, reason=reason)
