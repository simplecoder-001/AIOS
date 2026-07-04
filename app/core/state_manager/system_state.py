# app/core/state_manager/system_state.py
"""
System state definitions for the AIOS State Manager.

This module provides a lightweight, immutable representation of a registered
state machine and its current runtime state. Every subsystem (voice, brain,
security, GUI, agents, etc.) is represented by one SystemState instance
inside the StateRegistry.

Import-safe:
    - Standard library only
    - app.core.constants
    - app.core.exceptions

No EventBus, logging, persistence, or DI dependencies are introduced here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping
from types import MappingProxyType

from app.core.constants import BaseState
from app.core.exceptions import InvalidStateError

__all__ = [
    "SystemState",
]


def _utcnow() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class SystemState:
    """
    Immutable runtime state of a registered subsystem.

    Examples
    --------
    >>> SystemState(
    ...     name="voice",
    ...     state=VoiceState.IDLE,
    ... )
    """

    name: str
    state: BaseState
    previous_state: BaseState | None = None
    metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    entered_at: datetime = field(default_factory=_utcnow)
    version: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidStateError(
                current_state="<unknown>",
                operation="create SystemState with empty name",
            )

        if not isinstance(self.state, BaseState):
            raise InvalidStateError(
                current_state=repr(self.state),
                operation="create SystemState with non-BaseState value",
            )

        if (
            self.previous_state is not None
            and not isinstance(self.previous_state, BaseState)
        ):
            raise InvalidStateError(
                current_state=repr(self.previous_state),
                operation="create SystemState with invalid previous_state",
            )

        if not isinstance(self.metadata, Mapping):
            raise InvalidStateError(
                current_state=self.name,
                operation="create SystemState with non-mapping metadata",
            )

    # ------------------------------------------------------------------
    # State operations
    # ------------------------------------------------------------------

    def transition_to(
        self,
        new_state: BaseState,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "SystemState":
        """
        Create a new immutable instance representing the next state.
        """
        if not isinstance(new_state, BaseState):
            raise InvalidStateError(
                current_state=repr(new_state),
                operation="transition to non-BaseState value",
            )

        return SystemState(
            name=self.name,
            state=new_state,
            previous_state=self.state,
            metadata=metadata or self.metadata,
            entered_at=_utcnow(),
            version=self.version + 1,
        )

    def with_metadata(
        self,
        **updates: Any,
    ) -> "SystemState":
        """
        Return a copy with merged metadata.
        """
        merged: MutableMapping[str, Any] = dict(self.metadata)
        merged.update(updates)

        return SystemState(
            name=self.name,
            state=self.state,
            previous_state=self.previous_state,
            metadata=MappingProxyType(dict(merged)),
            entered_at=self.entered_at,
            version=self.version,
        )

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Convert to a JSON-serializable dictionary.
        """
        return {
            "name": self.name,
            "state": self.state.value,
            "previous_state": (
                self.previous_state.value
                if self.previous_state is not None
                else None
            ),
            "metadata": dict(self.metadata),
            "entered_at": self.entered_at.isoformat(),
            "version": self.version,
        }

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def changed(self) -> bool:
        """Whether at least one transition occurred."""
        return self.version > 0

    @property
    def age_seconds(self) -> float:
        """Seconds spent in the current state."""
        return (
            _utcnow() - self.entered_at
        ).total_seconds()

    def __str__(self) -> str:
        return (
            f"{self.name}:"
            f"{self.state.value}"
            f"(v{self.version})"
        )

    def __repr__(self) -> str:
        return (
            "SystemState("
            f"name={self.name!r}, "
            f"state={self.state!r}, "
            f"version={self.version}"
            ")"
        )