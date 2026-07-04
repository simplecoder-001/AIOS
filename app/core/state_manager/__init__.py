# app/core/state_manager/__init__.py
"""
AIOS State Manager package.
===========================

The State Manager is the central state orchestration subsystem of AIOS.
It provides:

- Immutable subsystem states
- Immutable application snapshots
- Ambient state contexts
- Transition validation
- State history and rollback support
- Snapshot creation and persistence
- State event publication
- High-level state orchestration

Architecture
------------
SystemState
      ↓
AppState
      ↓
StateContext
      ↓
TransitionRegistry
      ↓
StateValidator
      ↓
StateHistory
      ↓
StateSnapshot
      ↓
StateRegistry
      ↓
StateMachine
      ↓
StatePersistence
      ↓
StateEvents

Usage
-----
    from app.core.state_manager import (
        StateMachine,
        SystemState,
        AppState,
        StateSnapshot,
    )

    machine = StateMachine()
    state = machine.state("voice")
    snapshot = machine.snapshot()
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# Core state models
# ----------------------------------------------------------------------

from app.core.state_manager.system_state import (
    SystemState,
)

from app.core.state_manager.app_state import (
    AppState,
)

# ----------------------------------------------------------------------
# Ambient context
# ----------------------------------------------------------------------

from app.core.state_manager.state_context import (
    StateContext,
    get_current_app_state,
    set_current_app_state,
    reset_current_app_state,
    get_current_system_state,
    set_current_system_state,
    reset_current_system_state,
    use_app_state,
    use_system_state,
)

# ----------------------------------------------------------------------
# Transitions and validation
# ----------------------------------------------------------------------

from app.core.state_manager.transitions import (
    TransitionRegistry,
    default_transition_registry,
)

from app.core.state_manager.state_validator import (
    StateValidator,
    default_state_validator,
)

# ----------------------------------------------------------------------
# History and snapshots
# ----------------------------------------------------------------------

from app.core.state_manager.state_history import (
    StateHistory,
)

from app.core.state_manager.state_snapshot import (
    StateSnapshot,
)

# ----------------------------------------------------------------------
# Registry and orchestration
# ----------------------------------------------------------------------

from app.core.state_manager.state_registry import (
    StateRegistry,
    default_state_registry,
)

from app.core.state_manager.state_machine import (
    StateMachine,
)

from app.core.state_manager.state_persistence import (
    StatePersistence,
)

# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------

from app.core.state_manager.state_events import (
    StateEvent,
    StateTransitionEvent,
    StateRegisteredEvent,
    StateUnregisteredEvent,
    StateSnapshotCreatedEvent,
    StateSnapshotRestoredEvent,
    StateManagerStartedEvent,
    StateManagerStoppedEvent,
)

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

__all__ = [
    # Core models
    "SystemState",
    "AppState",

    # Context
    "StateContext",
    "get_current_app_state",
    "set_current_app_state",
    "reset_current_app_state",
    "get_current_system_state",
    "set_current_system_state",
    "reset_current_system_state",
    "use_app_state",
    "use_system_state",

    # Transitions
    "TransitionRegistry",
    "default_transition_registry",

    # Validation
    "StateValidator",
    "default_state_validator",

    # History
    "StateHistory",

    # Snapshots
    "StateSnapshot",

    # Registry
    "StateRegistry",
    "default_state_registry",

    # State machine
    "StateMachine",

    # Persistence
    "StatePersistence",

    # Events
    "StateEvent",
    "StateTransitionEvent",
    "StateRegisteredEvent",
    "StateUnregisteredEvent",
    "StateSnapshotCreatedEvent",
    "StateSnapshotRestoredEvent",
    "StateManagerStartedEvent",
    "StateManagerStoppedEvent",
]

__version__ = "1.0.0"