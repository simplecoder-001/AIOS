# app/bootstrap/lifecycle_manager.py

"""
Application lifecycle manager.

Responsibilities
----------------
1. Own high-level application lifecycle operations.
2. Coordinate state-machine transitions.
3. Publish lifecycle events.
4. Provide pause, resume, restart, and stop APIs.
5. Keep lifecycle operations thread-safe and idempotent.

This class does not perform initialization or shutdown logic itself.
It orchestrates lifecycle transitions and delegates actual startup/
shutdown work to their dedicated managers.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Optional

from app.core.constants.events import LifecycleEvent
from app.core.event_bus import EventBus
from app.core.exceptions import RuntimeError as AIOSRuntimeError
from app.logging import Logger
from app.state import (
    AppState,
    ApplicationStateMachine,
)

__all__ = [
    "LifecycleOperation",
    "LifecycleManager",
]


class LifecycleOperation(str, Enum):
    """
    Lifecycle operations supported by the manager.
    """

    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    RESTART = "restart"


class LifecycleManager:
    """
    Coordinates application lifecycle transitions.

    State flow
    ----------

    RUNNING
       │
       ├── pause()  ───► PAUSED
       │
       └── stop() ─────► STOPPING
                               │
                               ▼
                            STOPPED

    PAUSED
       │
       └── resume() ───► RUNNING
    """

    def __init__(
        self,
        state_machine: ApplicationStateMachine,
        event_bus: EventBus,
        logger: Logger,
    ) -> None:
        self._state_machine = state_machine
        self._event_bus = event_bus
        self._logger = logger

        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def state_machine(self) -> ApplicationStateMachine:
        return self._state_machine

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    @property
    def logger(self) -> Logger:
        return self._logger

    @property
    def current_state(self) -> AppState:
        return self._state_machine.current_state

    # ------------------------------------------------------------------
    # pause
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """
        Pause the application.

        Raises
        ------
        AIOSRuntimeError
            If the transition fails.
        """
        with self._lock:
            try:
                if self.current_state == AppState.PAUSED:
                    return

                self._logger.info(
                    "Pausing application"
                )

                self._state_machine.transition_to(
                    AppState.PAUSED
                )

                self._publish(
                    LifecycleEvent.APP_PAUSED
                )

                self._logger.info(
                    "Application paused"
                )

            except Exception as exc:
                raise AIOSRuntimeError(
                    "Failed to pause application",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # resume
    # ------------------------------------------------------------------

    def resume(self) -> None:
        """
        Resume application execution.
        """
        with self._lock:
            try:
                if self.current_state == AppState.RUNNING:
                    return

                self._logger.info(
                    "Resuming application"
                )

                self._state_machine.transition_to(
                    AppState.RUNNING
                )

                self._publish(
                    LifecycleEvent.APP_RESUMED
                )

                self._logger.info(
                    "Application resumed"
                )

            except Exception as exc:
                raise AIOSRuntimeError(
                    "Failed to resume application",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # stop
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """
        Transition application into stopping state.

        Actual resource cleanup belongs to
        app.bootstrap.shutdown.
        """
        with self._lock:
            try:
                state = self.current_state

                if state in (
                    AppState.STOPPING,
                    AppState.STOPPED,
                ):
                    return

                self._logger.info(
                    "Stopping application"
                )

                self._state_machine.transition_to(
                    AppState.STOPPING
                )

                self._publish(
                    LifecycleEvent.APP_STOPPING
                )

            except Exception as exc:
                raise AIOSRuntimeError(
                    "Failed to stop application",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # mark stopped
    # ------------------------------------------------------------------

    def mark_stopped(self) -> None:
        """
        Finalize stop sequence.
        """
        with self._lock:
            try:
                if self.current_state == AppState.STOPPED:
                    return

                self._state_machine.transition_to(
                    AppState.STOPPED
                )

                self._publish(
                    LifecycleEvent.APP_STOPPED
                )

                self._logger.info(
                    "Application stopped"
                )

            except Exception as exc:
                raise AIOSRuntimeError(
                    "Failed to finalize shutdown",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # restart
    # ------------------------------------------------------------------

    def restart(self) -> None:
        """
        Restart lifecycle.

        Actual bootstrap execution is handled by
        startup.py. This method only transitions
        states and emits events.
        """
        with self._lock:
            try:
                self._logger.info(
                    "Restarting application"
                )

                self.stop()
                self.mark_stopped()

                self._state_machine.transition_to(
                    AppState.STARTING
                )

                self._publish(
                    LifecycleEvent.APP_STARTED
                )

                self._state_machine.transition_to(
                    AppState.RUNNING
                )

                self._logger.info(
                    "Application restarted"
                )

            except Exception as exc:
                raise AIOSRuntimeError(
                    "Failed to restart application",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def can_pause(self) -> bool:
        return self.current_state == AppState.RUNNING

    def can_resume(self) -> bool:
        return self.current_state == AppState.PAUSED

    def can_stop(self) -> bool:
        return self.current_state not in (
            AppState.STOPPING,
            AppState.STOPPED,
        )

    def can_restart(self) -> bool:
        return self.current_state in (
            AppState.RUNNING,
            AppState.PAUSED,
        )

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _publish(
        self,
        event: LifecycleEvent,
        payload: Optional[dict] = None,
    ) -> None:
        """
        Publish lifecycle events.

        Event bus failures must never leave the
        state machine in an inconsistent state.
        """
        try:
            publisher = self._event_bus.publisher(
                "lifecycle_manager"
            )

            publisher.emit(
                event.value,
                payload=payload,
            )

        except Exception:
            self._logger.exception(
                "Failed to publish lifecycle event",
                extra={
                    "event": event.value,
                },
            )