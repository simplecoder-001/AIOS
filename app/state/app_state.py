# app/state/app_state.py

"""
Application lifecycle state definitions.

This module provides the public application-level state enum used by
bootstrap, lifecycle management, and feature groups.

The canonical state vocabulary lives in
`app.core.constants.states.AppState`. This module re-exports those
states and provides conversion helpers to/from SystemState.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from app.core.constants.states import AppState as _CoreAppState
from app.core.state_manager.system_state import SystemState


class AppState(str, Enum):
    """
    Public application lifecycle states.

    Mirrors the canonical states defined in
    `app.core.constants.states.AppState`.
    """

    CREATED = _CoreAppState.CREATED.value
    BOOTSTRAPPING = _CoreAppState.BOOTSTRAPPING.value
    INITIALIZING = _CoreAppState.INITIALIZING.value
    STARTING = _CoreAppState.STARTING.value
    RUNNING = _CoreAppState.RUNNING.value
    DEGRADED = _CoreAppState.DEGRADED.value
    PAUSING = _CoreAppState.PAUSING.value
    PAUSED = _CoreAppState.PAUSED.value
    RESUMING = _CoreAppState.RESUMING.value
    STOPPING = _CoreAppState.STOPPING.value
    STOPPED = _CoreAppState.STOPPED.value
    ERROR = _CoreAppState.ERROR.value
    SHUTDOWN = _CoreAppState.SHUTDOWN.value

    @classmethod
    def from_system_state(cls, state: SystemState) -> "AppState":
        """
        Convert a SystemState into an AppState.

        Raises:
            ValueError:
                If the system state cannot be mapped.
        """
        try:
            return _SYSTEM_TO_APP[state]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported SystemState: {state!r}"
            ) from exc

    def to_system_state(self) -> SystemState:
        """
        Convert this AppState into its corresponding SystemState.

        Raises:
            ValueError:
                If the state cannot be mapped.
        """
        try:
            return _APP_TO_SYSTEM[self]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported AppState: {self!r}"
            ) from exc

    @property
    def is_terminal(self) -> bool:
        """True if this is a terminal application state."""
        return self in TERMINAL_APP_STATES

    @property
    def is_running(self) -> bool:
        """True if the application is operational."""
        return self in ACTIVE_APP_STATES

    def __str__(self) -> str:
        return self.value


_SYSTEM_TO_APP: Final[dict[SystemState, AppState]] = {
    SystemState.CREATED: AppState.CREATED,
    SystemState.BOOTSTRAPPING: AppState.BOOTSTRAPPING,
    SystemState.INITIALIZING: AppState.INITIALIZING,
    SystemState.STARTING: AppState.STARTING,
    SystemState.RUNNING: AppState.RUNNING,
    SystemState.DEGRADED: AppState.DEGRADED,
    SystemState.PAUSING: AppState.PAUSING,
    SystemState.PAUSED: AppState.PAUSED,
    SystemState.RESUMING: AppState.RESUMING,
    SystemState.STOPPING: AppState.STOPPING,
    SystemState.STOPPED: AppState.STOPPED,
    SystemState.ERROR: AppState.ERROR,
    SystemState.SHUTDOWN: AppState.SHUTDOWN,
}

_APP_TO_SYSTEM: Final[dict[AppState, SystemState]] = {
    app_state: system_state
    for system_state, app_state in _SYSTEM_TO_APP.items()
}

ACTIVE_APP_STATES: Final[frozenset[AppState]] = frozenset(
    {
        AppState.RUNNING,
        AppState.DEGRADED,
        AppState.PAUSED,
    }
)

TERMINAL_APP_STATES: Final[frozenset[AppState]] = frozenset(
    {
        AppState.STOPPED,
        AppState.SHUTDOWN,
        AppState.ERROR,
    }
)

__all__ = [
    "AppState",
    "ACTIVE_APP_STATES",
    "TERMINAL_APP_STATES",
]