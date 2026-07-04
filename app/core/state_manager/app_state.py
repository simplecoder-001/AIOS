# app/core/state_manager/app_state.py
"""
Application-wide state container for the AIOS State Manager.

Unlike SystemState, which represents the state of a single subsystem,
AppState is the immutable snapshot of all registered subsystem states at
a specific moment in time.

Used by:
    - StateRegistry
    - StateMachine
    - StateSnapshot
    - StatePersistence
    - Recovery Manager
    - Event Bus state events

Import-safe:
    - Standard library
    - app.core.constants
    - app.core.exceptions
    - app.core.state_manager.system_state
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from app.core.exceptions import InvalidStateError
from app.core.state_manager.system_state import SystemState

__all__ = [
    "AppState",
]


def _utcnow() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class AppState:
    """
    Immutable snapshot of the entire application's runtime state.

    Example
    -------
    {
        "voice": SystemState(...),
        "brain": SystemState(...),
        "security": SystemState(...),
        "gui": SystemState(...),
    }
    """

    systems: Mapping[str, SystemState] = field(
        default_factory=lambda: MappingProxyType({})
    )
    created_at: datetime = field(default_factory=_utcnow)
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.systems, Mapping):
            raise InvalidStateError(
                current_state="<unknown>",
                operation="create AppState with non-mapping systems",
            )

        for name, state in self.systems.items():
            if not isinstance(name, str):
                raise InvalidStateError(
                    current_state=repr(name),
                    operation="register non-string subsystem name",
                )

            if not isinstance(state, SystemState):
                raise InvalidStateError(
                    current_state=repr(state),
                    operation="register non-SystemState object",
                )

    # ------------------------------------------------------------------
    # State lookup
    # ------------------------------------------------------------------

    def has(self, name: str) -> bool:
        """Return True if a subsystem is registered."""
        return name in self.systems

    def get(
        self,
        name: str,
        default: SystemState | None = None,
    ) -> SystemState | None:
        """
        Return subsystem state or default.
        """
        return self.systems.get(name, default)

    def require(self, name: str) -> SystemState:
        """
        Return subsystem state.

        Raises
        ------
        InvalidStateError
            If subsystem is not registered.
        """
        state = self.systems.get(name)
        if state is None:
            raise InvalidStateError(
                current_state=name,
                operation="access unregistered subsystem state",
            )

        return state

    # ------------------------------------------------------------------
    # Immutable modifications
    # ------------------------------------------------------------------

    def with_system(
        self,
        state: SystemState,
    ) -> "AppState":
        """
        Return a new AppState with a system inserted or replaced.
        """
        if not isinstance(state, SystemState):
            raise InvalidStateError(
                current_state=repr(state),
                operation="insert non-SystemState object",
            )

        systems = dict(self.systems)
        systems[state.name] = state

        return AppState(
            systems=MappingProxyType(systems),
            created_at=_utcnow(),
            version=self.version + 1,
        )

    def without_system(
        self,
        name: str,
    ) -> "AppState":
        """
        Return a new AppState without the specified subsystem.
        """
        if name not in self.systems:
            return self

        systems = dict(self.systems)
        systems.pop(name)

        return AppState(
            systems=MappingProxyType(systems),
            created_at=_utcnow(),
            version=self.version + 1,
        )

    def update_system(
        self,
        name: str,
        state: SystemState,
    ) -> "AppState":
        """
        Replace an existing subsystem state.

        Raises
        ------
        InvalidStateError
            If subsystem does not exist.
        """
        if name not in self.systems:
            raise InvalidStateError(
                current_state=name,
                operation="update unregistered subsystem state",
            )

        return self.with_system(state)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def subsystem_count(self) -> int:
        """Number of registered subsystem states."""
        return len(self.systems)

    @property
    def is_empty(self) -> bool:
        """Whether no subsystem states are registered."""
        return not self.systems

    @property
    def system_names(self) -> tuple[str, ...]:
        """Registered subsystem names."""
        return tuple(self.systems.keys())

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Convert to a JSON-serializable dictionary.
        """
        return {
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "systems": {
                name: state.to_dict()
                for name, state in self.systems.items()
            },
        }

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.systems)

    def __contains__(self, name: str) -> bool:
        return name in self.systems

    def __iter__(self):
        return iter(self.systems.values())

    def __repr__(self) -> str:
        return (
            "AppState("
            f"systems={len(self.systems)}, "
            f"version={self.version}"
            ")"
        )