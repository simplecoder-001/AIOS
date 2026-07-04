# app/core/state_manager/state_registry.py
"""
Central registry for all subsystem states.

The StateRegistry is the authoritative in-memory source of truth for every
registered subsystem state in AIOS. It coordinates:

- Subsystem registration
- State lookup
- State replacement
- State transitions
- History recording
- Snapshot generation support

The registry itself does not publish events or persist state. Those concerns
belong to StateMachine and StatePersistence.

Dependencies
------------
- system_state.py
- app_state.py
- state_history.py
- state_validator.py
"""

from __future__ import annotations

import threading
from types import MappingProxyType
from typing import Mapping, Optional

from app.core.constants import INITIAL_STATES
from app.core.exceptions import (
    InvalidStateError,
    StateValidationError,
)
from app.core.state_manager.app_state import AppState
from app.core.state_manager.state_history import StateHistory
from app.core.state_manager.state_validator import (
    StateValidator,
    default_state_validator,
)
from app.core.state_manager.system_state import SystemState

__all__ = [
    "StateRegistry",
    "default_state_registry",
]


class StateRegistry:
    """
    Thread-safe registry of subsystem states.

    The registry owns the current state of each subsystem and records every
    change in StateHistory.

    Example
    -------
        registry = StateRegistry()

        voice = registry.require("voice")

        registry.update(
            voice.transition_to(
                VoiceState.LISTENING
            )
        )
    """

    def __init__(
        self,
        *,
        validator: Optional[StateValidator] = None,
        history: Optional[StateHistory] = None,
        bootstrap: bool = True,
    ) -> None:
        self._validator = (
            validator
            or default_state_validator
        )

        self._history = (
            history
            or StateHistory()
        )

        self._states: dict[
            str,
            SystemState,
        ] = {}

        self._lock = threading.RLock()

        if bootstrap:
            self._bootstrap()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _bootstrap(self) -> None:
        """
        Initialize subsystem states from INITIAL_STATES.
        """
        for name, state in INITIAL_STATES.items():
            self.register(
                SystemState(
                    name=name,
                    state=state,
                )
            )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        state: SystemState,
        *,
        replace: bool = False,
    ) -> None:
        """
        Register a subsystem state.
        """
        self._validator.validate_system_state(
            state
        )

        with self._lock:
            if (
                state.name in self._states
                and not replace
            ):
                raise InvalidStateError(
                    current_state=state.name,
                    operation=(
                        "register duplicate "
                        "subsystem state"
                    ),
                )

            self._states[
                state.name
            ] = state

            self._history.record(
                state
            )

    def unregister(
        self,
        name: str,
    ) -> None:
        """
        Remove a subsystem state.
        """
        with self._lock:
            self._states.pop(
                name,
                None,
            )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(
        self,
        name: str,
        default: Optional[
            SystemState
        ] = None,
    ) -> Optional[SystemState]:
        with self._lock:
            return self._states.get(
                name,
                default,
            )

    def require(
        self,
        name: str,
    ) -> SystemState:
        """
        Return subsystem state.

        Raises
        ------
        InvalidStateError
        """
        state = self.get(name)

        if state is None:
            raise InvalidStateError(
                current_state=name,
                operation=(
                    "access unregistered "
                    "subsystem state"
                ),
            )

        return state

    def exists(
        self,
        name: str,
    ) -> bool:
        with self._lock:
            return name in self._states

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def update(
        self,
        state: SystemState,
    ) -> None:
        """
        Replace an existing subsystem state.
        """
        self._validator.validate_system_state(
            state
        )

        with self._lock:
            if state.name not in self._states:
                raise InvalidStateError(
                    current_state=state.name,
                    operation=(
                        "update unregistered "
                        "subsystem state"
                    ),
                )

            self._states[
                state.name
            ] = state

            self._history.record(
                state
            )

    def transition(
        self,
        name: str,
        new_state,
        *,
        metadata=None,
    ) -> SystemState:
        """
        Transition a subsystem to a new state.

        Raises
        ------
        InvalidStateTransitionError
        StateValidationError
        """
        current = self.require(
            name
        )

        self._validator.validate_system_transition(
            current,
            new_state,
        )

        next_state = (
            current.transition_to(
                new_state,
                metadata=metadata,
            )
        )

        self.update(
            next_state
        )

        return next_state

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def app_state(self) -> AppState:
        """
        Build an immutable application snapshot.
        """
        with self._lock:
            snapshot = (
                MappingProxyType(
                    dict(
                        self._states
                    )
                )
            )

        app_state = AppState(
            systems=snapshot,
        )

        self._validator.validate_app_state(
            app_state
        )

        return app_state

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    @property
    def history(
        self,
    ) -> StateHistory:
        return self._history

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    @property
    def states(
        self,
    ) -> Mapping[
        str,
        SystemState,
    ]:
        with self._lock:
            snapshot = dict(
                self._states
            )

        return MappingProxyType(
            snapshot
        )

    @property
    def validator(
        self,
    ) -> StateValidator:
        return self._validator

    def names(
        self,
    ) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                self._states.keys()
            )

    def clear(self) -> None:
        """
        Remove all registered states.
        """
        with self._lock:
            self._states.clear()

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __contains__(
        self,
        name: str,
    ) -> bool:
        return self.exists(
            name
        )

    def __len__(
        self,
    ) -> int:
        with self._lock:
            return len(
                self._states
            )

    def __iter__(
        self,
    ):
        return iter(
            self.names()
        )

    def __repr__(
        self,
    ) -> str:
        return (
            "StateRegistry("
            f"systems={len(self)}, "
            f"history={self.history.total_count()}"
            ")"
        )


# ----------------------------------------------------------------------
# Process-wide singleton registry
# ----------------------------------------------------------------------

default_state_registry = StateRegistry()