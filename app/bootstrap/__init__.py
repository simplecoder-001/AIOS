# app/bootstrap/__init__.py

"""
AIOS Bootstrap Package
======================

The bootstrap package is the Phase 0 entry point of the AIOS runtime.
It is responsible for bringing the application's foundational
infrastructure online in a deterministic order before any feature
groups are initialized.

Bootstrap Order
---------------
1. Configuration subsystem
2. Logging infrastructure
3. Dependency Injection container
4. Event Bus
5. Application State Machine
6. Application startup coordination
7. Lifecycle management
8. Graceful shutdown management

Typical Usage
-------------
    from app.bootstrap import ApplicationStartup

    startup = ApplicationStartup()
    context = startup.start()

    container = context.container
    event_bus = context.event_bus
    state_machine = context.state_machine

    # Application is now ready for feature-group startup.

Or:

    from app.bootstrap import bootstrap_application

    context = bootstrap_application()

The bootstrap package intentionally performs only Phase 0 system
initialization. Feature groups (FG1–FG10) are initialized by their
own bootstrap packages and runtime managers.
"""

from __future__ import annotations

# ---------------------------------------------------------------------
# Bootstrap context and initializer
# ---------------------------------------------------------------------

from app.bootstrap.initializer import (
    BootstrapContext,
    BootstrapInitializer,
)

# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------

from app.bootstrap.startup import (
    ApplicationStartup,
)

# ---------------------------------------------------------------------
# Lifecycle management
# ---------------------------------------------------------------------

from app.bootstrap.lifecycle_manager import (
    LifecycleManager,
    LifecycleOperation,
)

# ---------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------

from app.bootstrap.shutdown import (
    ApplicationShutdown,
)

__all__ = [
    # initializer
    "BootstrapContext",
    "BootstrapInitializer",

    # startup
    "ApplicationStartup",

    # lifecycle
    "LifecycleManager",
    "LifecycleOperation",

    # shutdown
    "ApplicationShutdown",

    # helpers
    "bootstrap_application",
]

__version__ = "1.0.0"


# =====================================================================
# Convenience Helpers
# =====================================================================

def bootstrap_application() -> BootstrapContext:
    """
    Execute complete Phase 0 bootstrap and return the runtime context.

    Equivalent to:

        startup = ApplicationStartup()
        context = startup.start()

    Returns
    -------
    BootstrapContext
        Fully initialized runtime context.
    """
    startup = ApplicationStartup()
    return startup.start()