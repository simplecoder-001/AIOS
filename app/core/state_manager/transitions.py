# app/core/state_manager/transitions.py
"""
State transition rules for the AIOS State Manager.

This module centralizes transition validation for all registered state
machines. The top-level application lifecycle already exposes an
APP_STATE_TRANSITIONS table in app.core.constants, while feature-group
state machines may register additional rules at runtime.

Responsibilities
----------------
- Store legal state transitions
- Validate transitions
- Register/unregister transition rules
- Provide immutable views of transition tables

Import-safe:
    - app.core.constants
    - app.core.exceptions

No dependencies on:
    - event_bus
    - logging
    - persistence
    - dependency_injection
"""

from __future__ import annotations

import threading
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, MutableMapping

from app.core.constants import (
    APP_STATE_TRANSITIONS,
    AppState,
    BaseState,
    can_transition,
)
from app.core.exceptions import InvalidStateTransitionError

__all__ = [
    "TransitionRegistry",
    "default_transition_registry",
]


class TransitionRegistry:
    """
    Thread-safe registry of legal state transitions.

    A subsystem owns a directed graph:

        current_state -> {allowed_next_states}

    Example
    -------
        registry.register(
            VoiceState.IDLE,
            [
                VoiceState.LISTENING,
                VoiceState.DISABLED,
            ],
        )

        registry.can_transition(
            VoiceState.IDLE,
            VoiceState.LISTENING,
        )
    """

    def __init__(self) -> None:
        self._transitions: Dict[
            BaseState,
            frozenset[BaseState],
        ] = {}

        self._lock = threading.RLock()

        # Bootstrap application lifecycle transitions
        self._transitions.update(
            {
                state: frozenset(targets)
                for state, targets in APP_STATE_TRANSITIONS.items()
            }
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        state: BaseState,
        next_states: Iterable[BaseState],
        *,
        replace: bool = False,
    ) -> None:
        """
        Register legal successor states.

        Raises
        ------
        ValueError
            If state values are invalid.
        """
        if not isinstance(state, BaseState):
            raise TypeError(
                f"Expected BaseState, got {type(state)!r}"
            )

        allowed = frozenset(next_states)

        for item in allowed:
            if not isinstance(item, BaseState):
                raise TypeError(
                    f"Invalid state: {item!r}"
                )

        with self._lock:
            if (
                state in self._transitions
                and not replace
            ):
                merged = (
                    self._transitions[state]
                    | allowed
                )
                self._transitions[state] = merged
            else:
                self._transitions[state] = allowed

    def unregister(
        self,
        state: BaseState,
    ) -> None:
        """
        Remove transition rules for a state.

        Application lifecycle transitions cannot be removed.
        """
        if state in APP_STATE_TRANSITIONS:
            return

        with self._lock:
            self._transitions.pop(state, None)

    def clear(self) -> None:
        """
        Remove all custom transitions while preserving
        application lifecycle transitions.
        """
        with self._lock:
            self._transitions.clear()

            self._transitions.update(
                {
                    state: frozenset(targets)
                    for state, targets
                    in APP_STATE_TRANSITIONS.items()
                }
            )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def can_transition(
        self,
        current: BaseState,
        target: BaseState,
    ) -> bool:
        """
        Return True if target is a legal successor.
        """
        if (
            isinstance(current, AppState)
            and isinstance(target, AppState)
        ):
            return can_transition(
                current,
                target,
            )

        with self._lock:
            allowed = self._transitions.get(
                current,
            )

        if allowed is None:
            return False

        return target in allowed

    def require_transition(
        self,
        current: BaseState,
        target: BaseState,
    ) -> None:
        """
        Validate a transition.

        Raises
        ------
        InvalidStateTransitionError
        """
        if self.can_transition(
            current,
            target,
        ):
            return

        allowed = self.allowed_next_states(
            current,
        )

        raise InvalidStateTransitionError(
            from_state=current,
            to_state=target,
            allowed=allowed,
        )

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def allowed_next_states(
        self,
        state: BaseState,
    ) -> frozenset[BaseState]:
        """
        Return legal successor states.
        """
        with self._lock:
            return self._transitions.get(
                state,
                frozenset(),
            )

    def has_state(
        self,
        state: BaseState,
    ) -> bool:
        """
        Return True if a state has transition rules.
        """
        with self._lock:
            return state in self._transitions

    @property
    def transitions(
        self,
    ) -> Mapping[
        BaseState,
        frozenset[BaseState],
    ]:
        """
        Immutable view of the transition graph.
        """
        with self._lock:
            snapshot: MutableMapping[
                BaseState,
                frozenset[BaseState],
            ] = dict(self._transitions)

        return MappingProxyType(snapshot)

    def __len__(self) -> int:
        return len(self._transitions)

    def __contains__(
        self,
        state: BaseState,
    ) -> bool:
        return self.has_state(state)

    def __repr__(self) -> str:
        return (
            "TransitionRegistry("
            f"states={len(self._transitions)}"
            ")"
        )


# ----------------------------------------------------------------------
# Process-wide default registry
# ----------------------------------------------------------------------

default_transition_registry = TransitionRegistry()