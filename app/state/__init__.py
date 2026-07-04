# app/state/__init__.py

"""
AIOS application state package.

Provides the application's top-level lifecycle state abstractions and
the application-specific state machine used by bootstrap and runtime
management.

Usage
-----
    from app.state import (
        AppState,
        LifecyclePhase,
        LifecycleStates,
        ApplicationStateMachine,
    )

    state_machine = ApplicationStateMachine(
        event_bus,
        logger_factory,
    )

    state_machine.bootstrap()
    state_machine.initialize()
    state_machine.run()

Architecture
------------
* app_state.py
    Public application lifecycle states.

* lifecycle_states.py
    Lifecycle phases, state metadata, and helper utilities.

* state_machine.py
    Application-specific wrapper around the core state manager that
    publishes lifecycle events and integrates with logging.
"""

from __future__ import annotations

# ---------------------------------------------------------------------
# Application states
# ---------------------------------------------------------------------

from app.state.app_state import (
    ACTIVE_APP_STATES,
    TERMINAL_APP_STATES,
    AppState,
)

# ---------------------------------------------------------------------
# Lifecycle metadata
# ---------------------------------------------------------------------

from app.state.lifecycle_states import (
    ACTIVE_STATES,
    PAUSED_STATES,
    STARTUP_STATES,
    STATE_EVENT_MAP,
    STATE_PHASE_MAP,
    TERMINAL_STATES,
    LifecyclePhase,
    LifecycleStates,
)

# ---------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------

from app.state.state_machine import (
    ApplicationStateMachine,
)

__all__ = [
    # application states
    "AppState",
    "ACTIVE_APP_STATES",
    "TERMINAL_APP_STATES",

    # lifecycle
    "LifecyclePhase",
    "LifecycleStates",
    "STATE_PHASE_MAP",
    "STATE_EVENT_MAP",
    "STARTUP_STATES",
    "ACTIVE_STATES",
    "PAUSED_STATES",
    "TERMINAL_STATES",

    # state machine
    "ApplicationStateMachine",
]

__version__ = "1.0.0"