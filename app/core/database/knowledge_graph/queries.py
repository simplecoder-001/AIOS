# app/core/database/knowledge_graph/queries.py
"""
Read-side query engine for the AIOS knowledge graph.
====================================================
Once :mod:`graph_storage` keeps the graph alive and :mod:`relationships`
defines the typed edge catalog, this module is the *read surface* every
feature group (FG2 context builder, FG9 agent planner, FG10 learning
sub-graph mining) uses to extract knowledge from the graph.

It deliberately exposes a *small* set of operations so callers stay on
well-defined paths; anything ad-hoc should be added as a new method here
instead of being written inline at call sites.

Operations
----------
* :meth:`neighbors` — typed 1-hop traversal with weight/confidence filters.
* :meth:`shortest_path` — minimum-hop path between two nodes, optionally
  restricted to a relationship-type subset.
* :meth:`all_simple_paths` — bounded-depth enumeration of every simple
  path between two nodes (used by FG10 pattern-mining).
* :meth:`subgraph` — extract a node + edge induced subgraph for visualization.
* :meth:`ancestors` / :meth:`descendants` — transitive closure on the
  directed edges (provenance, dependency chains).
* :meth:`most_central`, :meth:`degree_ranking` — common-topology helpers.
* :meth:`find_nodes` — attribute/predicate-style node lookup.

Design rules
------------
* Implements operate on the live networkx graph the GraphManager gives it.
  Storage and persistence are not touched here; the GraphManager snapshots
  to disk through ``GraphStorage`` only when mutations happened.
* Read methods are *never* mutating; they accept a snapshot graph copy at
  the GraphManager layer when they need a stable view across a long query.
* All weights are treated as distances via ``1 / weight`` so high-strength
  relationships are preferred over weak ones. This is consistent across
  methods to keep ranking semantics predictable for callers.

Dependency order
----------------
constants → exceptions → … → relationships → graph_storage → here.
This module imports networkx, stdlib, and ``KnowledgeGraphError`` only —
keeping it import-safe from any layer beneath the event bus / state
manager. The query engine never depends on the GraphManager itself (it
operates on the graph the manager hands it) so no cycle is created.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

import networkx as nx

from app.core.database.knowledge_graph.relationships import Relationship, RelationshipType
from app.core.exceptions.database import KnowledgeGraphError

__all__ = [
    "QueryOptions",
    "QueryResult",
    "QueryEngine",
]


# ---------------------------------------------------------------------------
# Options + result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class QueryOptions:
    """Common filters applied across most query methods."""

    relationship_types: Optional[Iterable[RelationshipType]] = None
    min_confidence: float = 0.0   # Inclusive lower bound; edges below are filtered out.
    min_weight: float = 0.0       # Inclusive lower bound on edge weight.
    max_weight: float = 1.0       # Inclusive upper bound on edge weight.
    limit: Optional[int] = None   # Optional result cap.
    include_properties: bool = False  # Include edge/node property dicts in the result.

    def type_filter(self) -> Optional[Set[str]]:
        if self.relationship_types is None:
            return None
        return {t.value for t in self.relationship_types}

    def matches_edge(
        self,
        weight: float,
        confidence: float,
        type_value: Optional[str] = None,
    ) -> bool:
        if confidence < self.min_confidence:
            return False
        if weight < self.min_weight or weight > self.max_weight:
            return False
        types = self.type_filter()
        if types is not None:
            if type_value is None or type_value not in types:
                return False
        return True


@dataclass(slots=True)
class QueryResult:
    """Container returned by query methods — enables extension without API breaks."""

    nodes: List[str] = field(default_factory=list)
    edges: List[Relationship] = field(default_factory=list)
    paths: List[List[str]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "nodes": list(self.nodes),
            "edges": [edge.as_dict() for edge in self.edges],
            "paths": [list(path) for path in self.paths],
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# QueryEngine
# ---------------------------------------------------------------------------


class QueryEngine:
    """Read-only query facade over a networkx MultiDiGraph.

    Instances are stateless beyond the (optional) stats counters and a
    reference to the graph they operate on. Messy statefulness lives in
    the GraphManager, never here — this allows tests to construct one
    quickly against any throwaway graph.
    """

    __slots__ = (
        "_stats",
        "_lock",
    )

    def __init__(self) -> None:
        self._stats = _QueryStats()
        self._lock = threading.Lock()

    # ----------------------------------------------------- neighbors
    def neighbors(
        self,
        graph: nx.MultiDiGraph,
        node: str,
        *,
        direction: str = "out",   # "out" | "in" | "both"
        options: Optional[QueryOptions] = None,
    ) -> QueryResult:
        """Return typed 1-hop neighbours of ``node``.

        ``direction`` selects outgoing edges, incoming edges, or both. The
        returned ``nodes`` list de-duplicates neighbour ids, while ``edges``
        carries the typed :class:`Relationship` records for every kept
        edge.
        """
        if direction not in {"out", "in", "both"}:
            raise KnowledgeGraphError(
                f"Invalid direction value: {direction!r}"
            ).with_context(direction=direction)
        if not graph.has_node(node):
            raise KnowledgeGraphError(
                f"Query against unknown node: {node!r}"
            ).with_context(node=node)

        opts = options or QueryOptions()
        kept_nodes: List[str] = []
        kept_edges: List[Relationship] = []
        seen: Set[str] = set()

        if direction in {"out", "both"}:
            for _, target, key, data in graph.out_edges(node, keys=True, data=True):
                rel = self._edge_to_relationship(node, target, data)
                if rel is None:
                    continue
                if not self._edge_match(opts, rel):
                    continue
                kept_edges.append(rel)
                if target not in seen:
                    seen.add(target)
                    kept_nodes.append(target)

        if direction in {"in", "both"}:
            for source, _, key, data in graph.in_edges(node, keys=True, data=True):
                rel = self._edge_to_relationship(source, node, data)
                if rel is None:
                    continue
                if not self._edge_match(opts, rel):
                    continue
                kept_edges.append(rel)
                if source not in seen:
                    seen.add(source)
                    kept_nodes.append(source)

        if opts.limit is not None:
            kept_nodes = kept_nodes[: opts.limit]
            kept_edges = kept_edges[: opts.limit]

        self._bump("neighbors")
        return QueryResult(
            nodes=kept_nodes,
            edges=kept_edges,
            metadata={"root": node, "direction": direction, "matchers": opts.type_filter()},
        )

    # ----------------------------------------------------- paths
    def shortest_path(
        self,
        graph: nx.MultiDiGraph,
        source: str,
        target: str,
        *,
        options: Optional[QueryOptions] = None,
    ) -> Optional[List[str]]:
        """Return the minimum-weight path from ``source`` to ``target``.

        Edge weights are treated as inverse-distance (high weight = strong
        link = cheap), so shortest paths favour high-confidence edges.
        ``None`` is returned when no path exists.
        """
        self._require_nodes(graph, (source, target))
        opts = options or QueryOptions()
        view = self._filtered_view(graph, opts)
        weight_fn = self._weight_function(opts)

        # Simple direct-probe first: a single-step edge is common in practice.
        if view.has_edge(source, target):
            self._bump("shortest_path")
            return [source, target]

        try:
            path = nx.shortest_path(view, source=source, target=target, weight=weight_fn)
        except nx.NetworkXNoPath:
            self._bump("shortest_path")
            return None
        except nx.NodeNotFound:
            raise KnowledgeGraphError(
                f"Path endpoints not present in filtered view",
            ).with_context(source=source, target=target)
        self._bump("shortest_path")
        return list(path)

    def all_simple_paths(
        self,
        graph: nx.MultiDiGraph,
        source: str,
        target: str,
        *,
        cutoff: int = 5,
        options: Optional[QueryOptions] = None,
    ) -> List[List[str]]:
        """Return every simple path up to ``cutoff`` hops inclusively."""
        if cutoff <= 0:
            raise KnowledgeGraphError(
                f"all_simple_paths cutoff must be positive; got {cutoff}"
            ).with_context(cutoff=cutoff)
        self._require_nodes(graph, (source, target))
        opts = options or QueryOptions()
        view = self._filtered_view(graph, opts)
        try:
            paths = nx.all_simple_paths(view, source=source, target=target, cutoff=cutoff)
        except nx.NodeNotFound:
            self._bump("all_simple_paths")
            return []
        self._bump("all_simple_paths")
        result: List[List[str]] = []
        for path in paths:
            if opts.limit is not None and len(result) >= opts.limit:
                break
            result.append(list(path))
        return result

    # ----------------------------------------------------- closures
    def ancestors(self, graph: nx.MultiDiGraph, node: str) -> List[str]:
        """Return all upstream nodes that transitively reach ``node``."""
        if not graph.has_node(node):
            raise KnowledgeGraphError(
                f"Query against unknown node: {node!r}"
            ).with_context(node=node)
        self._bump("ancestors")
        return list(nx.ancestors(graph, node))

    def descendants(self, graph: nx.MultiDiGraph, node: str) -> List[str]:
        """Return all downstream nodes ``node`` transitively reaches."""
        if not graph.has_node(node):
            raise KnowledgeGraphError(
                f"Query against unknown node: {node!r}"
            ).with_context(node=node)
        self._bump("descendants")
        return list(nx.descendants(graph, node))

    # ----------------------------------------------------- subgraph
    def subgraph(
        self,
        graph: nx.MultiDiGraph,
        nodes: Iterable[str],
        *,
        options: Optional[QueryOptions] = None,
    ) -> nx.MultiDiGraph:
        """Return an induced subgraph restricted to ``nodes`` + filtered edges."""
        node_set = set(nodes)
        for node in node_set:
            if not graph.has_node(node):
                raise KnowledgeGraphError(
                    f"Subgraph references unknown node: {node!r}"
                ).with_context(node=node)
        opts = options or QueryOptions()
        induced = nx.MultiDiGraph(graph).subgraph(node_set).copy()
        if opts.relationship_types is not None:
            types = opts.type_filter() or set()
            keep = [edge for edge in induced.edges(data=True)
                    if edge[2].get("type") in types]
            induced.remove_edges_from(
                edge for edge in induced.edges(data=True) if edge not in keep
            )
        # Apply confidence / weight cutoffs on the surviving edges.
        if opts.min_confidence > 0 or opts.min_weight > 0 or opts.max_weight < 1.0:
            drop = []
            for source, target, data in induced.edges(data=True):
                conf = float(data.get("confidence", 0.0))
                weight = float(data.get("weight", 0.0))
                if conf < opts.min_confidence:
                    drop.append((source, target))
                elif weight < opts.min_weight or weight > opts.max_weight:
                    drop.append((source, target))
            induced.remove_edges_from(drop)
        self._bump("subgraph")
        return induced

    # ----------------------------------------------------- search
    def find_nodes(
        self,
        graph: nx.MultiDiGraph,
        *,
        node_type: Optional[str] = None,
        properties: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        """Return node ids matching ``node_type`` and property values exactly."""
        if node_type is None and not properties:
            raise KnowledgeGraphError(
                "find_nodes requires a node_type or properties filter"
            )
        result: List[str] = []
        for node_id, data in graph.nodes(data=True):
            if node_type is not None and data.get("type") != node_type:
                continue
            if properties is not None:
                matched = True
                for key, expected in properties.items():
                    if data.get(key) != expected:
                        matched = False
                        break
                if not matched:
                    continue
            result.append(node_id)
            if limit is not None and len(result) >= limit:
                break
        self._bump("find_nodes")
        return result

    # ----------------------------------------------------- centrality
    def most_central(
        self,
        graph: nx.MultiDiGraph,
        *,
        metric: str = "degree",
        limit: int = 10,
    ) -> List[Tuple[str, float]]:
        """Return the top ``limit`` nodes by a centrality measure.

        Supported metrics: ``degree``, ``in_degree``, ``out_degree``,
        ``betweenness``, ``closeness``. Heavy computations (betweenness,
        closeness) are gated by size guards because they are O(V*E).
        """
        if limit <= 0:
            raise KnowledgeGraphError(
                f"most_central limit must be positive; got {limit}"
            ).with_context(limit=limit)
        if metric not in {"degree", "in_degree", "out_degree", "betweenness", "closeness"}:
            raise KnowledgeGraphError(
                f"Unsupported centrality metric: {metric!r}"
            ).with_context(metric=metric)
        if graph.number_of_nodes() == 0:
            self._bump("most_central")
            return []

        if metric == "degree":
            ranking = dict(graph.degree())
        elif metric == "in_degree":
            ranking = dict(graph.in_degree())
        elif metric == "out_degree":
            ranking = dict(graph.out_degree())
        elif metric == "betweenness":
            # Betweenness on a MultiDiGraph collides parallel edges; we
            # collapse to a simple graph view for the computation.
            if graph.number_of_nodes() > 5000:
                # O(V*E) on large graphs is too expensive synchronously.
                raise KnowledgeGraphError(
                    "betweenness centrality refuses graphs > 5000 nodes",
                ).with_context(nodes=graph.number_of_nodes())
            simplified = nx.DiGraph(graph)
            ranking = nx.betweenness_centrality(simplified, normalized=True)
        else:  # closeness
            if graph.number_of_nodes() > 5000:
                raise KnowledgeGraphError(
                    "closeness centrality refuses graphs > 5000 nodes",
                ).with_context(nodes=graph.number_of_nodes())
            simplified = nx.DiGraph(graph)
            ranking = nx.closeness_centrality(simplified)
        ranked = sorted(ranking.items(), key=lambda item: (-float(item[1]), item[0]))
        self._bump("most_central")
        return ranked[:limit]

    def degree_ranking(
        self,
        graph: nx.MultiDiGraph,
        *,
        limit: int = 10,
    ) -> List[Tuple[str, int]]:
        """Top ``limit`` nodes by total degree (in + out)."""
        if limit <= 0:
            raise KnowledgeGraphError(
                f"degree_ranking limit must be positive; got {limit}"
            ).with_context(limit=limit)
        ranked = sorted(
            ((node, int(degree)) for node, degree in graph.degree()),
            key=lambda item: (-item[1], item[0]),
        )
        self._bump("degree_ranking")
        return ranked[:limit]

    # ----------------------------------------------------- traversal
    def bfs(
        self,
        graph: nx.MultiDiGraph,
        start: str,
        *,
        max_depth: int = 2,
        options: Optional[QueryOptions] = None,
    ) -> List[str]:
        """Breadth-first outward traversal up to ``max_depth`` hops."""
        if max_depth <= 0:
            raise KnowledgeGraphError(
                f"bfs max_depth must be positive; got {max_depth}"
            ).with_context(max_depth=max_depth)
        if not graph.has_node(start):
            raise KnowledgeGraphError(
                f"bfs against unknown node: {start!r}"
            ).with_context(node=start)
        opts = options or QueryOptions()
        visited: Set[str] = {start}
        frontier: List[str] = [start]
        result: List[str] = [start]
        for _depth in range(max_depth):
            next_frontier: List[str] = []
            for node in frontier:
                for _, target, data in graph.out_edges(node, data=True):
                    rel = self._edge_to_relationship(node, target, data)
                    if rel is None or not self._edge_match(opts, rel):
                        continue
                    if target in visited:
                        continue
                    visited.add(target)
                    result.append(target)
                    next_frontier.append(target)
            frontier = next_frontier
            if not frontier:
                break
        if opts.limit is not None:
            result = result[: opts.limit]
        self._bump("bfs")
        return result

    # ----------------------------------------------------- stats
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return self._stats.as_dict()

    # ----------------------------------------------------- internals
    def _edge_to_relationship(
        self,
        source: str,
        target: str,
        data: Mapping[str, Any],
    ) -> Optional[Relationship]:
        type_value = data.get("type")
        if type_value is None:
            return None
        try:
            rtype = RelationshipType(str(type_value))
        except ValueError:
            return None
        return Relationship(
            source=source,
            target=target,
            type=rtype,
            weight=float(data.get("weight", 0.5)),
            confidence=float(data.get("confidence", 0.5)),
            properties={
                k: v
                for k, v in data.items()
                if k not in {"type", "weight", "confidence", "source", "target"}
            },
        )

    def _edge_match(self, opts: QueryOptions, rel: Relationship) -> bool:
        return opts.matches_edge(
            weight=rel.weight,
            confidence=rel.confidence,
            type_value=rel.type.value,
        )

    def _filtered_view(self, graph: nx.MultiDiGraph, opts: QueryOptions) -> nx.MultiDiGraph:
        """Return a copy of ``graph`` with edges filtered per ``opts``."""
        if not opts.relationship_types and opts.min_confidence <= 0.0 \
                and opts.min_weight <= 0.0 and opts.max_weight >= 1.0:
            return graph
        view = nx.MultiDiGraph()
        view.add_nodes_from(graph.nodes(data=True))
        for source, target, data in graph.edges(data=True):
            rel = self._edge_to_relationship(source, target, data)
            if rel is None:
                continue
            if self._edge_match(opts, rel):
                view.add_edge(source, target, **dict(data))
        return view

    def _weight_function(self, opts: QueryOptions) -> Callable[[str, str, Mapping[str, Any]], float]:
        types = opts.type_filter()

        def weight_fn(source: str, target: str, data: Mapping[str, Any]) -> float:
            w = float(data.get("weight", 0.5))
            if w <= 0.0:
                return 1e6
            return 1.0 / w

        return weight_fn

    def _require_nodes(self, graph: nx.MultiDiGraph, nodes: Iterable[str]) -> None:
        for node in nodes:
            if not graph.has_node(node):
                raise KnowledgeGraphError(
                    f"Query against unknown node: {node!r}"
                ).with_context(node=node)

    def _bump(self, name: str) -> None:
        with self._lock:
            self._stats.bump(name)


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _QueryStats:
    neighbors: int = 0
    shortest_path: int = 0
    all_simple_paths: int = 0
    ancestors: int = 0
    descendants: int = 0
    subgraph: int = 0
    find_nodes: int = 0
    most_central: int = 0
    degree_ranking: int = 0
    bfs: int = 0

    def bump(self, name: str) -> None:
        # setattr on a slots dataclass — keep names alphabetic here.
        if hasattr(self, name):
            current = getattr(self, name)
            setattr(self, name, current + 1)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "neighbors": self.neighbors,
            "shortest_path": self.shortest_path,
            "all_simple_paths": self.all_simple_paths,
            "ancestors": self.ancestors,
            "descendants": self.descendants,
            "subgraph": self.subgraph,
            "find_nodes": self.find_nodes,
            "most_central": self.most_central,
            "degree_ranking": self.degree_ranking,
            "bfs": self.bfs,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ += [
    "QueryOptions",
    "QueryResult",
    "QueryEngine",
]
