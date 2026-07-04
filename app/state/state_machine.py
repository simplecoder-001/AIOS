# app/state/state_machine.py

from __future__ import annotations

from threading import RLock
from typing import Optional

from app.core.event_bus.bus import EventBus
from app.core.exceptions.state import (
    InvalidStateTransitionError,
    StateError,
)
from app.core.state_manager.state_machine import StateMachine
from app.logging.logger_factory import LoggerFactory
from app.state.app_state import AppState
from app.state.lifecycle_states import LifecycleStates


class ApplicationStateMachine:
    """
    Top-level application lifecycle state machine.

    Wraps the generic core StateMachine and adds:
        • lifecycle event publishing
        • application logging
        • convenience transition helpers
    """

    def __init__(
        self,
        event_bus: EventBus,
        logger_factory: LoggerFactory,
    ) -> None:
        self._event_bus = event_bus
        self._logger = logger_factory.get_logger(
            "app.state.state_machine"
        )

        self._lock = RLock()

        self._machine = StateMachine[AppState](
            initial_state=AppState.CREATED
        )

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def current_state(self) -> AppState:
        """
        Return the current application state.
        """
        return self._machine.current_state

    @property
    def phase(self):
        """
        Return the current lifecycle phase.
        """
        return LifecycleStates.phase_of(
            self.current_state
        )

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #

    def transition_to(self, state: AppState) -> AppState:
        """
        Transition application state.

        Raises:
            InvalidStateTransitionError
            StateError
        """
        with self._lock:
            current = self.current_state

            if current is state:
                return current

            try:
                self._machine.transition_to(state)
            except Exception as exc:
                raise StateError(
                    f"Failed to transition "
                    f"{current.value} -> {state.value}"
                ) from exc

            self._publish_state_change(
                previous=current,
                current=state,
            )

            return state

    # ------------------------------------------------------------------ #
    # Convenience transitions
    # ------------------------------------------------------------------ #

    def bootstrap(self) -> AppState:
        return self.transition_to(
            AppState.BOOTSTRAPPING
        )

    def initialize(self) -> AppState:
        return self.transition_to(
            AppState.INITIALIZING
        )

    def start(self) -> AppState:
        return self.transition_to(
            AppState.STARTING
        )

    def run(self) -> AppState:
        return self.transition_to(
            AppState.RUNNING
        )

    def pause(self) -> AppState:
        return self.transition_to(
            AppState.PAUSED
        )

    def resume(self) -> AppState:
        return self.transition_to(
            AppState.RESUMING
        )

    def degrade(self) -> AppState:
        return self.transition_to(
            AppState.DEGRADED
        )

    def stop(self) -> AppState:
        return self.transition_to(
            AppState.STOPPED
        )

    def shutdown(self) -> AppState:
        return self.transition_to(
            AppState.SHUTDOWN
        )

    def fail(self) -> AppState:
        return self.transition_to(
            AppState.ERROR
        )

    # ------------------------------------------------------------------ #
    # State queries
    # ------------------------------------------------------------------ #

    def is_running(self) -> bool:
        return LifecycleStates.is_active_state(
            self.current_state
        )

    def is_paused(self) -> bool:
        return LifecycleStates.is_paused_state(
            self.current_state
        )

    def is_terminal(self) -> bool:
        return LifecycleStates.is_terminal_state(
            self.current_state
        )

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _publish_state_change(
        self,
        previous: AppState,
        current: AppState,
    ) -> None:
        """
        Publish lifecycle events and emit logs.
        """
        try:
            event = LifecycleStates.event_for(
                current
            )

            payload = {
                "previous_state": previous.value,
                "current_state": current.value,
                "phase": self.phase.value,
            }

            self._logger.info(
                "Application state changed",
                extra=payload,
            )

            if event is not None:
                self._event_bus.publish(
                    event.value,
                    payload=payload,
                )

        except Exception as exc:
            self._logger.exception(
                "Failed to publish lifecycle event",
                extra={
                    "previous_state": previous.value,
                    "current_state": current.value,
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------ #
    # Dunder methods
    # ------------------------------------------------------------------ #

    def __str__(self) -> str:
        return self.current_state.value

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}"
            f"(state={self.current_state.value!r})"
        )


__all__ = [
    "ApplicationStateMachine",
]