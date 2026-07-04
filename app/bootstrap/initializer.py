# app/bootstrap/initializer.py

"""
Phase 0 bootstrap initializer.

Responsible for creating and wiring the application's foundational
infrastructure in the correct order:

    Environment
        ↓
    Configuration
        ↓
    Logging
        ↓
    Dependency Injection
        ↓
    Event Bus
        ↓
    Application State Machine

This module performs infrastructure initialization only.
It does NOT start feature groups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.configs import (
    ConfigManager,
    initialize_configuration,
)
from app.core.constants.events import LifecycleEvent
from app.core.exceptions import (
    BootstrapError,
    InitializationError,
)
from app.core.event_bus import EventBus, register_event_bus
from app.dependency_injection import (
    Container,
    build_root_container,
)
from app.logging import (
    Logger,
    LoggerFactory,
)
from app.state import ApplicationStateMachine


__all__ = [
    "BootstrapContext",
    "BootstrapInitializer",
]


# =====================================================================
# Bootstrap Context
# =====================================================================

@dataclass(frozen=True, slots=True)
class BootstrapContext:
    """
    Immutable container holding all core runtime dependencies.
    """

    config: ConfigManager
    logger_factory: LoggerFactory
    logger: Logger
    container: Container
    event_bus: EventBus
    state_machine: ApplicationStateMachine


# =====================================================================
# Bootstrap Initializer
# =====================================================================

class BootstrapInitializer:
    """
    Builds and wires all Phase 0 infrastructure.

    Initialization Order
    --------------------
        1. Configuration
        2. DI Container + Logging
        3. Event Bus
        4. Application State Machine
    """

    def __init__(self) -> None:
        self._context: Optional[BootstrapContext] = None

    # -----------------------------------------------------------------
    # properties
    # -----------------------------------------------------------------

    @property
    def context(self) -> BootstrapContext:
        if self._context is None:
            raise BootstrapError(
                stage="initializer",
                cause=RuntimeError("Bootstrap has not been initialized."),
            )
        return self._context

    @property
    def is_initialized(self) -> bool:
        return self._context is not None

    # -----------------------------------------------------------------
    # initialization
    # -----------------------------------------------------------------

    def initialize(self) -> BootstrapContext:
        """
        Create and wire all Phase 0 services.

        Returns
        -------
        BootstrapContext
        """
        if self._context is not None:
            return self._context

        try:
            # ---------------------------------------------------------
            # Configuration
            # ---------------------------------------------------------
            config = initialize_configuration()

            # ---------------------------------------------------------
            # DI + Logging
            # ---------------------------------------------------------
            container = build_root_container(
                log_dir=config.paths.logs_dir,
            )

            logger = container.resolve(Logger)
            logger_factory = container.resolve(LoggerFactory)

            logger.info("Initializing AIOS bootstrap infrastructure")

            # ---------------------------------------------------------
            # Event Bus
            # ---------------------------------------------------------
            event_bus = register_event_bus(
                container,
                strict=True,
            )

            # ---------------------------------------------------------
            # Application State Machine
            # ---------------------------------------------------------
            state_machine = ApplicationStateMachine(
                event_bus=event_bus,
            )

            # ---------------------------------------------------------
            # Register core services into DI
            # ---------------------------------------------------------
            container.register_instance(
                ConfigManager,
                config,
                replace=True,
            )

            container.register_instance(
                EventBus,
                event_bus,
                replace=True,
            )

            container.register_instance(
                ApplicationStateMachine,
                state_machine,
                replace=True,
            )

            # ---------------------------------------------------------
            # Publish bootstrap event
            # ---------------------------------------------------------
            try:
                publisher = event_bus.publisher("bootstrap")

                publisher.emit(
                    LifecycleEvent.APP_BOOTSTRAP_STARTED.value
                )
            except Exception:
                # bootstrap must remain resilient
                logger.exception(
                    "Failed to publish bootstrap event"
                )

            # ---------------------------------------------------------
            # Build context
            # ---------------------------------------------------------
            self._context = BootstrapContext(
                config=config,
                logger_factory=logger_factory,
                logger=logger,
                container=container,
                event_bus=event_bus,
                state_machine=state_machine,
            )

            logger.info(
                "Phase 0 bootstrap initialization completed"
            )

            return self._context

        except Exception as exc:
            raise InitializationError(
                component="bootstrap_initializer",
                cause=exc,
            ) from exc

    # -----------------------------------------------------------------
    # reset
    # -----------------------------------------------------------------

    def reset(self) -> None:
        """
        Clear cached bootstrap context.

        Intended primarily for testing.
        """
        self._context = None