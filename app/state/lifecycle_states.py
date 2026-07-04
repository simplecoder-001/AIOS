# app/state/lifecycle_states.py

"""
Application lifecycle definitions and transition metadata.

This module sits above the canonical state vocabulary and provides
lifecycle-oriented helpers used by bootstrap, lifecycle management,
and the application state machine.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping

from app.core.constants.events import LifecycleEvent
from app.state.app_state import AppState


class LifecyclePhase(str, Enum):
    """
    High-level application phases.

    Multiple AppState values may belong to the same phase.
    """

    CREATION = "creation"
    STARTUP = "startup"
    ACTIVE = "active"
    PAUSED = "paused"
    SHUTDOWN = "shutdown"
    FAILURE = "failure"


STATE_PHASE_MAP: Final[Mapping[AppState, LifecyclePhase]] = (
    MappingProxyType(
        {
            AppState.CREATED: LifecyclePhase.CREATION,
            AppState.BOOTSTRAPPING: LifecyclePhase.STARTUP,
            AppState.INITIALIZING: LifecyclePhase.STARTUP,
            AppState.STARTING: LifecyclePhase.STARTUP,
            AppState.RUNNING: LifecyclePhase.ACTIVE,
            AppState.DEGRADED: LifecyclePhase.ACTIVE,
            AppState.PAUSING: LifecyclePhase.PAUSED,
            AppState.PAUSED: LifecyclePhase.PAUSED,
            AppState.RESUMING: LifecyclePhase.ACTIVE,
            AppState.STOPPING: LifecyclePhase.SHUTDOWN,
            AppState.STOPPED: LifecyclePhase.SHUTDOWN,
            AppState.SHUTDOWN: LifecyclePhase.SHUTDOWN,
            AppState.ERROR: LifecyclePhase.FAILURE,
        }
    )
)


STATE_EVENT_MAP: Final[Mapping[AppState, LifecycleEvent]] = (
    MappingProxyType(
        {
            AppState.BOOTSTRAPPING: LifecycleEvent.APP_BOOTSTRAP_STARTED,
            AppState.INITIALIZING: LifecycleEvent.APP_INITIALIZED,
            AppState.STARTING: LifecycleEvent.APP_STARTED,
            AppState.PAUSED: LifecycleEvent.APP_PAUSED,
            AppState.RESUMING: LifecycleEvent.APP_RESUMED,
            AppState.STOPPING: LifecycleEvent.APP_STOPPING,
            AppState.STOPPED: LifecycleEvent.APP_STOPPED,
            AppState.SHUTDOWN: LifecycleEvent.SHUTDOWN_EVENT,
        }
    )
)


STARTUP_STATES: Final[frozenset[AppState]] = frozenset(
    {
        AppState.BOOTSTRAPPING,
        AppState.INITIALIZING,
        AppState.STARTING,
    }
)

ACTIVE_STATES: Final[frozenset[AppState]] = frozenset(
    {
        AppState.RUNNING,
        AppState.DEGRADED,
        AppState.RESUMING,
    }
)

PAUSED_STATES: Final[frozenset[AppState]] = frozenset(
    {
        AppState.PAUSING,
        AppState.PAUSED,
    }
)

TERMINAL_STATES: Final[frozenset[AppState]] = frozenset(
    {
        AppState.STOPPED,
        AppState.SHUTDOWN,
        AppState.ERROR,
    }
)


class LifecycleStates:
    """
    Convenience helper around application lifecycle metadata.

    This class is intentionally stateless and only exposes pure helpers.
    """

    @staticmethod
    def phase_of(state: AppState) -> LifecyclePhase:
        """
        Return the lifecycle phase for an application state.
        """
        return STATE_PHASE_MAP[state]

    @staticmethod
    def event_for(state: AppState) -> LifecycleEvent | None:
        """
        Return the lifecycle event associated with a state.

        Returns:
            LifecycleEvent | None
        """
        return STATE_EVENT_MAP.get(state)

    @staticmethod
    def is_startup_state(state: AppState) -> bool:
        """
        Return True if the state belongs to startup.
        """
        return state in STARTUP_STATES

    @staticmethod
    def is_active_state(state: AppState) -> bool:
        """
        Return True if the application is operational.
        """
        return state in ACTIVE_STATES

    @staticmethod
    def is_paused_state(state: AppState) -> bool:
        """
        Return True if the application is paused.
        """
        return state in PAUSED_STATES

    @staticmethod
    def is_terminal_state(state: AppState) -> bool:
        """
        Return True if the state is terminal.
        """
        return state in TERMINAL_STATES

    @staticmethod
    def requires_shutdown(state: AppState) -> bool:
        """
        Return True if shutdown procedures should execute.
        """
        return state in {
            AppState.STOPPING,
            AppState.STOPPED,
            AppState.SHUTDOWN,
            AppState.ERROR,
        }


__all__ = [
    "LifecyclePhase",
    "LifecycleStates",
    "STATE_PHASE_MAP",
    "STATE_EVENT_MAP",
    "STARTUP_STATES",
    "ACTIVE_STATES",
    "PAUSED_STATES",
    "TERMINAL_STATES",
]