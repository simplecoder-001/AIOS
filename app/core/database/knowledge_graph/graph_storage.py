# app/core/database/knowledge_graph/graph_storage.py
"""
Persistent storage layer for the AIOS knowledge graph.
=====================================================
A networkx in-memory graph is fast, but a knowledge graph is permanence
sensitive — entity relationships learned over hours (FG2 memory engine)
down to a JSON document on disk (``data/graphs/knowledge_graph/``) and
loads it back into a networkx ``MultiDiGraph`` on startup.

Design choices
--------------
* **JSON document, not a graph database.** AIOS runs locally on a single
  Windows host, so a self-contained file is preferable to an external
  service. The format uses ``networkx.readwrite.json_graph.node_link_data``
  which is stable, human-readable, and diff-friendly.
* **Atomic writes.** Every save is staged to ``<name>.tmp`` then
  os-replaced over the live file so a crash mid-write can never corrupt
  the only copy.
* **Versioned envelope.** The file begins with a small header carrying the
  schema version, source code version and node/edge counts. This makes
  future migrations safe: a future ``migrate_v1_v2`` can consult the
  version before deserializing.
* **Throttled save.** Persists are debounced through a configurable
  ``dirty_interval`` so a high-frequency writer (the FG2 reasoning engine
  when bulk-learning) does not thrash the disk.
* **Backups.** Every successful save rotates the previous file into
  ``<name>.bak`` — one-step rollback for free.

Dependency order
----------------
constants → exceptions → … → database/relationships → here. This module
imports networkx, stdlib json/pathlib, the constants paths/limits, and
``KnowledgeGraphError`` only. It does NOT import relationships' manager;
the storage treats edges as opaque dicts so callers (graph_manager) move
between typed and untyped representations.

Concurrency
-----------
Thread-safe via a single RLock coordinating the live graph, the dirty
flag, and the persistence timer. ``load`` and ``save`` can run from
different threads; the lock ensures consistency between flag changes
and disk commits.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import networkx as nx
from networkx.readwrite import json_graph

from app.core.constants.limits import MAX_KNOWLEDGE_GRAPH_NODES
from app.core.constants.paths import KNOWLEDGE_GRAPH_DIR
from app.core.exceptions.database import KnowledgeGraphError

__all__ = [
    "GraphStorageConfig",
    "GraphStorageStats",
    "GraphStorage",
]


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

_SCHEMA_VERSION: int = 1
_FORMAT: str = "node_link_data"
_MAGIC: str = "AIOS-KG"

_DEFAULT_FILE_NAME: str = "knowledge_graph.json"


# ---------------------------------------------------------------------------
# Config + stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GraphStorageConfig:
    """Tunables for :class:`GraphStorage`."""

    directory: Path = field(default_factory=lambda: KNOWLEDGE_GRAPH_DIR)
    file_name: str = _DEFAULT_FILE_NAME
    schema_version: int = _SCHEMA_VERSION
    max_nodes: int = MAX_KNOWLEDGE_GRAPH_NODES
    dirty_interval_ms: int = 500          # Minimum spacing between saves.
    keep_backup: bool = True
    pretty: bool = False                  # Pretty-printed JSON for ops review.

    @property
    def path(self) -> Path:
        return self.directory / self.file_name

    @property
    def backup_path(self) -> Path:
        return self.directory / f"{self.file_name}.bak"

    @property
    def temp_path(self) -> Path:
        return self.directory / f"{self.file_name}.tmp"


@dataclass(slots=True)
class GraphStorageStats:
    """Counters observed across the storage's lifetime."""

    loads: int = 0
    saves: int = 0
    save_failures: int = 0
    backups_kept: int = 0
    last_save_at: float = 0.0
    last_save_nodes: int = 0
    last_save_edges: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "loads": self.loads,
            "saves": self.saves,
            "save_failures": self.save_failures,
            "backups_kept": self.backups_kept,
            "last_save_at": self.last_save_at,
            "last_save_nodes": self.last_save_nodes,
            "last_save_edges": self.last_save_edges,
        }


# ---------------------------------------------------------------------------
# GraphStorage
# ---------------------------------------------------------------------------


