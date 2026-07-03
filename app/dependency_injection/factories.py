# app/dependency_injection/factories.py
"""
Dependency Injection Factories
==============================
High-level composition helpers built on top of :class:`Container`.

Where the container is the low-level engine, this module provides the
ergonomics the bootstrap layer needs:

* ``ContainerBuilder`` : a fluent builder for declaratively wiring services
  before the container is "built" and handed to the application.
* ``build_root_container`` : constructs the process-wide root container and
  seeds it with the foundational services every feature group depends on
  (the ``LoggerFactory`` and the bootstrap ``Logger``).
* ``ServiceFactory`` : a thin, typed convenience wrapper for resolving and
  lazily creating named services from an existing container.

These helpers keep ``app/bootstrap`` free of repetitive registration
boilerplate and give a single, well-documented place for wiring policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Type, TypeVar

from app.core.constants.app import APP_SLUG
from app.core.exceptions import ProviderError
from app.dependency_injection.container import Container
from app.dependency_injection.scopes import Lifetime
from app.logging import Logger, LoggerFactory, LogLevel

__all__ = [
    "ContainerBuilder",
    "ServiceFactory",
    "build_root_container",
]

T = TypeVar("T")


class ContainerBuilder:
    """Fluent builder for declaratively wiring a :class:`Container`.

    Every ``add_*`` method returns ``self`` so registrations can be chained.
    Call :meth:`build` once to obtain the configured container.

        container = (
            ContainerBuilder()
            .add_instance(Config, config)
            .add_singleton(EventBus)
            .add_factory(Logger, make_logger, lifetime=Lifetime.SINGLETON)
            .build()
        )
    """

    def __init__(self, logger: Optional[Logger] = None) -> None:
        self._container = Container(logger=logger)
        self._built = False

    # ------------------------------------------------------------ additions
    def add_instance(self, token: Any, instance: T, *, replace: bool = False) -> "ContainerBuilder":
        """Register a pre-built instance."""
        self._ensure_open()
        self._container.register_instance(token, instance, replace=replace)
        return self

    def add_factory(
        self,
        token: Any,
        factory: Callable[..., T],
        *,
        lifetime: Lifetime = Lifetime.TRANSIENT,
        disposer: Optional[Callable[[T], None]] = None,
        replace: bool = False,
    ) -> "ContainerBuilder":
        """Register a factory callable with a configurable lifetime."""
        self._ensure_open()
        self._container.register_factory(
            token, factory, lifetime=lifetime, disposer=disposer, replace=replace
        )
        return self

    def add_singleton(
        self,
        token: Any,
        cls: Optional[Type[T]] = None,
        *,
        disposer: Optional[Callable[[T], None]] = None,
        replace: bool = False,
    ) -> "ContainerBuilder":
        """Register an auto-wired class as a SINGLETON."""
        self._ensure_open()
        self._container.register_class(
            token, cls, lifetime=Lifetime.SINGLETON, disposer=disposer, replace=replace
        )
        return self

    def add_transient(
        self,
        token: Any,
        cls: Optional[Type[T]] = None,
        *,
        replace: bool = False,
    ) -> "ContainerBuilder":
        """Register an auto-wired class as TRANSIENT (new per resolve)."""
        self._ensure_open()
        self._container.register_class(
            token, cls, lifetime=Lifetime.TRANSIENT, replace=replace
        )
        return self

    def add_scoped(
        self,
        token: Any,
        cls: Optional[Type[T]] = None,
        *,
        disposer: Optional[Callable[[T], None]] = None,
        replace: bool = False,
    ) -> "ContainerBuilder":
        """Register an auto-wired class as SCOPED (one per active scope)."""
        self._ensure_open()
        self._container.register_class(
            token, cls, lifetime=Lifetime.SCOPED, disposer=disposer, replace=replace
        )
        return self

    def configure(self, configurator: Callable[[Container], None]) -> "ContainerBuilder":
        """Apply an arbitrary configuration callback to the container.

        Enables feature groups to expose their own ``register(container)``
        modules that the builder invokes during composition.
        """
        self._ensure_open()
        configurator(self._container)
        return self

    # --------------------------------------------------------------- build
    def build(self) -> Container:
        """Finalize and return the configured container.

        The builder is single-use; calling :meth:`build` twice raises.
        """
        if self._built:
            raise ProviderError("ContainerBuilder", reason="builder already consumed")
        self._built = True
        return self._container

    def _ensure_open(self) -> None:
        if self._built:
            raise ProviderError(
                "ContainerBuilder", reason="cannot modify a container after build()"
            )


class ServiceFactory:
    """Typed convenience wrapper over an existing container.

    Provides ``get`` / ``get_or_create`` helpers for call sites that prefer a
    small, explicit surface instead of the full container API.
    """

    def __init__(self, container: Container) -> None:
        self._container = container

    def get(self, token: Type[T]) -> T:
        """Resolve a required service (raises if unregistered)."""
        return self._container.resolve(token)

    def get_optional(self, token: Type[T], default: Optional[T] = None) -> Optional[T]:
        """Resolve a service or return ``default`` when it is absent."""
        return self._container.try_resolve(token, default)

    def get_or_create(
        self,
        token: Any,
        factory: Callable[[], T],
        *,
        lifetime: Lifetime = Lifetime.SINGLETON,
    ) -> T:
        """Return the registered service, registering ``factory`` on first use."""
        if not self._container.has(token):
            self._container.register_factory(token, factory, lifetime=lifetime)
        return self._container.resolve(token)

    @property
    def container(self) -> Container:
        return self._container


def build_root_container(
    *,
    log_dir: Optional[Path] = None,
    console_level: LogLevel = LogLevel.INFO,
    logger_factory: Optional[LoggerFactory] = None,
) -> Container:
    """Construct and seed the process-wide root container.

    This is the single entry point the bootstrap sequencer calls before any
    feature group is initialized. It guarantees the two foundational services
    every subsystem relies on are present:

    * :class:`LoggerFactory` : registered as an eager singleton instance.
    * bootstrap :class:`Logger` : a composite (console + rotating file) logger
      registered as a singleton, used by the container itself.

    Parameters
    ----------
    log_dir:
        Directory for the bootstrap log file. When omitted, a console-only
        bootstrap logger is created so the container is still fully usable in
        tests and headless environments.
    console_level:
        Minimum level for the bootstrap console output.
    logger_factory:
        An existing factory to reuse; a fresh one is created when omitted.
    """
    factory = logger_factory or LoggerFactory()

    if log_dir is not None:
        log_path = str(Path(log_dir) / "startup" / "bootstrap.log")
        boot_logger = factory.create_composite_logger(
            name=f"{APP_SLUG}.bootstrap",
            file_path=log_path,
            level=console_level,
        )
    else:
        boot_logger = factory.create_console_logger(
            name=f"{APP_SLUG}.bootstrap",
            level=console_level,
        )

    container = Container(logger=boot_logger)
    # Seed foundational services. The LoggerFactory is shared so every feature
    # group creates its loggers through the same cached registry.
    container.register_instance(LoggerFactory, factory)
    container.register_instance(Logger, boot_logger)

    boot_logger.info("Root DI container initialized")
    return container
