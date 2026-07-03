# app/dependency_injection/__init__.py
"""
AIOS Dependency Injection Package — public API.
===============================================
The backbone of Phase 0 wiring. Every subsystem is registered against a
container and resolved through a single, consistent, thread-safe entry point,
so feature groups depend on abstractions rather than constructing their own
collaborators.

Usage
-----
Import directly from the package root, not the submodules:

    from app.dependency_injection import (
        Container,
        ContainerBuilder,
        Lifetime,
        build_root_container,
    )

    # Bootstrap seeds the root container with logging + config.
    container = build_root_container(log_dir=Path("logs"))

    # Feature groups wire themselves via the fluent builder.
    container = (
        ContainerBuilder()
        .add_instance(Config, config)
        .add_singleton(EventBus)
        .build()
    )

    bus = container.resolve(EventBus)

    with container.scope("voice-session"):
        session = container.resolve(VoiceSession)  # SCOPED lifetime

Design
------
* ``interfaces`` defines the ``IProvider`` / ``IContainer`` / ``ILifecycle``
  contracts, keeping the container decoupled and testable.
* ``scopes`` owns lifetime semantics (SINGLETON / TRANSIENT / SCOPED) and the
  thread-safe instance stores.
* ``providers`` encapsulates *how* instances are produced; ``container``
  encapsulates *registration and resolution* with cycle detection.
* ``factories`` provides high-level composition helpers used by
  ``app/bootstrap``.

Failures raise the structured exceptions from ``app.core.exceptions``
(``DependencyNotFoundError``, ``CircularDependencyError``,
``DuplicateRegistrationError``, ``ProviderError``, ``ScopeError``,
``DependencyResolutionError``), so DI errors flow uniformly into the Event Bus,
audit logs, and the Recovery Manager.
"""

from __future__ import annotations

# --- Contracts ------------------------------------------------------------
from app.dependency_injection.interfaces import (
    IContainer,
    ILifecycle,
    IProvider,
)

# --- Lifetimes & scopes ---------------------------------------------------
from app.dependency_injection.scopes import (
    Lifetime,
    Scope,
    ScopeManager,
)

# --- Providers ------------------------------------------------------------
from app.dependency_injection.providers import (
    ClassProvider,
    FactoryProvider,
    InstanceProvider,
)

# --- Container ------------------------------------------------------------
from app.dependency_injection.container import Container

# --- Composition helpers --------------------------------------------------
from app.dependency_injection.factories import (
    ContainerBuilder,
    ServiceFactory,
    build_root_container,
)

__all__ = [
    # contracts
    "IContainer",
    "IProvider",
    "ILifecycle",
    # lifetimes & scopes
    "Lifetime",
    "Scope",
    "ScopeManager",
    # providers
    "InstanceProvider",
    "FactoryProvider",
    "ClassProvider",
    # container
    "Container",
    # composition helpers
    "ContainerBuilder",
    "ServiceFactory",
    "build_root_container",
]

__version__ = "1.0.0"
