# app/core/event_bus/events/__init__.py

"""
AIOS Event Definitions
======================

Central export point for all built-in Event Bus event definitions.

Importing from this package avoids deep import chains:

    from app.core.event_bus.events import (
        ApplicationStartedEvent,
        WakeWordDetectedEvent,
        PermissionDeniedEvent,
        AgentStartedEvent,
    )

Architecture
------------
Phase 0
├── System Events
├── Lifecycle Events
├── Security Events
├── Voice Events
├── GUI Events
├── Agent Events
└── Learning Events

This module intentionally contains no business logic and no runtime
initialization. It only re-exports event classes and enums to provide
a stable public API.
"""

from .system_events import *
from .lifecycle_events import *
from .security_events import *
from .voice_events import *
from .gui_events import *
from .agent_events import *
from .learning_events import *

# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

from .system_events import __all__ as _system_all
from .lifecycle_events import __all__ as _lifecycle_all
from .security_events import __all__ as _security_all
from .voice_events import __all__ as _voice_all
from .gui_events import __all__ as _gui_all
from .agent_events import __all__ as _agent_all
from .learning_events import __all__ as _learning_all

__all__ = (
    _system_all
    + _lifecycle_all
    + _security_all
    + _voice_all
    + _gui_all
    + _agent_all
    + _learning_all
)

del _system_all
del _lifecycle_all
del _security_all
del _voice_all
del _gui_all
del _agent_all
del _learning_all