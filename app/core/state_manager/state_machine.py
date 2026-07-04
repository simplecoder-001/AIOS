# app/core/state_manager/state_machine.py
"""
High-level state orchestration engine for AIOS.

The StateMachine is the public façade over the State Manager subsystem.
It coordinates:

- StateRegistry
- StateValidator
- StateHistory
- StateSnapshot creation
- State transition execution
- Event publication (optional)
- Snapshot persistence hooks (optional)

The StateMachine is intentionally thin and orchestration-focused. Actual
storage lives in StateRegistry, validation lives in StateValidator, and
persistence lives in StatePersistence.

Thread-safe and suitable for use across:
    - EventBus workers
    - Voice/STT/TTS threads
    - Agent executors
    - Plugin workers
    - Recovery tasks
"""

from __future__ import annotations

import threading
from typing import Any, Mapping, Optional

from app.core.event_bus import EventBus
from app.core.exceptions import (
    InvalidStateError,
    StateLockError,
)
from app.core.state_manager.app_state import AppState
from app.core.state_manager.state_registry import (
    StateRegistry,
    default_state_registry,
)
from app.core.state_manager.state_snapshot import StateSnapshot
from app.core.state_manager.system_state import SystemState

__all__ = [
    "StateMachine",
]


class StateMachine:
    """
    High-level state orchestration engine.

    Example
    -------
        machine = StateMachine()

        machine.transition(
            "voice",
            VoiceState.LISTENING,
        )

        snapshot = machine.snapshot()
    """

    def __init__(
        self,
        *,
        registry: Optional[StateRegistry] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self._registry = (
            registry
            or default_state_registry
        )

        self._event_bus = event_bus

        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    @property
    def registry(self) -> StateRegistry:
        return self._registry

    @property
    def event_bus(self) -> Optional[EventBus]:
        return self._event_bus

    def app_state(self) -> AppState:
        """
        Return immutable application state.
        """
        return self._registry.app_state()

    def state(
        self,
        name: str,
    ) -> SystemState:
        """
        Return subsystem state.
        """
        return self._registry.require(
            name
        )

    def states(
        self,
    ) -> Mapping[str, SystemState]:
        """
        Return immutable subsystem mapping.
        """
        return self._registry.states

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
        with self._lock:
            self._registry.register(
                state,
                replace=replace,
            )

    def unregister(
        self,
        name: str,
    ) -> None:
        """
        Remove subsystem state.
        """
        with self._lock:
            self._registry.unregister(
                name
            )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(
        self,
        name: str,
        new_state,
        *,
        metadata: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> SystemState:
        """
        Execute a validated state transition.

        Raises
        ------
        InvalidStateError
        InvalidStateTransitionError
        StateValidationError
        """
        try:
            with self._lock:
                previous = (
                    self._registry.require(
                        name
                    )
                )

                current = (
                    self._registry.transition(
                        name,
                        new_state,
                        metadata=metadata,
                    )
                )

            self._publish_transition(
                previous,
                current,
            )

            return current

        except Exception as exc:
            raise StateLockError(
                operation=(
                    f"transition:{name}"
                ),
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(
        self,
        *,
        metadata: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> StateSnapshot:
        """
        Create point-in-time application snapshot.
        """
        state = self.app_state()

        return StateSnapshot.from_app_state(
            state,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Rollback support
    # ------------------------------------------------------------------

    def rollback(
        self,
        name: str,
    ) -> SystemState:
        """
        Roll back subsystem state to the previous entry.

        Raises
        ------
        InvalidStateError
        """
        history = (
            self._registry.history
        )

        previous = history.previous(
            name
        )

        if previous is None:
            raise InvalidStateError(
                current_state=name,
                operation=(
                    "rollback without "
                    "previous state"
                ),
            )

        with self._lock:
            self._registry.update(
                previous
            )

        return previous

    # ------------------------------------------------------------------
    # Event publication
    # ------------------------------------------------------------------

    def _publish_transition(
        self,
        previous: SystemState,
        current: SystemState,
    ) -> None:
        """
        Publish transition events if an EventBus
        is attached.

        StateEvents are defined in state_events.py
        and imported lazily to avoid cycles.
        """
        if self._event_bus is None:
            return

        try:
            from app.core.state_manager.state_events import (
                StateTransitionEvent,
            )

            event = (
                StateTransitionEvent(
                    system=current.name,
                    previous_state=(
                        previous.state
                    ),
                    current_state=(
                        current.state
                    ),
                    version=(
                        current.version
                    ),
                )
            )

            self._event_bus.publish(
                event
            )

        except Exception:
            # State changes must never fail because
            # event publication failed.
            pass

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def exists(
        self,
        name: str,
    ) -> bool:
        return self._registry.exists(
            name
        )

    def clear(self) -> None:
        """
        Remove all registered states.
        """
        with self._lock:
            self._registry.clear()

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
        return len(
            self._registry
        )

    def __repr__(
        self,
    ) -> str:
        return (
            "StateMachine("
            f"systems={len(self)}, "
            f"event_bus={self._event_bus is not None}"
            ")"
        )