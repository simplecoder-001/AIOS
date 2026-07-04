# app/bootstrap/startup.py

"""
AIOS application startup coordinator.

Responsibilities
----------------
1. Execute Phase 0 bootstrap initialization.
2. Transition the application state machine.
3. Publish lifecycle events.
4. Register core services in the root container.
5. Produce a fully initialized BootstrapContext.

This module does NOT start feature groups. Feature-group startup
belongs to their own bootstrap packages and higher-level runtime
managers.
"""

from __future__ import annotations

import threading
from typing import Optional

from app.bootstrap.initializer import (
    BootstrapContext,
    BootstrapInitializer,
)
from app.core.constants.events import LifecycleEvent
from app.core.event_bus import EventBus
from app.core.exceptions import StartupError
from app.dependency_injection import Container
from app.logging import Logger
from app.state import (
    ApplicationStateMachine,
    AppState,
)

__all__ = [
    "ApplicationStartup",
]


class ApplicationStartup:
    """
    Coordinates Phase 0 application startup.

    Thread-safe and idempotent.

    Startup sequence:

        CREATED
            ↓
        INITIALIZING
            ↓
        INITIALIZED
            ↓
        STARTING
            ↓
        RUNNING
    """

    def __init__(
        self,
        initializer: Optional[BootstrapInitializer] = None,
    ) -> None:
        self._initializer = (
            initializer or BootstrapInitializer()
        )

        self._context: Optional[BootstrapContext] = None
        self._started = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def context(self) -> BootstrapContext:
        if self._context is None:
            raise StartupError(
                "Application has not been started."
            )
        return self._context

    @property
    def is_started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # public api
    # ------------------------------------------------------------------

    def start(self) -> BootstrapContext:
        """
        Execute application startup.

        Returns
        -------
        BootstrapContext
            Fully initialized bootstrap context.

        Raises
        ------
        StartupError
            If startup fails.
        """
        with self._lock:
            if self._started:
                return self.context

            try:
                context = self._initializer.initialize()

                container = context.container
                logger = context.logger
                event_bus = context.event_bus
                state_machine = context.state_machine

                logger.info(
                    "Starting AIOS application"
                )

                self._transition_to_starting(
                    state_machine,
                    event_bus,
                    logger,
                )

                self._register_runtime_services(
                    container,
                    context,
                )

                self._transition_to_running(
                    state_machine,
                    event_bus,
                    logger,
                )

                self._context = context
                self._started = True

                logger.info(
                    "AIOS application started successfully"
                )

                return context

            except Exception as exc:
                raise StartupError(
                    "Failed to start AIOS application",
                    cause=exc,
                ) from exc

    # ------------------------------------------------------------------
    # startup phases
    # ------------------------------------------------------------------

    def _transition_to_starting(
        self,
        state_machine: ApplicationStateMachine,
        event_bus: EventBus,
        logger: Logger,
    ) -> None:
        """
        Transition state machine into startup state.
        """
        try:
            state_machine.transition_to(
                AppState.STARTING
            )

            publisher = event_bus.publisher(
                "bootstrap"
            )

            publisher.emit(
                LifecycleEvent.APP_INITIALIZED.value
            )

            logger.debug(
                "Application entered STARTING state"
            )

        except Exception as exc:
            raise StartupError(
                "Failed during startup transition",
                cause=exc,
            ) from exc

    def _register_runtime_services(
        self,
        container: Container,
        context: BootstrapContext,
    ) -> None:
        """
        Re-register bootstrap context as a singleton.

        Allows any subsystem to resolve the entire runtime
        context from DI.
        """
        if not container.has(BootstrapContext):
            container.register_instance(
                BootstrapContext,
                context,
            )

    def _transition_to_running(
        self,
        state_machine: ApplicationStateMachine,
        event_bus: EventBus,
        logger: Logger,
    ) -> None:
        """
        Finalize startup.
        """
        try:
            state_machine.transition_to(
                AppState.RUNNING
            )

            publisher = event_bus.publisher(
                "bootstrap"
            )

            publisher.emit(
                LifecycleEvent.APP_STARTED.value
            )

            logger.info(
                "Application entered RUNNING state"
            )

        except Exception as exc:
            raise StartupError(
                "Failed entering RUNNING state",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # lifecycle operations
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """
        Pause the application.
        """
        if not self._started:
            return

        context = self.context

        context.state_machine.transition_to(
            AppState.PAUSED
        )

        context.event_bus.publisher(
            "bootstrap"
        ).emit(
            LifecycleEvent.APP_PAUSED.value
        )

        context.logger.info(
            "Application paused"
        )

    def resume(self) -> None:
        """
        Resume application execution.
        """
        if not self._started:
            return

        context = self.context

        context.state_machine.transition_to(
            AppState.RUNNING
        )

        context.event_bus.publisher(
            "bootstrap"
        ).emit(
            LifecycleEvent.APP_RESUMED.value
        )

        context.logger.info(
            "Application resumed"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def get_container(self) -> Container:
        return self.context.container

    def get_event_bus(self) -> EventBus:
        return self.context.event_bus

    def get_state_machine(
        self,
    ) -> ApplicationStateMachine:
        return self.context.state_machine

    def get_logger(self) -> Logger:
        return self.context.logger