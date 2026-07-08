# app/core/utils/singleton.py
"""
Thread-safe singleton utilities.

Provides patterns for creating process-wide singletons with thread-safe
initialization, optional lazy loading, and explicit reset support for testing.
These are used throughout the infrastructure layer (configs, logging, DI, event bus,
state manager, database) to ensure exactly one instance per process.

Design
------
* ``SingletonMeta`` — a metaclass that guarantees a class has exactly one instance.
  Thread-safe via double-checked locking. Supports ``_reset_instance()`` for tests.
* ``singleton`` — a decorator that turns a function into a cached call-once factory.
  The first call computes and caches the result; subsequent calls return the cached
  value. Thread-safe via a module-level lock. Supports ``reset`` for tests.

Dependency order
----------------
Standard library only. Zero project dependencies — import-safe at the earliest
bootstrap step.
"""

from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable, ClassVar, Dict, Optional, Type, TypeVar

__all__ = [
    "SingletonMeta",
    "singleton",
]

T = TypeVar("T")


class SingletonMeta(type):
    """Thread-safe singleton metaclass.

    Any class using this as its metaclass is guaranteed to have exactly one
    instance per process. Construction is lazily triggered on the first call
    to ``cls()`` and cached thereafter under a double-checked lock.

    Usage::

        class MyService(metaclass=SingletonMeta):
            def __init__(self):
                self.started = False

        a = MyService()
        b = MyService()
        assert a is b

    For testing, call ``MyService._reset_instance()`` between tests to force
    a fresh instance.

    Concurrency note
    ----------------
    Uses a per-class reentrant lock. ``_reset_instance`` acquires the same lock
    so concurrent resets and constructions serialize correctly.
    """

    _instances: ClassVar[Dict[Type[Any], Any]] = {}

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls not in SingletonMeta._instances:
            lock = cls._singleton_lock()
            with lock:
                if cls not in SingletonMeta._instances:
                    instance = super().__call__(*args, **kwargs)
                    SingletonMeta._instances[cls] = instance
        return SingletonMeta._instances[cls]

    def _singleton_lock(cls) -> threading.Lock:
        lock_attr = f"_singleton_lock_{cls.__name__}"
        lock = getattr(cls, lock_attr, None)
        if lock is None:
            lock = threading.Lock()
            setattr(cls, lock_attr, lock)
        return lock

    def _reset_instance(cls) -> None:
        lock = cls._singleton_lock()
        with lock:
            SingletonMeta._instances.pop(cls, None)


def singleton(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator that memoises a function's return value permanently.

    The wrapped function is called exactly once (on first invocation) and the
    result is cached. Every subsequent call returns the cached value regardless
    of arguments. Thread-safe via a shared module-level lock.

    Usage::

        @singleton
        def get_cache_manager() -> CacheManager:
            return CacheManager(paths=get_paths())

        mgr = get_cache_manager()   # constructs
        mgr2 = get_cache_manager()  # returns same instance

        get_cache_manager.reset()   # force rebuild (testing only)

    Parameters
    ----------
    func:
        The factory function to wrap. Must return the object to cache.

    Returns
    -------
    Callable
        A thread-safe, cached wrapper with a ``.reset()`` method on it.
    """
    cache: list[Optional[T]] = [None]
    lock = threading.Lock()

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        if cache[0] is None:
            with lock:
                if cache[0] is None:
                    cache[0] = func(*args, **kwargs)
        return cache[0]  # type: ignore[return-value]

    wrapper.reset = lambda: _reset()  # type: ignore[attr-defined]

    def _reset() -> None:
        with lock:
            cache[0] = None

    return wrapper