# app/core/database/knowledge_graph/graph_manager.py
"""
Top-level coordinator for the AIOS knowledge graph.
====================================================
:class:`GraphManager` is the *only* public surface feature groups import
from ``app.core.database.knowledge_graph``. It composes the three
subordinate collaborators:

* :class:`RelationshipManager` — typed edge catalog + builder.
* :class:`GraphStorage` — atomic on-disk JSON persistence (snapshots).
* :class:`QueryEngine` — read-side traversal + centrality + pathing.

…and binds them to the AIOS process-wide infrastructure (EventBus,
DI container, logger factory, ConfigManager hooks). The seeds of the
graph are:

* Started once during Phase 0 bootstrap (after the DatabaseManager) via
  :meth:`start`.
* Loaded from disk by :meth:`load_or_init`.
* Mutated through the typed facade (``add_node``, ``add_relationship``,
  ``add_relationships``, ``remove_node``, ``remove_edge``).
* Queried read-only via :meth:`query` (the QueryEngine surface).
* Persisted through the storage debounce timer; flushed synchronously by
  :meth:`flush`; restored from backup by :meth:`restore_from_backup`.
* Stopped idempotently via :meth:`stop` (final flush + release).

Event Bus bridge
----------------
Mutations publish ``knowledge.graph.node_added`` /
``knowledge.graph.relationship_added`` / ``knowledge.graph.removed`` /
``knowledge.graph.flushed`` events through the source-bound publisher so
subscribers (FG2 memory engine, FG5 dashboard, FG10 self-learning) can
react without polling the storage file. The manager never *imports* the
EventBus: it accepts an optional bus in its constructor so test
environments can supply a fake.

Dependency graph
----------------
constants → exceptions → … → database/relationships → graph_storage →
queries → here. This module is the *only* knowledge-graph module that
imports the event bus, state machine, and DI container — keeping the
others import-safe from any layer beneath them.

Concurrency
-----------
Every mutating operation takes the graph's coarse RLock. The lock is
re-entrant so callers may compose graph mutations inside transactional
blocks (see :meth:`transaction`). Reads dispatch to the QueryEngine
which only consults the live graph; for a stable snapshot across a long
query, callers use :meth:`snapshot` to copy the live graph.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

import networkx as nx

from app.core.configs.config_manager import ConfigManager
from app.core.constants.events import (
    EventCategory,
    EventDeliveryMode,
    EventPriority,
)
from app.core.database.knowledge_graph.graph_storage import (
    GraphStorage,
    GraphStorageConfig,
    GraphStorageStats,
    default_storage_config,
)
from app.core.database.knowledge_graph.queries import (
    QueryEngine,
    QueryOptions,
    QueryResult,
)
from app.core.database.knowledge_graph.relationships import (
    Relationship,
    RelationshipDescriptor,
    RelationshipManager,
    RelationshipType,
)
from app.core.event_bus import EventBus
from app.core.exceptions.database import KnowledgeGraphError
from app.dependency_injection.container import Container
from app.logging import Logger, LoggerFactory, LogLevel

__all__ = [
    "GraphState",
    "GraphStats",
    "GraphConfig",
    "GraphManager",
    "register_knowledge_graph",
]


# ---------------------------------------------------------------------------
# Lifecycle states
# ---------------------------------------------------------------------------


class GraphState(str, Enum):
    """Knowledge-graph lifecycle states observed by the GraphManager."""

    UNINITIALIZED = "uninitialized"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Event names (not in the global constants catalog — graph-local only)
# ---------------------------------------------------------------------------

_EVT_NODE_ADDED = "knowledge.graph.node_added"
_EVT_NODE_REMOVED = "knowledge.graph.node_removed"
_EVT_RELATION_ADDED = "knowledge.graph.relationship_added"
_EVT_RELATION_REMOVED = "knowledge.graph.relationship_removed"
_EVT_FLUSHED = "knowledge.graph.flushed"
_EVT_LOADED = "knowledge.graph.loaded"
_EVT_FAILED = "knowledge.graph.failed"

_EVT_ALL: Tuple[str, ...] = (
    _EVT_NODE_ADDED,
    _EVT_NODE_REMOVED,
    _EVT_RELATION_ADDED,
    _EVT_RELATION_REMOVED,
    _EVT_FLUSHED,
    _EVT_LOADED,
    _EVT_FAILED,
)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GraphStats:
    """Roll-up of knowledge-graph counters for observability."""

    nodes_added: int = 0
    nodes_removed: int = 0
    relationships_added: int = 0
    relationships_removed: int = 0
    flushes: int = 0
    flush_failures: int = 0
    loads: int = 0
    load_failures: int = 0
    transactions: int = 0
    transaction_failures: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "nodes_added": self.nodes_added,
            "nodes_removed": self.nodes_removed,
            "relationships_added": self.relationships_added,
            "relationships_removed": self.relationships_removed,
            "flushes": self.flushes,
            "flush_failures": self.flush_failures,
            "loads": self.loads,
            "load_failures": self.load_failures,
            "transactions": self.transactions,
            "transaction_failures": self.transaction_failures,
        }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphConfig:
    """Top-level knobs consumed during GraphManager construction."""

    storage: GraphStorageConfig = field(default_factory=default_storage_config)
    strict_relationships: bool = True
    publish_events: bool = True
    flush_on_shutdown: bool = True


# ---------------------------------------------------------------------------
# GraphManager
# ---------------------------------------------------------------------------


class GraphManager:
    """The facade that owns the live networkx graph and its collaborators.

    The manager is constructed with a :class:`GraphConfig` and an optional
    DI :class:`Container`. :meth:`start` brings it online (load or
    initialize, register with DI), :meth:`stop` flushes and tears down.
    Both are idempotent and thread-safe — the bootstrap only knows a
    one-line "build then start".
    """

    __slots__ = (
        "_config",
        "_logger_factory",
        "_logger",
        "_event_bus",
        "_publisher",
        "_container",
        "_lock",
        "_state",
        "_stats",
        "_graph",
        "_relationships",
        "_storage",
        "_queries",
        "_event_unsubscribers",
        "_closed",
        "_started",
        "_in_transaction",
    )

    def __init__(
        self,
        config: Optional[GraphConfig] = None,
        *,
        logger_factory: Optional[LoggerFactory] = None,
        event_bus: Optional[EventBus] = None,
        container: Optional[Container] = None,
    ) -> None:
        self._config = config or GraphConfig()
        self._logger_factory = logger_factory or LoggerFactory()
        self._logger = self._logger_factory.create_rotating_logger(
            name="app.core.database.knowledge_graph",
            file_path="logs/system/knowledge_graph.log",
            level=LogLevel.INFO,
        )
        self._event_bus = event_bus
        self._publisher = None  # Late-bound on start().
        self._container = container

        self._lock = threading.RLock()
        self._state = GraphState.UNINITIALIZED
        self._stats = GraphStats()

        # Collaborators.
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph(name="aios_knowledge_graph")
        self._relationships = RelationshipManager()
        self._storage = GraphStorage(self._config.storage)
        self._queries = QueryEngine()

        self._event_unsubscribers: List[Callable[[], None]] = []
        self._closed = False
        self._started = False
        self._in_transaction = 0  # Nesting tracker — events are batched at depth 0.

        # Storage callbacks.
        self._storage.attach(self._graph)
        self._storage.on_save(self._on_storage_saved)

    # ----------------------------------------------------- properties
    @property
    def state(self) -> GraphState:
        with self._lock:
            return self._state

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._started

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def stats(self) -> GraphStats:
        with self._lock:
            return GraphStats(
                nodes_added=self._stats.nodes_added,
                nodes_removed=self._stats.nodes_removed,
                relationships_added=self._stats.relationships_added,
                relationships_removed=self._stats.relationships_removed,
                flushes=self._stats.flushes,
                flush_failures=self._stats.flush_failures,
                loads=self._stats.loads,
                load_failures=self._stats.load_failures,
                transactions=self._stats.transactions,
                transaction_failures=self._stats.transaction_failures,
            )

    @property
    def config(self) -> GraphConfig:
        return self._config

    @property
    def logger(self) -> Logger:
        return self._logger

    @property
    def event_bus(self) -> Optional[EventBus]:
        return self._event_bus

    @property
    def container(self) -> Optional[Container]:
        return self._container

    @property
    def graph(self) -> nx.MultiDiGraph:
        """Expose the live graph for read paths.

        Callers using this for read-only traversal should treat it as
        immutable; mutations should go through the manager so dirty
        tracking, event publishing, and validation are consistent. For
        long-running queries across mutations, use :meth:`snapshot`.
        """
        return self._graph

    @property
    def relationships(self) -> RelationshipManager:
        return self._relationships

    @property
    def storage(self) -> GraphStorage:
        return self._storage

    @property
    def queries(self) -> QueryEngine:
        return self._queries

    @property
    def order(self) -> int:
        with self._lock:
            return self._graph.number_of_nodes()

    @property
    def size(self) -> int:
        with self._lock:
            return self._graph.number_of_edges()

    # ----------------------------------------------------- start / stop
    def start(self) -> None:
        """Bring the manager online: load or initialize, register with DI."""
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise KnowledgeGraphError("GraphManager has been shut down")
            self._state = GraphState.STARTING
        try:
            self._wire_publisher()
            self.load_or_init()
            self._register_container()
        except Exception as exc:
            with self._lock:
                self._state = GraphState.FAILED
            self._logger.error(
                "Knowledge-graph bootstrap failed",
                extra={"error": str(exc)},
            )
            self._publish_event(_EVT_FAILED, payload={"reason": str(exc)},
                               priority=EventPriority.HIGH)
            raise KnowledgeGraphError(
                f"Knowledge-graph bootstrap failed: {exc}",
                cause=exc,
            ) from exc

        with self._lock:
            self._state = GraphState.READY
            self._started = True

        self._publish_event(_EVT_LOADED, payload={"nodes": self.order, "edges": self.size})
        self._logger.info(
            "GraphManager initialized",
            extra={"nodes": self.order, "edges": self.size},
        )

    def stop(self) -> None:
        """Flush pending changes (if configured) and release the storage."""
        with self._lock:
            if self._closed:
                return
            self._state = GraphState.STOPPING
        self._emergency_shutdown(flush=self._config.flush_on_shutdown)
        with self._lock:
            self._state = GraphState.STOPPED
            self._closed = True
            self._started = False
        self._logger.info("GraphManager stopped")

    def _emergency_shutdown(self, *, flush: bool = True) -> None:
        for unsub in self._event_unsubscribers:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubscribers.clear()
        if flush:
            try:
                self.flush()
            except Exception:
                pass
        try:
            self._storage.close()
        except Exception:
            pass
        with self._lock:
            self._graph.clear()

    # ----------------------------------------------------- load / init
    def load_or_init(self) -> None:
        """Load from disk if a file exists; otherwise keep the empty graph."""
        try:
            with self._lock:
                self._graph = nx.MultiDiGraph(name="aios_knowledge_graph")
            self._storage.attach(self._graph)
            self._storage.load(into=self._graph)
            with self._lock:
                self._stats.loads += 1
        except Exception as exc:
            with self._lock:
                self._state = GraphState.DEGRADED
                self._stats.load_failures += 1
            self._logger.warning(
                "Knowledge-graph load failed; starting with empty graph",
                extra={"error": str(exc)},
            )
            with self._lock:
                self._graph = nx.MultiDiGraph(name="aios_knowledge_graph")
            self._storage.attach(self._graph)

    # ----------------------------------------------------- wire publisher
    def _wire_publisher(self) -> None:
        if self._event_bus is None or not self._config.publish_events:
            self._publisher = None
            return
        # Each event name is registered dynamically so strict-mode buses
        # accept it. ``register`` is idempotent for dynamic re-registration.
        registry = self._event_bus.registry
        for name in _EVT_ALL:
            try:
                registry.register(name=name, category=EventCategory.SYSTEM)
            except Exception:
                # Strict registry already knows the name; ignore the collision.
                pass
        self._publisher = self._event_bus.publisher("app.core.database.knowledge_graph")

    def _publish_event(
        self,
        name: str,
        *,
        payload: Mapping[str, Any],
        priority: EventPriority = EventPriority.NORMAL,
    ) -> None:
        if not self._config.publish_events or self._publisher is None:
            return
        try:
            self._publisher.emit(
                name,
                payload=dict(payload),
                category=EventCategory.SYSTEM,
                priority=priority,
                delivery_mode=EventDeliveryMode.ASYNC,
            )
        except Exception as exc:
            self._logger.debug(
                "Graph event publish failed",
                extra={"event": name, "error": str(exc)},
            )

    # ----------------------------------------------------- container
    def _register_container(self) -> None:
        if self._container is None:
            return
        self._container.register_instance(GraphManager, self, replace=True)
        self._container.register_instance(RelationshipManager, self._relationships, replace=True)
        self._container.register_instance(QueryEngine, self._queries, replace=True)

    # ----------------------------------------------------- node API
    def add_node(
        self,
        node_id: str,
        *,
        node_type: str,
        properties: Optional[Mapping[str, Any]] = None,
        merge: bool = True,
    ) -> bool:
        """Add (or merge) a node into the graph. Returns True if newly added."""
        if not node_id or not isinstance(node_id, str):
            raise KnowledgeGraphError("Node id must be a non-empty string")
        if not node_type or not isinstance(node_type, str):
            raise KnowledgeGraphError("Node type must be a non-empty string")
        with self._lock:
            existed = self._graph.has_node(node_id)
            attrs = {"type": node_type}
            if properties is not None:
                attrs.update(properties)
            self._graph.add_node(node_id, **attrs)
            if not existed:
                self._stats.nodes_added += 1
            self._storage.mark_dirty()
        if not existed:
            self._pub_event(_EVT_NODE_ADDED, {
                "node": node_id,
                "type": node_type,
                "merge": merge,
            })
        return not existed

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and all its edges. Returns True if it existed."""
        with self._lock:
            if not self._graph.has_node(node_id):
                return False
            removed_edges = list(self._graph.edges(node_id, data=True))
            self._graph.remove_node(node_id)
            self._stats.nodes_removed += 1
            self._storage.mark_dirty()
        self._pub_event(_EVT_NODE_REMOVED, {
            "node": node_id,
            "edges_removed": len(removed_edges),
        })
        return True

    def has_node(self, node_id: str) -> bool:
        with self._lock:
            return self._graph.has_node(node_id)

    def node(self, node_id: str) -> Dict[str, Any]:
        with self._lock:
            if not self._graph.has_node(node_id):
                raise KnowledgeGraphError(
                    f"Unknown node: {node_id!r}"
                ).with_context(node=node_id)
            return dict(self._graph.nodes[node_id])

    # ----------------------------------------------------- relationship API
    def add_relationship(
        self,
        source: str,
        target: str,
        type_: RelationshipType,
        *,
        weight: Optional[float] = None,
        confidence: Optional[float] = None,
        properties: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Add a typed relationship to the graph. Returns the number of edges added.

        Automatically creates missing nodes (with type="entity") when
        ``strict_relationships`` is False; raises otherwise.
        """
        with self._lock:
            missing: List[str] = []
            if not self._graph.has_node(source):
                missing.append(source)
            if not self._graph.has_node(target):
                missing.append(target)
            if missing:
                if self._config.strict_relationships:
                    raise KnowledgeGraphError(
                        "Cannot add relationship — missing node(s)",
                    ).with_context(source=source, target=target, missing=missing)
                for node_id in missing:
                    self._graph.add_node(node_id, type="entity")
                    self._stats.nodes_added += 1
                    self._pub_event_inline(_EVT_NODE_ADDED, {
                        "node": node_id,
                        "type": "entity",
                        "auto_created": True,
                    })

            rel = self._relationships.build(
                source=source,
                target=target,
                type_=type_,
                weight=weight,
                confidence=confidence,
                properties=properties,
            )
            added = self._relationships.apply_to_graph(self._graph, rel)
            self._stats.relationships_added += added
            self._storage.mark_dirty()
        self._pub_event(_EVT_RELATION_ADDED, {
            "source": source,
            "target": target,
            "type": type_.value,
            "edges_added": added,
        })
        return added

    def add_relationships(
        self,
        relationships: Iterable[Relationship],
    ) -> int:
        """Bulk-add a sequence of already-built :class:`Relationship` records."""
        count = 0
        for rel in relationships:
            with self._lock:
                if not self._graph.has_node(rel.source):
                    self._graph.add_node(rel.source, type="entity")
                if not self._graph.has_node(rel.target):
                    self._graph.add_node(rel.target, type="entity")
                count += self._relationships.apply_to_graph(self._graph, rel)
                self._storage.mark_dirty()
        with self._lock:
            self._stats.relationships_added += count
        self._pub_event(_EVT_RELATION_ADDED, {
            "bulk": True,
            "edges_added": count,
        })
        return count

    def remove_edge(
        self,
        source: str,
        target: str,
        type_: Optional[RelationshipType] = None,
    ) -> int:
        """Remove one or all edges between two nodes. Returns the count removed."""
        with self._lock:
            if not self._graph.has_edge(source, target):
                return 0
            if type_ is None:
                edges_keep = []
                for _, _, key, data in self._graph.out_edges(source, keys=True, data=True):
                    if _ == source:
                        edges_keep.append((source, target, key))
                # MultiDiGraph edges keyed; copy keys first
                keys = list(self._graph[source][target].keys())
                removed = len(keys)
                for key in keys:
                    try:
                        self._graph.remove_edge(source, target, key=key)
                    except nx.NetworkXError:
                        pass
            else:
                type_value = type_.value
                removed = 0
                keys_to_drop = []
                for key, data in self._graph[source][target].items():
                    if data.get("type") == type_value:
                        keys_to_drop.append(key)
                for key in keys_to_drop:
                    try:
                        self._graph.remove_edge(source, target, key=key)
                        removed += 1
                    except nx.NetworkXError:
                        pass
            if removed:
                self._stats.relationships_removed += removed
                self._storage.mark_dirty()
        if removed:
            self._pub_event(_EVT_RELATION_REMOVED, {
                "source": source,
                "target": target,
                "removed": removed,
            })
        return removed

    # ----------------------------------------------------- query API
    def query(self) -> QueryEngine:
        """Return the shared query engine bound to the live graph."""
        return self._queries

    def neighbors(
        self,
        node: str,
        *,
        direction: str = "out",
        options: Optional[QueryOptions] = None,
    ) -> QueryResult:
        return self._queries.neighbors(self._graph, node, direction=direction, options=options)

    def shortest_path(
        self,
        source: str,
        target: str,
        *,
        options: Optional[QueryOptions] = None,
    ) -> Optional[List[str]]:
        return self._queries.shortest_path(self._graph, source, target, options=options)

    def all_simple_paths(
        self,
        source: str,
        target: str,
        *,
        cutoff: int = 5,
        options: Optional[QueryOptions] = None,
    ) -> List[List[str]]:
        return self._queries.all_simple_paths(
            self._graph, source, target, cutoff=cutoff, options=options,
        )

    def find_nodes(
        self,
        *,
        node_type: Optional[str] = None,
        properties: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        return self._queries.find_nodes(
            self._graph,
            node_type=node_type,
            properties=properties,
            limit=limit,
        )

    def subgraph(
        self,
        nodes: Iterable[str],
        *,
        options: Optional[QueryOptions] = None,
    ) -> nx.MultiDiGraph:
        return self._queries.subgraph(self._graph, nodes, options=options)

    def snapshot(self) -> nx.MultiDiGraph:
        """Return a deep copy of the live graph for stable read paths."""
        with self._lock:
            return self._graph.copy()

    # ----------------------------------------------------- transaction
    @contextmanager
    def transaction(self) -> Iterator["GraphManager"]:
        """Batch mutations: suppress intermediate events until commit.

        Exceptions propagate; the storage dirty flag still flips so a
        partial transaction can be persisted (callers decide whether to
        rollback explicitly by removing the just-added data, but graph
        mutations are not transactional in the ACID sense — they are
        in-memory consistent because the lock is held).
        """
        with self._lock:
            self._in_transaction += 1
        try:
            yield self
        except Exception as exc:
            with self._lock:
                self._stats.transaction_failures += 1
            raise KnowledgeGraphError(
                f"Knowledge-graph transaction failed: {exc}",
                cause=exc,
            ) from exc
        finally:
            with self._lock:
                self._in_transaction -= 1
                if self._in_transaction == 0:
                    self._stats.transactions += 1
                    if self._graph.number_of_nodes() or self._graph.number_of_edges():
                        self._storage.mark_dirty()

    # ----------------------------------------------------- storage control
    def flush(self) -> None:
        """Persist pending changes immediately and cancel the debounce timer."""
        try:
            self._storage.flush()
            with self._lock:
                self._stats.flushes += 1
        except Exception:
            with self._lock:
                self._stats.flush_failures += 1
            raise
        self._pub_event(_EVT_FLUSHED, {
            "nodes": self.order,
            "edges": self.size,
            "stats": self._storage.stats.as_dict(),
        })

    def restore_from_backup(self) -> None:
        """Restore the graph from the most recent .bak file."""
        with self._lock:
            self._graph = nx.MultiDiGraph(name="aios_knowledge_graph")
        self._storage.attach(self._graph)
        self._storage.restore_from_backup()
        with self._lock:
            self._stats.loads += 1
        self._pub_event(_EVT_LOADED, {
            "restored": True,
            "nodes": self.order,
            "edges": self.size,
        })

    def reset(self) -> None:
        """Wipe the live graph AND the on-disk file + backup."""
        with self._lock:
            self._graph.clear()
        self._storage.reset()
        self._logger.warning("Knowledge graph reset to empty state (storage deleted)")

    # ----------------------------------------------------- introspection
    def describe(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "started": self.is_started,
            "closed": self.is_closed,
            "nodes": self.order,
            "edges": self.size,
            "storage": self._storage.describe(),
            "queries": self._queries.stats(),
            "stats": self.stats.as_dict(),
            "registered_types": [t.value for t in self._relationships.types()],
        }

    # ----------------------------------------------------- storage hooks
    def _on_storage_saved(self, stats: GraphStorageStats) -> None:
        self._pub_event(_EVT_FLUSHED, {
            "debounced": True,
            "nodes": stats.last_save_nodes,
            "edges": stats.last_save_edges,
        })

    # ----------------------------------------------------- internal event helper
    def _pub_event(self, name: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            if self._in_transaction:
                return  # Defer events until commit/close of the transaction.
        self._publish_event(name, payload=payload)

    def _pub_event_inline(self, name: str, payload: Mapping[str, Any]) -> None:
        """Used for events emitted while we already hold the lock."""
        # We cannot safely emit-on-bus holding our own RLock (could re-enter
        # through a synchronous subscriber). Defer to a background thread.
        threading.Thread(
            target=self._publish_event,
            args=(name,),
            kwargs={"payload": dict(payload)},
            name="kg-event",
            daemon=True,
        ).start()

    # ----------------------------------------------------- guards
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<GraphManager state={self._state.value} "
            f"nodes={self._graph.number_of_nodes()} "
            f"edges={self._graph.number_of_edges()} "
            f"started={self._started}>"
        )


# ---------------------------------------------------------------------------
# Factory + configuration loader
# ---------------------------------------------------------------------------


def default_graph_config() -> GraphConfig:
    """Return a :class:`GraphConfig` honouring project path constants."""
    return GraphConfig()


def from_config(config: ConfigManager) -> GraphConfig:
    """Build a :class:`GraphConfig` from a bootstrapped :class:`ConfigManager`.

    Reads nothing strictly required; everything is optional and bounded
    to safe defaults by the storage config.
    """
    storage = default_storage_config()
    publish_events = config.get_bool("core.knowledge_graph.publish_events", True) or True
    strict_relationships = config.get_bool("core.knowledge_graph.strict_relationships", True) or True
    flush_on_shutdown = config.get_bool("core.knowledge_graph.flush_on_shutdown", True) or True
    debounce = config.get_int("core.knowledge_graph.dirty_interval_ms", 500) or 500
    pretty = config.get_bool("core.knowledge_graph.pretty_json", False) or False

    storage.dirty_interval_ms = max(100, debounce)
    storage.pretty = pretty
    return GraphConfig(
        storage=storage,
        strict_relationships=strict_relationships,
        publish_events=publish_events,
        flush_on_shutdown=flush_on_shutdown,
    )


# ---------------------------------------------------------------------------
# DI registration
# ---------------------------------------------------------------------------


def register_knowledge_graph(
    container: Container,
    *,
    manager: Optional[GraphManager] = None,
    config: Optional[GraphConfig] = None,
    logger_factory: Optional[LoggerFactory] = None,
    event_bus: Optional[EventBus] = None,
) -> GraphManager:
    """Build (or register a pre-built) :class:`GraphManager` into ``container``.

    Used by the bootstrap on startup; the same call works for tests — pass
    a fresh container and the manager will register itself as a singleton
    and call :meth:`GraphManager.start` immediately. Callers that want
    lazy start may pass ``manager=<unstarted-instance>``.
    """
    actual_config = config or default_graph_config()
    actual_manager = manager or GraphManager(
        actual_config,
        logger_factory=logger_factory,
        event_bus=event_bus,
        container=container,
    )
    container.register_instance(GraphManager, actual_manager, replace=True)
    container.register_instance(
        RelationshipManager, actual_manager.relationships, replace=True,
    )
    container.register_instance(QueryEngine, actual_manager.query(), replace=True)
    if not actual_manager.is_started:
        actual_manager.start()
    return actual_manager


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ += [
    "GraphState",
    "GraphStats",
    "GraphConfig",
    "GraphManager",
    "default_graph_config",
    "from_config",
    "register_knowledge_graph",
]