class GraphStorage:
    """Persistent on-disk store for a knowledge graph.

    The storage is bound to a single :class:`GraphStorageConfig`. Callers
    feed it a live networkx graph via :meth:`attach` (the storage does not
    own an in-memory graph itself — it is a saver/loader only). The
    :class:`GraphManager` keeps the live graph and uses ``GraphStorage`` to
    persist it on a debounce timer, on shutdown, and on demand via
    :meth:`flush`.
    """

    __slots__ = (
        "_config",
        "_lock",
        "_graph",
        "_dirty",
        "_pending_save_timer",
        "_stats",
        "_on_save",
        "_on_load",
        "_closed",
    )

    def __init__(self, config: Optional[GraphStorageConfig] = None) -> None:
        self._config = config or GraphStorageConfig()
        self._lock = threading.RLock()
        self._graph: Optional[nx.MultiDiGraph] = None
        self._dirty: bool = False
        self._pending_save_timer: Optional[threading.Timer] = None
        self._stats = GraphStorageStats()
        self._on_save: List[Callable[[GraphStorageStats], None]] = []
        self._on_load: List[Callable[[nx.MultiDiGraph], None]] = []
        self._closed = False

        self._ensure_directory()

    # ----------------------------------------------------- properties
    @property
    def config(self) -> GraphStorageConfig:
        return self._config

    @property
    def stats(self) -> GraphStorageStats:
        with self._lock:
            return _clone_stats(self._stats)

    @property
    def is_dirty(self) -> bool:
        with self._lock:
            return self._dirty

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def path(self) -> Path:
        return self._config.path

    @property
    def backup_path(self) -> Path:
        return self._config.backup_path

    @property
    def attached(self) -> bool:
        with self._lock:
            return self._graph is not None

    # ----------------------------------------------------- listeners
    def on_save(self, callback: Callable[[GraphStorageStats], None]) -> None:
        """Register a callback invoked after every successful save."""
        with self._lock:
            self._on_save.append(callback)

    def on_load(self, callback: Callable[[nx.MultiDiGraph], None]) -> None:
        """Register a callback invoked after every successful load."""
        with self._lock:
            self._on_load.append(callback)

    # ----------------------------------------------------- lifecycle
    def attach(self, graph: nx.MultiDiGraph) -> None:
        """Bind a live graph to the storage so ``mark_dirty`` can be used."""
        if not isinstance(graph, nx.MultiDiGraph):
            raise KnowledgeGraphError(
                "GraphStorage requires a networkx.MultiDiGraph instance"
            )
        with self._lock:
            if self._closed:
                raise KnowledgeGraphError("GraphStorage has been closed")
            self._graph = graph

    def close(self) -> None:
        """Flush pending changes and stop the storage."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.flush()
        with self._lock:
            self._cancel_pending_save()
            self._graph = None

    # ----------------------------------------------------- directory
    def _ensure_directory(self) -> None:
        directory = self._config.directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise KnowledgeGraphError(
                f"Failed to create knowledge-graph directory: {directory}",
                cause=exc,
            ) from exc

    # ----------------------------------------------------- load
    def load(self, *, into: Optional[nx.MultiDiGraph] = None) -> nx.MultiDiGraph:
        """Load the persisted graph from disk into ``into``.

        If ``into`` is None, a fresh ``MultiDiGraph`` is created. When the
        on-disk file does not exist, an empty graph is returned — the first
        load on a fresh install is not an error.
        """
        with self._lock:
            if self._closed:
                raise KnowledgeGraphError("GraphStorage has been closed")
            path = self._config.path
            graph = into if into is not None else nx.MultiDiGraph()
            if not path.exists():
                self._graph = graph
                self._dirty = False
                self._stats.loads += 1
                self._fire_load(graph)
                return graph

        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except json.JSONDecodeError as exc:
            # Try the backup before giving up — keeps a single corrupt
            # write from permanently blocking boot.
            backup = self._config.backup_path
            if backup.exists():
                try:
                    with backup.open("r", encoding="utf-8") as handle:
                        envelope = json.load(handle)
                except json.JSONDecodeError as exc2:
                    raise KnowledgeGraphError(
                        "Corrupt graph file and backup; manual recovery required",
                        cause=exc2,
                    ) from exc2
            else:
                raise KnowledgeGraphError(
                    f"Knowledge graph file is corrupt and no backup exists: {path}",
                    cause=exc,
                ) from exc

        graph = self._envelope_to_graph(envelope, graph)
        with self._lock:
            self._graph = graph
            self._dirty = False
            self._stats.loads += 1
            self._fire_load(graph)
        return graph

    def _envelope_to_graph(
        self,
        envelope: Mapping[str, Any],
        into: nx.MultiDiGraph,
    ) -> nx.MultiDiGraph:
        magic = envelope.get("magic")
        if magic != _MAGIC:
            raise KnowledgeGraphError(
                f"Knowledge graph file has invalid magic header: {magic!r}"
            )
        schema_version = int(envelope.get("schema", _SCHEMA_VERSION))
        if schema_version > _SCHEMA_VERSION:
            raise KnowledgeGraphError(
                f"Knowledge graph schema v{schema_version} is newer than supported "
                f"v{_SCHEMA_VERSION}; please upgrade AIOS."
            )
        # Future: migrate_v1_v2(envelope) when schema > 1.
        data = envelope.get("graph")
        if data is None:
            raise KnowledgeGraphError("Missing graph payload in knowledge-graph file")
        try:
            loaded = json_graph.node_link_graph(data, directed=True, multigraph=True)
        except Exception as exc:
            raise KnowledgeGraphError(
                "Failed to deserialize knowledge-graph payload",
                cause=exc,
            ) from exc
        if not isinstance(loaded, nx.MultiDiGraph):
            # node_link_graph honors directed=True + multigraph=True,
            # but be defensive in case upstream changes semantics.
            converted = nx.MultiDiGraph()
            converted.update(loaded)
            loaded = converted
        into.clear()
        into.update(loaded)
        return into

    # ----------------------------------------------------- save
    def mark_dirty(self) -> None:
        """Mark the attached graph as having unsaved changes.

        Schedules a debounced save if one is not already pending.
        """
        with self._lock:
            if self._closed:
                return
            self._dirty = True
            if self._pending_save_timer is not None:
                return
            interval = self._config.dirty_interval_ms / 1000.0
            timer = threading.Timer(interval, self._flush_locked)
            timer.daemon = True
            timer.name = "kg-storage-save"
            self._pending_save_timer = timer
            timer.start()

    def flush(self) -> None:
        """Persist any pending changes immediately and cancel the timer."""
        with self._lock:
            self._cancel_pending_save()
            if self._closed:
                return
            if not self._dirty or self._graph is None:
                return
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Save helper. Caller must hold ``self._lock`` or guarantee none."""
        graph = self._graph
        if graph is None:
            return
        try:
            self._write_atomic(graph)
            self._dirty = False
            with self._lock:
                self._stats.saves += 1
                self._stats.last_save_at = time.time()
                self._stats.last_save_nodes = graph.number_of_nodes()
                self._stats.last_save_edges = graph.number_of_edges()
                stats = _clone_stats(self._stats)
        except Exception as exc:
            with self._lock:
                self._stats.save_failures += 1
            raise KnowledgeGraphError(
                "Failed to persist knowledge graph",
                cause=exc,
            ) from exc

        # Rotate a backup of the prior version.
        if self._config.keep_backup:
            try:
                self._rotate_backup()
            except Exception:
                # Backups are best-effort; failure to rotate must not raise.
                pass
        self._fire_save(stats)

    def _write_atomic(self, graph: nx.MultiDiGraph) -> None:
        cfg = self._config
        if graph.number_of_nodes() > cfg.max_nodes:
            raise KnowledgeGraphError(
                f"Knowledge graph exceeds max_nodes limit "
                f"({graph.number_of_nodes()} > {cfg.max_nodes})",
            ).with_context(nodes=graph.number_of_nodes(), max=cfg.max_nodes)
        try:
            data = json_graph.node_link_data(graph)
        except Exception as exc:
            raise KnowledgeGraphError(
                "Failed to serialize knowledge graph via node_link_data",
                cause=exc,
            ) from exc
        envelope = {
            "magic": _MAGIC,
            "schema": cfg.schema_version,
            "format": _FORMAT,
            "saved_at": time.time(),
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "graph": data,
        }
        indent = 2 if cfg.pretty else None
        payload = json.dumps(envelope, indent=indent, ensure_ascii=False, sort_keys=True)
        cfg.directory.mkdir(parents=True, exist_ok=True)
        temp_path = cfg.temp_path
        # Write to a temporary file in the same directory, then atomic-replace.
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp_path), str(cfg.path))
        except OSError as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise KnowledgeGraphError(
                "Knowledge graph atomic write failed",
                cause=exc,
            ) from exc

    def _rotate_backup(self) -> None:
        cfg = self._config
        path = cfg.path
        backup = cfg.backup_path
        if not path.exists():
            return
        try:
            if backup.exists():
                backup.unlink()
            shutil.copy2(str(path), str(backup))
            with self._lock:
                self._stats.backups_kept += 1
        except OSError:
            # Best-effort: a failed backup rotation must never fail the save.
            pass

    # ----------------------------------------------------- reset / delete
    def reset(self) -> None:
        """Delete the persisted graph and its backup."""
        with self._lock:
            self._cancel_pending_save()
            self._dirty = False
            if self._graph is not None:
                self._graph.clear()
        for path in (self._config.path, self._config.backup_path, self._config.temp_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def restore_from_backup(self) -> nx.MultiDiGraph:
        """Restore the live graph from the last backup file."""
        backup = self._config.backup_path
        if not backup.exists():
            raise KnowledgeGraphError(
                f"No knowledge-graph backup to restore from: {backup}"
            )
        # Swap paths: promote the backup to the live file, then load.
        live = self._config.path
        try:
            shutil.copy2(str(backup), str(live))
        except OSError as exc:
            raise KnowledgeGraphError(
                "Failed to restore knowledge-graph backup",
                cause=exc,
            ) from exc
        return self.load()

    # ----------------------------------------------------- introspection
    def describe(self) -> Dict[str, Any]:
        with self._lock:
            graph = self._graph
            return {
                "path": str(self._config.path),
                "schema_version": self._config.schema_version,
                "attached": graph is not None,
                "nodes": graph.number_of_nodes() if graph is not None else 0,
                "edges": graph.number_of_edges() if graph is not None else 0,
                "dirty": self._dirty,
                "closed": self._closed,
                "stats": self._stats.as_dict(),
            }

    # ----------------------------------------------------- internals
    def _cancel_pending_save(self) -> None:
        if self._pending_save_timer is not None:
            self._pending_save_timer.cancel()
            self._pending_save_timer = None

    def _fire_save(self, stats: GraphStorageStats) -> None:
        for callback in list(self._on_save):
            try:
                callback(stats)
            except Exception:
                # Listener failures must never break the save path.
                pass

    def _fire_load(self, graph: nx.MultiDiGraph) -> None:
        for callback in list(self._on_load):
            try:
                callback(graph)
            except Exception:
                pass

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        with self._lock:
            return (
                f"<GraphStorage path={self._config.path} "
                f"attached={self._graph is not None} "
                f"dirty={self._dirty} closed={self._closed}>"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clone_stats(src: GraphStorageStats) -> GraphStorageStats:
    """Copy stats so callers cannot mutate the storage's internal counters."""
    clone = GraphStorageStats(
        loads=src.loads,
        saves=src.saves,
        save_failures=src.save_failures,
        backups_kept=src.backups_kept,
        last_save_at=src.last_save_at,
        last_save_nodes=src.last_save_nodes,
        last_save_edges=src.last_save_edges,
    )
    return clone


def default_storage_config() -> GraphStorageConfig:
    """Return a sane default storage config pointing at the project path."""
    return GraphStorageConfig()


def from_directory(directory: Path, *, file_name: str = _DEFAULT_FILE_NAME) -> GraphStorage:
    """Convenience factory: build a storage pointing at a custom directory."""
    return GraphStorage(GraphStorageConfig(directory=directory, file_name=file_name))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ += [
    "GraphStorageConfig",
    "GraphStorageStats",
    "GraphStorage",
    "default_storage_config",
    "from_directory",
]
