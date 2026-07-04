# app/core/state_manager/state_validator.py
"""
Validation utilities for the AIOS State Manager.

This module performs structural and transition validation for
system states and application state snapshots.

Responsibilities
----------------
- Validate SystemState instances
- Validate AppState snapshots
- Validate state transitions
- Register custom validators
- Aggregate validation errors

Import-safe:
    - app.core.constants
    - app.core.exceptions
    - app.core.state_manager.system_state
    - app.core.state_manager.app_state
    - app.core.state_manager.transitions

No dependencies on:
    - event_bus
    - logging
    - persistence
    - dependency_injection
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable, List, Optional, Sequence

from app.core.constants import BaseState
from app.core.exceptions import (
    InvalidStateError,
    StateValidationError,
)
from app.core.state_manager.app_state import AppState
from app.core.state_manager.system_state import SystemState
from app.core.state_manager.transitions import (
    TransitionRegistry,
    default_transition_registry,
)

__all__ = [
    "StateValidator",
    "default_state_validator",
]


Validator = Callable[[SystemState], None]


class StateValidator:
    """
    Performs validation of states and transitions.

    Supports custom validators that can be registered by feature groups.

    Examples
    --------
        validator.validate_system_state(state)

        validator.validate_transition(
            old_state,
            new_state,
        )
    """

    def __init__(
        self,
        *,
        transitions: Optional[TransitionRegistry] = None,
    ) -> None:
        self._transitions = (
            transitions or default_transition_registry
        )

        self._validators: List[Validator] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Validator registration
    # ------------------------------------------------------------------

    def register_validator(
        self,
        validator: Validator,
    ) -> None:
        """
        Register a custom validator.
        """
        if not callable(validator):
            raise TypeError(
                "validator must be callable"
            )

        with self._lock:
            if validator not in self._validators:
                self._validators.append(validator)

    def unregister_validator(
        self,
        validator: Validator,
    ) -> None:
        """
        Remove a custom validator.
        """
        with self._lock:
            try:
                self._validators.remove(validator)
            except ValueError:
                pass

    def clear_validators(self) -> None:
        """
        Remove all custom validators.
        """
        with self._lock:
            self._validators.clear()

    @property
    def validators(
        self,
    ) -> Sequence[Validator]:
        """
        Immutable snapshot of registered validators.
        """
        with self._lock:
            return tuple(self._validators)

    # ------------------------------------------------------------------
    # System state validation
    # ------------------------------------------------------------------

    def validate_system_state(
        self,
        state: SystemState,
    ) -> None:
        """
        Validate a SystemState instance.

        Raises
        ------
        StateValidationError
        """
        if not isinstance(
            state,
            SystemState,
        ):
            raise StateValidationError(
                reason="Expected SystemState instance",
            )

        if not state.name:
            raise StateValidationError(
                reason="System state name is empty",
            )

        if not isinstance(
            state.state,
            BaseState,
        ):
            raise StateValidationError(
                reason="Invalid current state",
            )

        if (
            state.previous_state is not None
            and not isinstance(
                state.previous_state,
                BaseState,
            )
        ):
            raise StateValidationError(
                reason="Invalid previous state",
            )

        with self._lock:
            validators = tuple(
                self._validators
            )

        for validator in validators:
            try:
                validator(state)
            except Exception as exc:
                raise StateValidationError(
                    reason=(
                        f"Custom validator "
                        f"'{validator.__name__}' failed"
                    ),
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # Application state validation
    # ------------------------------------------------------------------

    def validate_app_state(
        self,
        state: AppState,
    ) -> None:
        """
        Validate an application snapshot.

        Raises
        ------
        StateValidationError
        """
        if not isinstance(
            state,
            AppState,
        ):
            raise StateValidationError(
                reason="Expected AppState instance",
            )

        for system in state:
            self.validate_system_state(
                system
            )

    # ------------------------------------------------------------------
    # Transition validation
    # ------------------------------------------------------------------

    def validate_transition(
        self,
        current: BaseState,
        target: BaseState,
    ) -> None:
        """
        Validate a state transition.

        Raises
        ------
        InvalidStateTransitionError
        """
        self._transitions.require_transition(
            current,
            target,
        )

    def validate_system_transition(
        self,
        current: SystemState,
        target: BaseState,
    ) -> None:
        """
        Validate transition from a SystemState.

        Raises
        ------
        InvalidStateError
        InvalidStateTransitionError
        """
        if not isinstance(
            current,
            SystemState,
        ):
            raise InvalidStateError(
                current_state=repr(current),
                operation=(
                    "validate system transition"
                ),
            )

        self.validate_transition(
            current.state,
            target,
        )

    # ------------------------------------------------------------------
    # Batch validation
    # ------------------------------------------------------------------

    def validate_all(
        self,
        states: Iterable[SystemState],
    ) -> None:
        """
        Validate multiple states.

        Stops at first failure.
        """
        for state in states:
            self.validate_system_state(
                state
            )

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def is_valid_system_state(
        self,
        state: SystemState,
    ) -> bool:
        try:
            self.validate_system_state(
                state
            )
            return True
        except Exception:
            return False

    def is_valid_app_state(
        self,
        state: AppState,
    ) -> bool:
        try:
            self.validate_app_state(
                state
            )
            return True
        except Exception:
            return False

    def can_transition(
        self,
        current: BaseState,
        target: BaseState,
    ) -> bool:
        return self._transitions.can_transition(
            current,
            target,
        )

    def __repr__(self) -> str:
        with self._lock:
            count = len(
                self._validators
            )

        return (
            "StateValidator("
            f"validators={count}"
            ")"
        )


# ----------------------------------------------------------------------
# Process-wide default validator
# ----------------------------------------------------------------------

default_state_validator = StateValidator()