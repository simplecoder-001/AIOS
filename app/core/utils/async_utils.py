# app/core/utils/async_utils.py
"""
Asyncio utilities.

Provides the canonical async patterns used by the Event Bus dispatcher
(``asyncio.gather``, ``run_in_executor``), the FG2 search manager (bounded
concurrent HTTP calls), the FG9 multi-agent pool (task limiting), and the
FG1 voice pipeline (streaming TTS playback with deadline control).

* :func:`run_coro` — safe ``asyncio.run`` wrapper (exceptions → RuntimeError_).
* :func:`safe_gather` — ``asyncio.gather`` with ``return_exceptions=True``.
* :func:`gather_with_limit` — bounded concurrency via Semaphore.
* :func:`run_blocking` — offload sync callable to the thread-pool executor.
* :func:`wait_for_deadline` — ``asyncio.wait_for`` with ms deadline.
* :class:`TaskPool` — context manager for a set of asyncio Tasks.

Dependency order
----------------
Standard library → ``app.core.exceptions.runtime`` → here.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Coroutine, List, Optional, Tuple, TypeVar

from app.core.exceptions.runtime import RuntimeError_, TimeoutError_

__all__ = [
    "run_coro",
    "safe_gather",
    "gather_with_limit",
    "run_blocking",
    "wait_for_deadline",
    "sleep_ms",
    "TaskPool",
]

T = TypeVar("T")


def run_coro(
    coro: Coroutine[Any, Any, T],
    *,
    loop_factory: Optional[Callable[[], asyncio.AbstractEventLoop]] = None,
) -> T:
    """Execute ``coro`` and block until complete.

    Creates a fresh event loop (``asyncio.run`` semantics) — suitable for
    one-shot entry points like tool invocations and startup sequences, but
    not for repeated calls in the same thread. Exceptions are wrapped as
    :class:`RuntimeError_`.
    """
    try:
        return asyncio.run(coro, loop_factory=loop_factory)
    except Exception as exc:
        raise RuntimeError_(
            f"Async operation failed: {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc


async def safe_gather(
    *coros: Coroutine[Any, Any, Any],
) -> Tuple[List[Any], List[Exception]]:
    """Run multiple coroutines concurrently; return ``(results, errors)``.

    Catches only ``Exception`` subclasses — ``KeyboardInterrupt`` and
    ``SystemExit`` propagate normally.
    """
    raw = await asyncio.gather(*coros, return_exceptions=True)
    results: List[Any] = []
    errors: List[Exception] = []
    for item in raw:
        if isinstance(item, Exception) and not isinstance(item, (KeyboardInterrupt, SystemExit)):
            errors.append(item)
        elif isinstance(item, BaseException):
            raise item
        else:
            results.append(item)
    return results, errors


async def gather_with_limit(
    limit: int,
    *coros: Coroutine[Any, Any, T],
) -> List[Optional[T]]:
    """Run ``coros`` concurrently with max ``limit`` active via Semaphore."""
    if limit <= 0:
        limit = 1
    semaphore = asyncio.Semaphore(limit)

    async def _worker(coro: Coroutine[Any, Any, T]) -> Optional[T]:
        async with semaphore:
            try:
                return await coro
            except Exception:
                return None

    tasks = [asyncio.create_task(_worker(c)) for c in coros]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    return [r if not isinstance(r, BaseException) else None for r in raw]


async def run_blocking(
    callable: Callable[..., T],
    *args: Any,
    executor: Optional[ThreadPoolExecutor] = None,
    **kwargs: Any,
) -> T:
    """Offload a synchronous callable to a thread-pool executor."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(executor, callable, *args, **kwargs)
    except Exception as exc:
        raise RuntimeError_(
            f"Blocking call failed in thread pool: {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc


async def wait_for_deadline(
    coro: Coroutine[Any, Any, T],
    *,
    timeout_ms: int,
    label: str = "async operation",
) -> T:
    """Await ``coro`` with a ms deadline. Raises project :class:`TimeoutError_`."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
    except asyncio.TimeoutError as exc:
        raise TimeoutError_(
            f"{label} timed out after {timeout_ms}ms",
            settings={"timeout_ms": timeout_ms},
        ) from exc
    except asyncio.CancelledError:
        raise


async def sleep_ms(milliseconds: int) -> None:
    """Asyncio sleep in ms."""
    await asyncio.sleep(milliseconds / 1000.0)


class TaskPool:
    """Context manager that spawns and manages asyncio Tasks.

    On exit, outstanding tasks are cancelled and drained.

    Usage::

        async with TaskPool() as pool:
            pool.create_task(run_agent("a"))
            pool.create_task(run_agent("b"))
            results, errors = await pool.gather()
    """

    def __init__(self) -> None:
        self._tasks: List[asyncio.Task[Any]] = []

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def gather(self) -> Tuple[List[Any], List[Exception]]:
        if not self._tasks:
            return [], []
        return await safe_gather(*self._tasks)

    async def drain(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def __aenter__(self) -> "TaskPool":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.drain()