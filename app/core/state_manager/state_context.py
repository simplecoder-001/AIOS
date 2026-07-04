# app/core/state_manager/state_context.py
"""
Ambient state context for the AIOS State Manager.

State transitions may occur on multiple execution paths:

- EventBus dispatcher workers
- Voice/STT/TTS threads
- Async agent tasks
- Plugin execution contexts
- Recovery and persistence workers

This module provides thread-safe and async-safe access to the currently
active application state and subsystem state.

Import-safe:
    - Standard library
    - app_state
    - system_state
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

from app.core.state_manager.app_state import AppState
from app.core.state_manager.system_state import SystemState

__all__ = [
    "StateContext",
    "get_current_app_state",
    "set_current_app_state",
    "reset_current_app_state",
    "get_current_system_state",
    "set_current_system_state",
    "reset_current_system_state",
    "use_app_state",
    "use_system_state",
]


# ---------------------------------------------------------------------------
# Context variables
# ---------------------------------------------------------------------------

_CURRENT_APP_STATE: ContextVar[Optional[AppState]] = ContextVar(
    "aios_current_app_state",
    default=None,
)

_CURRENT_SYSTEM_STATE: ContextVar[Optional[SystemState]] = ContextVar(
    "aios_current_system_state",
    default=None,
)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def get_current_app_state() -> Optional[AppState]:
    """
    Return the currently active AppState.

    Returns
    -------
    Optional[AppState]
    """
    return _CURRENT_APP_STATE.get()


def set_current_app_state(
    state: Optional[AppState],
) -> Token[Optional[AppState]]:
    """
    Set the current AppState.

    Returns
    -------
    Token
        Token required for reset().
    """
    return _CURRENT_APP_STATE.set(state)


def reset_current_app_state(
    token: Token[Optional[AppState]],
) -> None:
    """
    Restore the previous AppState.
    """
    _CURRENT_APP_STATE.reset(token)


def get_current_system_state() -> Optional[SystemState]:
    """
    Return the currently active SystemState.
    """
    return _CURRENT_SYSTEM_STATE.get()


def set_current_system_state(
    state: Optional[SystemState],
) -> Token[Optional[SystemState]]:
    """
    Set the current SystemState.

    Returns
    -------
    Token
        Token required for reset().
    """
    return _CURRENT_SYSTEM_STATE.set(state)


def reset_current_system_state(
    token: Token[Optional[SystemState]],
) -> None:
    """
    Restore the previous SystemState.
    """
    _CURRENT_SYSTEM_STATE.reset(token)


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


@contextmanager
def use_app_state(
    state: Optional[AppState],
) -> Iterator[Optional[AppState]]:
    """
    Temporarily bind an AppState.

    Example
    -------
    with use_app_state(app_state):
        ...
    """
    token = set_current_app_state(state)

    try:
        yield state
    finally:
        reset_current_app_state(token)


@contextmanager
def use_system_state(
    state: Optional[SystemState],
) -> Iterator[Optional[SystemState]]:
    """
    Temporarily bind a SystemState.

    Example
    -------
    with use_system_state(voice_state):
        ...
    """
    token = set_current_system_state(state)

    try:
        yield state
    finally:
        reset_current_system_state(token)


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


class StateContext:
    """
    Ambient state context accessor.

    This class is intentionally stateless and only wraps ContextVar
    operations with a cleaner API.

    Example
    -------
    StateContext.set_app_state(app_state)
    current = StateContext.current_app_state()

    with StateContext.bind_system_state(state):
        ...
    """

    @staticmethod
    def current_app_state() -> Optional[AppState]:
        return get_current_app_state()

    @staticmethod
    def current_system_state() -> Optional[SystemState]:
        return get_current_system_state()

    @staticmethod
    def set_app_state(
        state: Optional[AppState],
    ) -> Token[Optional[AppState]]:
        return set_current_app_state(state)

    @staticmethod
    def set_system_state(
        state: Optional[SystemState],
    ) -> Token[Optional[SystemState]]:
        return set_current_system_state(state)

    @staticmethod
    def reset_app_state(
        token: Token[Optional[AppState]],
    ) -> None:
        reset_current_app_state(token)

    @staticmethod
    def reset_system_state(
        token: Token[Optional[SystemState]],
    ) -> None:
        reset_current_system_state(token)

    @staticmethod
    def bind_app_state(
        state: Optional[AppState],
    ):
        return use_app_state(state)

    @staticmethod
    def bind_system_state(
        state: Optional[SystemState],
    ):
        return use_system_state(state)