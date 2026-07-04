# app/bootstrap/shutdown.py

"""
AIOS application shutdown coordinator.

Responsibilities
----------------
1. Transition application into stopping/stopped states.
2. Publish shutdown lifecycle events.
3. Dispose root container resources.
4. Flush and close logging infrastructure.
5. Execute graceful, idempotent shutdown.

This module only coordinates shutdown of Phase 0
infrastructure. Feature groups should perform their own
cleanup before this manager is executed.
"""

from __future__ import annotations

import threading
from typing import Optional

from app.core.constants.events import LifecycleEvent
from app.core.event_bus import EventBus
from app.core.exceptions import ShutdownError
from app.dependency_injection import Container
from app.logging import Logger
from app.state import (
    AppState,
    ApplicationStateMachine,
)

__all__ = [
    "ApplicationShutdown",
]


class ApplicationShutdown:
    """
    Coordinates graceful application shutdown.

    Shutdown sequence
    -----------------

    RUNNING / PAUSED
            │
            ▼
        STOPPING
            │
            ▼
        APP_STOPPING event
            │
            ▼
        Container disposal
            │
            ▼
        APP_STOPPED event
            │
            ▼
        STOPPED
    """

    def __init__(
        self,
        container: Container,
        event_bus: EventBus,
        state_machine: ApplicationStateMachine,
        logger: Logger,
    ) -> None:
        self._container = container
        self._event_bus = event_bus
        self._state_machine = state_machine
        self._logger = logger

        self._lock = threading.RLock()
        self._shutdown = False

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    @property
    def current_state(self) -> AppState:
        return self._state_machine.current_state

    # ------------------------------------------------------------------
    # public api
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """
        Execute graceful application shutdown.

        Safe to call multiple times.
        """
        with self._lock:
            if self._shutdown:
                return

            self._logger.info(
                "AIOS shutdown initiated"
            )

            try:
                self._enter_stopping_state()
                self._publish_shutdown_events()
                self._dispose_container()
                self._enter_stopped_state()

                self._shutdown = True

                self._logger.info(
                    "AIOS shutdown completed"
                )

            except Exception as exc:
                raise ShutdownError(
                    "Application shutdown failed",
                    cause=exc,
                ) from exc

            finally:
                self._close_logger()

    # ------------------------------------------------------------------
    # phases
    # ------------------------------------------------------------------

    def _enter_stopping_state(self) -> None:
        """
        Transition to STOPPING state.
        """
        state = self.current_state

        if state not in (
            AppState.STOPPING,
            AppState.STOPPED,
        ):
            self._state_machine.transition_to(
                AppState.STOPPING
            )

    def _publish_shutdown_events(self) -> None:
        """
        Publish shutdown lifecycle events.

        Event publication failures are logged but
        must never prevent cleanup.
        """
        try:
            publisher = self._event_bus.publisher(
                "shutdown"
            )

            publisher.emit(
                LifecycleEvent.APP_STOPPING.value
            )

            publisher.emit(
                LifecycleEvent.SHUTDOWN_EVENT.value
            )

        except Exception:
            self._logger.exception(
                "Failed to publish shutdown events"
            )

    def _dispose_container(self) -> None:
        """
        Dispose root DI container.

        The container implementation already performs
        singleton disposal in reverse registration order.
        """
        try:
            self._container.dispose()

        except Exception:
            self._logger.exception(
                "Container disposal failed"
            )

    def _enter_stopped_state(self) -> None:
        """
        Finalize application shutdown.
        """
        try:
            self._state_machine.transition_to(
                AppState.STOPPED
            )

        except Exception:
            self._logger.exception(
                "Failed to enter STOPPED state"
            )

        try:
            publisher = self._event_bus.publisher(
                "shutdown"
            )

            publisher.emit(
                LifecycleEvent.APP_STOPPED.value
            )

        except Exception:
            self._logger.exception(
                "Failed to publish APP_STOPPED event"
            )

    # ------------------------------------------------------------------
    # logger cleanup
    # ------------------------------------------------------------------

    def _close_logger(self) -> None:
        """
        Flush and close logger resources.

        Logging failures must never propagate during
        shutdown.
        """
        try:
            flush = getattr(
                self._logger,
                "flush",
                None,
            )

            if callable(flush):
                flush()

            close = getattr(
                self._logger,
                "close",
                None,
            )

            if callable(close):
                close()

        except Exception:
            pass