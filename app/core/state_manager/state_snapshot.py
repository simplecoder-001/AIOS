# app/core/state_manager/state_snapshot.py
"""
Application state snapshots for the AIOS State Manager.

A StateSnapshot is an immutable, serializable point-in-time capture of the
entire application state. Snapshots are used for:

- Persistence
- Recovery and rollback
- Crash diagnostics
- State replay
- Audit logging
- Event sourcing

This module intentionally contains no EventBus, logging, or persistence
dependencies to keep it import-safe and reusable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from types import MappingProxyType

from app.core.state_manager.app_state import AppState

__all__ = [
    "StateSnapshot",
]


def _utcnow() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class StateSnapshot:
    """
    Immutable application state snapshot.

    Examples
    --------
        snapshot = StateSnapshot.from_app_state(app_state)

        data = snapshot.to_dict()
    """

    snapshot_id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )

    state: AppState = field(
        default_factory=AppState
    )

    created_at: datetime = field(
        default_factory=_utcnow
    )

    metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.state, AppState):
            raise TypeError(
                "state must be an AppState instance"
            )

        if not isinstance(
            self.metadata,
            Mapping,
        ):
            raise TypeError(
                "metadata must be a mapping"
            )

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_app_state(
        cls,
        state: AppState,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "StateSnapshot":
        """
        Create a snapshot from an AppState.
        """
        return cls(
            state=state,
            metadata=(
                MappingProxyType(
                    dict(metadata)
                )
                if metadata
                else MappingProxyType({})
            ),
        )

    # ------------------------------------------------------------------
    # Metadata operations
    # ------------------------------------------------------------------

    def with_metadata(
        self,
        **updates: Any,
    ) -> "StateSnapshot":
        """
        Return a new snapshot with merged metadata.
        """
        merged = dict(self.metadata)
        merged.update(updates)

        return StateSnapshot(
            snapshot_id=self.snapshot_id,
            state=self.state,
            created_at=self.created_at,
            metadata=MappingProxyType(
                merged
            ),
            version=self.version,
        )

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def subsystem_count(self) -> int:
        """
        Number of registered subsystems.
        """
        return self.state.subsystem_count

    @property
    def is_empty(self) -> bool:
        """
        Whether the snapshot contains no states.
        """
        return self.state.is_empty

    @property
    def age_seconds(self) -> float:
        """
        Age of the snapshot in seconds.
        """
        return (
            _utcnow() - self.created_at
        ).total_seconds()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Convert snapshot to a JSON-serializable dictionary.
        """
        return {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "created_at": (
                self.created_at.isoformat()
            ),
            "metadata": dict(
                self.metadata
            ),
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        state: AppState,
    ) -> "StateSnapshot":
        """
        Reconstruct a snapshot.

        AppState reconstruction is intentionally delegated
        to StatePersistence/StateRegistry because rebuilding
        subsystem states may require registries and validators.
        """
        return cls(
            snapshot_id=str(
                payload["snapshot_id"]
            ),
            state=state,
            created_at=datetime.fromisoformat(
                payload["created_at"]
            ),
            metadata=MappingProxyType(
                dict(
                    payload.get(
                        "metadata",
                        {},
                    )
                )
            ),
            version=int(
                payload.get(
                    "version",
                    1,
                )
            ),
        )

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return self.subsystem_count

    def __contains__(
        self,
        subsystem: str,
    ) -> bool:
        return subsystem in self.state

    def __repr__(self) -> str:
        return (
            "StateSnapshot("
            f"id={self.snapshot_id!r}, "
            f"systems={self.subsystem_count}, "
            f"version={self.version}"
            ")"
        )