# app/core/state_manager/state_history.py
"""
State history tracking for the AIOS State Manager.

Maintains an in-memory, thread-safe history of state transitions for every
registered subsystem. The history is used by:

- Recovery Manager
- StateSnapshot
- StatePersistence
- StateMachine rollback support
- Event replay and diagnostics
- Audit logging

This module intentionally stores immutable SystemState instances, making
history entries safe to share across threads.

Import-safe:
    - Standard library
    - app.core.state_manager.system_state
"""

from __future__ import annotations

import threading
from collections import deque
from types import MappingProxyType
from typing import Deque, Dict, Iterator, Mapping, Optional

from app.core.state_manager.system_state import SystemState

__all__ = [
    "StateHistory",
]


class StateHistory:
    """
    Thread-safe history store for subsystem states.

    Each subsystem maintains its own bounded history queue.

    Example
    -------
        history.record(voice_state)

        previous = history.last("voice")
        states = history.get("voice")
    """

    def __init__(
        self,
        *,
        max_history: int = 1000,
    ) -> None:
        if max_history <= 0:
            raise ValueError(
                "max_history must be greater than zero"
            )

        self._max_history = max_history
        self._history: Dict[
            str,
            Deque[SystemState],
        ] = {}

        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        state: SystemState,
    ) -> None:
        """
        Record a new state snapshot.
        """
        if not isinstance(state, SystemState):
            raise TypeError(
                "state must be a SystemState"
            )

        with self._lock:
            history = self._history.setdefault(
                state.name,
                deque(maxlen=self._max_history),
            )
            history.append(state)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get(
        self,
        name: str,
    ) -> tuple[SystemState, ...]:
        """
        Return full history for a subsystem.
        """
        with self._lock:
            history = self._history.get(name)

            if history is None:
                return ()

            return tuple(history)

    def last(
        self,
        name: str,
    ) -> Optional[SystemState]:
        """
        Return the most recent state.
        """
        with self._lock:
            history = self._history.get(name)

            if not history:
                return None

            return history[-1]

    def previous(
        self,
        name: str,
    ) -> Optional[SystemState]:
        """
        Return the state immediately before the latest.
        """
        with self._lock:
            history = self._history.get(name)

            if history is None:
                return None

            if len(history) < 2:
                return None

            return history[-2]

    def latest_states(
        self,
    ) -> Mapping[str, SystemState]:
        """
        Return latest state for every subsystem.
        """
        with self._lock:
            snapshot = {
                name: history[-1]
                for name, history in self._history.items()
                if history
            }

        return MappingProxyType(snapshot)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def contains(
        self,
        name: str,
    ) -> bool:
        with self._lock:
            return (
                name in self._history
                and bool(self._history[name])
            )

    def count(
        self,
        name: str,
    ) -> int:
        with self._lock:
            history = self._history.get(name)

            if history is None:
                return 0

            return len(history)

    def total_count(self) -> int:
        """
        Total number of stored snapshots.
        """
        with self._lock:
            return sum(
                len(history)
                for history in self._history.values()
            )

    # ------------------------------------------------------------------
    # Removal
    # ------------------------------------------------------------------

    def clear(
        self,
        name: str,
    ) -> None:
        """
        Remove history of one subsystem.
        """
        with self._lock:
            self._history.pop(name, None)

    def clear_all(self) -> None:
        """
        Remove all histories.
        """
        with self._lock:
            self._history.clear()

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._history.keys())

    def items(
        self,
    ) -> Mapping[
        str,
        tuple[SystemState, ...],
    ]:
        with self._lock:
            snapshot = {
                name: tuple(history)
                for name, history in self._history.items()
            }

        return MappingProxyType(snapshot)

    def __contains__(
        self,
        name: str,
    ) -> bool:
        return self.contains(name)

    def __len__(self) -> int:
        return self.total_count()

    def __iter__(
        self,
    ) -> Iterator[str]:
        return iter(self.names())

    @property
    def max_history(self) -> int:
        return self._max_history

    def __repr__(self) -> str:
        return (
            "StateHistory("
            f"systems={len(self._history)}, "
            f"entries={self.total_count()}, "
            f"max_history={self._max_history}"
            ")"
        )