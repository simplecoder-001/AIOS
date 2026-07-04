# app/core/state_manager/state_persistence.py
"""
State persistence layer for the AIOS State Manager.

Responsible for durable storage and recovery of application state snapshots.

Features
--------
- JSON snapshot serialization
- Atomic writes
- Automatic snapshot loading
- Snapshot metadata support
- Thread-safe file operations
- Storage backend abstraction point

This module intentionally depends only on StateSnapshot/AppState and does
not publish events.

Future extensions:
    - SQLite backend
    - Encrypted snapshots (FG6)
    - Snapshot compression
    - Versioned migrations
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from app.core.exceptions import (
    StatePersistenceError,
)
from app.core.state_manager.app_state import AppState
from app.core.state_manager.state_snapshot import (
    StateSnapshot,
)

__all__ = [
    "StatePersistence",
]


class StatePersistence:
    """
    Snapshot persistence service.

    Parameters
    ----------
    directory:
        Directory where snapshots are stored.
    filename:
        Snapshot filename.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        filename: str = "state_snapshot.json",
    ) -> None:
        self._directory = Path(
            directory
        ).expanduser()

        self._filename = filename
        self._path = (
            self._directory
            / filename
        )

        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.exists()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(
        self,
        snapshot: StateSnapshot,
    ) -> Path:
        """
        Persist a snapshot atomically.

        Returns
        -------
        Path
            Persisted snapshot path.

        Raises
        ------
        StatePersistenceError
        """
        if not isinstance(
            snapshot,
            StateSnapshot,
        ):
            raise TypeError(
                "snapshot must be a "
                "StateSnapshot instance"
            )

        try:
            with self._lock:
                self._directory.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                temp_path = (
                    self._path.with_suffix(
                        ".tmp"
                    )
                )

                payload = (
                    snapshot.to_dict()
                )

                with temp_path.open(
                    "w",
                    encoding="utf-8",
                ) as fp:
                    json.dump(
                        payload,
                        fp,
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )

                temp_path.replace(
                    self._path
                )

                return self._path

        except Exception as exc:
            raise StatePersistenceError(
                operation="save",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(
        self,
        *,
        state: AppState,
    ) -> StateSnapshot:
        """
        Load persisted snapshot metadata.

        Notes
        -----
        Rebuilding AppState from disk is delegated
        to StateRegistry/RecoveryManager because
        reconstruction may require validators and
        subsystem registries.

        Raises
        ------
        StatePersistenceError
        """
        try:
            with self._lock:
                if not self._path.exists():
                    raise FileNotFoundError(
                        str(
                            self._path
                        )
                    )

                with self._path.open(
                    "r",
                    encoding="utf-8",
                ) as fp:
                    payload = json.load(
                        fp
                    )

                return (
                    StateSnapshot.from_dict(
                        payload,
                        state=state,
                    )
                )

        except Exception as exc:
            raise StatePersistenceError(
                operation="load",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def load_payload(
        self,
    ) -> Mapping[str, Any]:
        """
        Load raw snapshot payload.

        Useful for migration and diagnostics.
        """
        try:
            with self._lock:
                if not self._path.exists():
                    return {}

                with self._path.open(
                    "r",
                    encoding="utf-8",
                ) as fp:
                    data = json.load(
                        fp
                    )

                return data

        except Exception as exc:
            raise StatePersistenceError(
                operation="load_payload",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Management
    # ------------------------------------------------------------------

    def delete(self) -> None:
        """
        Remove persisted snapshot.
        """
        try:
            with self._lock:
                self._path.unlink(
                    missing_ok=True
                )

        except Exception as exc:
            raise StatePersistenceError(
                operation="delete",
                cause=exc,
            ) from exc

    def clear(self) -> None:
        """
        Alias for delete().
        """
        self.delete()

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def create_snapshot(
        self,
        state: AppState,
        *,
        metadata: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> StateSnapshot:
        """
        Build a snapshot from an AppState.
        """
        return (
            StateSnapshot.from_app_state(
                state,
                metadata=metadata,
            )
        )

    def save_state(
        self,
        state: AppState,
        *,
        metadata: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> Path:
        """
        Convenience method.

        Create and save snapshot in one call.
        """
        snapshot = (
            self.create_snapshot(
                state,
                metadata=metadata,
            )
        )

        return self.save(
            snapshot
        )

    def __repr__(
        self,
    ) -> str:
        return (
            "StatePersistence("
            f"path='{self._path}', "
            f"exists={self.exists}"
            ")"
        )