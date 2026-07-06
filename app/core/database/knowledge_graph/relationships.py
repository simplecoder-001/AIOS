# app/core/database/knowledge_graph/relationships.py
"""
Knowledge-graph relationship model and manager.
================================================
This module is the *typed* surface of every edge in the AIOS knowledge graph.
Edges are not bare strings: they belong to a :class:`RelationshipType` catalog
mirroring the FG2 SDD (``related_to``, ``part_of``, ``depends_on``, …) so that
the graph never invents ad-hoc predicates. Validation, weights, metadata and
direction semantics all live here; the storage and query layers depend on this
module and never construct edges themselves.

Why a dedicated module?
----------------------
* **Single source of truth** — every other layer (graph_manager, queries,
  graph_storage) refers to edge kinds through this module, so renaming a
  relationship touches one place.
* **Validation upstream** — invalid edges are rejected before they reach the
  networkx graph or the on-disk JSON, keeping persistence layer concerns
  minimal.
* **Weight + confidence semantics** — the AI brain (FG2) attaches a
  ``weight`` (0..1) representing relationship strength) and a ``confidence``
  (0..1) representing source reliability; both are enforced here.
* **Direction is part of the type** — directed (``DEPENDS_ON``) versus
  symmetric (``RELATED_TO``) is encoded so traversals can interpret edges
  without case-by-case reasoning.

Dependency order
----------------
constants → exceptions → … → database → graph_storage → here → queries
→ graph_manager. This module imports only stdlib + networkx; it never reaches
up to the event bus, state manager or DI container so it stays import-safe
from any layer beneath them.

Concurrency
-----------
The :class:`RelationshipManager` is thread-safe. The catalog is immutable at
runtime; mutation only happens through the registration API, which is guarded.
Edge construction records are pure data and freely shareable between threads.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

import networkx as nx

from app.core.exceptions.database import KnowledgeGraphError

__all__ = [
    "RelationshipType",
    "Direction",
    "Relationship",
    "RelationshipDescriptor",
    "RelationshipManager",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    """Edge directionality semantics."""

    DIRECTED = "directed"      # Source → Target only.
    SYMMETRIC = "symmetric"    # Edge implies the reverse relationship too.
    INVERSE = "inverse"        # A reverse edge of another type is implied.


class RelationshipType(str, Enum):
    """Canonical AIOS knowledge-graph relationship catalog.

    The values are deliberately stable strings — they are written to disk by
    ``graph_storage`` and must remain comparable across releases. New entries
    are appended; existing ones are never renamed without a migration.
    """

    RELATED_TO = "related_to"                # Generic, symmetric association.
    PART_OF = "part_of"                      # Composition: target contains source.
    DEPENDS_ON = "depends_on"                # Functional dependency.
    DERIVED_FROM = "derived_from"            # Lineage / provenance.
    INSTANCE_OF = "instance_of"              # Type membership.
    EQUIVALENT_TO = "equivalent_to"          # Semantic equivalence (symmetric).
    SIMILAR_TO = "similar_to"                # Soft similarity (symmetric).
    CONTRADICTS = "contradicts"              # Tension / conflict (symmetric).
    CAUSES = "causes"                         # Causal link.
    PRECEDES = "precedes"                     # Temporal ordering.
    REFERENCES = "references"                # Citation / pointer.
    MENTIONS = "mentions"                     # Soft reference inside text.
    USED_BY = "used_by"                      # Tool / resource usage.
    BELONGS_TO = "belongs_to"               # Grouping membership.
    AUTHORED_BY = "authored_by"             # Provenance — author/creator.
    KNOWS = "knows"                          # Entity-to-entity acquaintance.
    LOCATED_IN = "located_in"                # Spatial containment.
    OCCURRED_AT = "occurred_at"              # Temporal anchor.
    TRIGGERS = "triggers"                    # Behavioural trigger.
    CUSTOM = "custom"                         # Escape-hatch for callers that
                                              # register their own type.


# ---------------------------------------------------------------------------
# Relationship descriptor (catalog metadata)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationshipDescriptor:
    """Metadata for a relationship type.

    Encodes how an edge of this type should be created, traversed, and
    constrained. The :class:`RelationshipManager` keeps one descriptor per
    registered type (built-in or ``CUSTOM``), and consults it on every
    creation / reverse lookup.
    """

    type: RelationshipType
    direction: Direction
    inverse: Optional["RelationshipType"] = None
    min_weight: float = 0.0
    max_weight: float = 1.0
    default_weight: float = 0.5
    default_confidence: float = 0.5
    description: str = ""

    def clamp_weight(self, weight: Optional[float]) -> float:
        if weight is None:
            return self.default_weight
        w = float(weight)
        if w < self.min_weight:
            return self.min_weight
        if w > self.max_weight:
            return self.max_weight
        return w

    def clamp_confidence(self, confidence: Optional[float]) -> float:
        if confidence is None:
            return self.default_confidence
        c = float(confidence)
        if c < 0.0:
            return 0.0
        if c > 1.0:
            return 1.0
        return c

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "direction": self.direction.value,
            "inverse": self.inverse.value if self.inverse else None,
            "min_weight": self.min_weight,
            "max_weight": self.max_weight,
            "default_weight": self.default_weight,
            "default_confidence": self.default_confidence,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# Relationship record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Relationship:
    """A typed edge between two node identifiers.

    Instances are produced solely by :class:`RelationshipManager.build`, which
    validates the type, weights, and applies catalog semantics (symmetric /
    inverse) before returning. They are plain data after that: queries and
    storage iterate them without needing the manager.
    """

    source: str
    target: str
    type: RelationshipType
    weight: float
    confidence: float
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def key(self) -> Tuple[str, str, str]:
        """Return the ``(source, target, type)`` triple uniquely identifying."""
        return (self.source, self.target, self.type.value)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.type.value,
            "weight": self.weight,
            "confidence": self.confidence,
            "properties": dict(self.properties),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Relationship":
        """Reconstruct a Relationship from its serialized form."""
        type_name = data.get("type")
        try:
            rtype = RelationshipType(type_name)
        except ValueError as exc:
            raise KnowledgeGraphError(
                f"Unknown relationship type during deserialization: {type_name!r}",
                cause=exc,
            ) from exc
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            type=rtype,
            weight=float(data.get("weight", 0.5)),
            confidence=float(data.get("confidence", 0.5)),
            properties=dict(data.get("properties", {})),
            created_at=float(data.get("created_at", time.time())),
        )


# ---------------------------------------------------------------------------
# RelationshipManager
# ---------------------------------------------------------------------------


class RelationshipManager:
    """Owns the relationship catalog and validates + builds edges.

    The manager constructs built-in descriptors at import time, exposes
    registration for callers needing custom predicates, and produces
    :class:`Relationship` instances whose direction semantics (symmetric /
    inverse) are materialized by the caller (typically the storage layer).

    Thread safety
    -------------
    The catalog dict is mutated under a lock. Read paths (``descriptor``,
    ``is_known``, ``build`` for known types) only consult the catalog without
    mutating, but still acquire the lock to avoid tearing reads.
    """

    __slots__ = (
        "_catalog",
        "_lock",
        "_stats",
        "_on_register",
    )

    def __init__(self) -> None:
        self._catalog: Dict[RelationshipType, RelationshipDescriptor] = {}
        self._lock = threading.RLock()
        self._stats = _RelationshipStats()
        self._on_register: List[Callable[[RelationshipDescriptor], None]] = []
        self._register_builtins()

    # ----------------------------------------------------- catalog mutation
    def register(
        self,
        type_: RelationshipType,
        direction: Direction,
        *,
        inverse: Optional[RelationshipType] = None,
        min_weight: float = 0.0,
        max_weight: float = 1.0,
        default_weight: float = 0.5,
        default_confidence: float = 0.5,
        description: str = "",
        replace: bool = False,
    ) -> RelationshipDescriptor:
        """Register (or replace) a relationship descriptor.

        Existing built-in descriptors can be replaced only with
        ``replace=True`` to prevent silent semantic drift.
        """
        if min_weight > max_weight:
            raise KnowledgeGraphError(
                "Relationship descriptor with min_weight > max_weight",
            ).with_context(type=type_.value)
        descriptor = RelationshipDescriptor(
            type=type_,
            direction=direction,
            inverse=inverse,
            min_weight=min_weight,
            max_weight=max_weight,
            default_weight=default_weight,
            default_confidence=default_confidence,
            description=description,
        )
        with self._lock:
            existing = self._catalog.get(type_)
            if existing is not None and not replace:
                raise KnowledgeGraphError(
                    f"Relationship type already registered: {type_.value}",
                ).with_context(type=type_.value)
            self._catalog[type_] = descriptor
            self._stats.types_registered = len(self._catalog)
        self._fire_register(descriptor)
        return descriptor

    def register_listener(self, callback: Callable[[RelationshipDescriptor], None]) -> None:
        """Subscribe to descriptor registrations (used by graph_manager)."""
        with self._lock:
            self._on_register.append(callback)

    # ----------------------------------------------------- catalog access
    def descriptor(self, type_: RelationshipType) -> RelationshipDescriptor:
        with self._lock:
            descriptor = self._catalog.get(type_)
            if descriptor is None:
                raise KnowledgeGraphError(
                    f"Unknown relationship type: {type_.value}",
                ).with_context(type=type_.value)
            return descriptor

    def is_known(self, type_: RelationshipType) -> bool:
        with self._lock:
            return type_ in self._catalog

    def types(self) -> List[RelationshipType]:
        with self._lock:
            return list(self._catalog.keys())

    def descriptors(self) -> List[RelationshipDescriptor]:
        with self._lock:
            return list(self._catalog.values())

    def stats(self) -> "_RelationshipStats":
        with self._lock:
            # Return a shallow copy so callers cannot mutate state externally.
            return _RelationshipStats(
                types_registered=self._stats.types_registered,
                edges_built=self._stats.edges_built,
                invalid_rejected=self._stats.invalid_rejected,
            )

    # ----------------------------------------------------- edge construction
    def build(
        self,
        source: str,
        target: str,
        type_: RelationshipType,
        *,
        weight: Optional[float] = None,
        confidence: Optional[float] = None,
        properties: Optional[Mapping[str, Any]] = None,
    ) -> Relationship:
        """Validate and build a :class:`Relationship`.

        Raises :class:`KnowledgeGraphError` for malformed input. The returned
        relationship is *materialized* once: symmetric / inverse semantics
        produce additional edges only when asked via :meth:`materialize`.
        """
        if not source or not isinstance(source, str):
            raise KnowledgeGraphError(
                "Relationship source must be a non-empty string",
            ).with_context(type=type_.value)
        if not target or not isinstance(target, str):
            raise KnowledgeGraphError(
                "Relationship target must be a non-empty string",
            ).with_context(type=type_.value)
        if source == target:
            raise KnowledgeGraphError(
                "Relationship source and target must differ",
            ).with_context(type=type_.value, source=source)

        with self._lock:
            descriptor = self._catalog.get(type_)
            if descriptor is None:
                self._stats.invalid_rejected += 1
                raise KnowledgeGraphError(
                    f"Unknown relationship type: {type_.value}",
                ).with_context(type=type_.value)
            rel = Relationship(
                source=source,
                target=target,
                type=type_,
                weight=descriptor.clamp_weight(weight),
                confidence=descriptor.clamp_confidence(confidence),
                properties=dict(properties or {}),
            )
            self._stats.edges_built += 1
        return rel

    def materialize(
        self,
        relationship: Relationship,
    ) -> List[Relationship]:
        """Expand a relationship into all its materialized edges.

        * ``DIRECTED`` → single edge.
        * ``SYMMETRIC`` → two edges (forward + reverse).
        * ``INVERSE``  → forward + reverse typed via the inverse descriptor.
        """
        with self._lock:
            descriptor = self._catalog.get(relationship.type)
            if descriptor is None:
                raise KnowledgeGraphError(
                    f"Unregistered relationship type: {relationship.type.value}",
                ).with_context(type=relationship.type.value)

        edges: List[Relationship] = [relationship]
        if descriptor.direction is Direction.SYMMETRIC:
            edges.append(
                Relationship(
                    source=relationship.target,
                    target=relationship.source,
                    type=relationship.type,
                    weight=relationship.weight,
                    confidence=relationship.confidence,
                    properties=dict(relationship.properties),
                    created_at=relationship.created_at,
                )
            )
        elif descriptor.direction is Direction.INVERSE and descriptor.inverse is not None:
            edges.append(
                Relationship(
                    source=relationship.target,
                    target=relationship.source,
                    type=descriptor.inverse,
                    weight=relationship.weight,
                    confidence=relationship.confidence,
                    properties=dict(relationship.properties),
                    created_at=relationship.created_at,
                )
            )
        return edges

    def reverse_lookup(self, type_: RelationshipType) -> Optional[RelationshipType]:
        """Return the registered inverse type for ``type_``, if any."""
        with self._lock:
            descriptor = self._catalog.get(type_)
            return descriptor.inverse if descriptor is not None else None

    # ----------------------------------------------------- helpers
    def apply_to_graph(
        self,
        graph: nx.Graph,
        relationship: Relationship,
    ) -> int:
        """Apply a relationship to a networkx graph, honouring direction.

        Returns the number of edges actually added (1 or 2).
        """
        if not graph.has_node(relationship.source):
            raise KnowledgeGraphError(
                f"Missing source node for edge: {relationship.source}",
            ).with_context(type=relationship.type.value, source=relationship.source)
        if not graph.has_node(relationship.target):
            raise KnowledgeGraphError(
                f"Missing target node for edge: {relationship.target}",
            ).with_context(type=relationship.type.value, target=relationship.target)
        materialized = self.materialize(relationship)
        added = 0
        for edge in materialized:
            data = edge.as_dict()
            data.pop("source", None)
            data.pop("target", None)
            graph.add_edge(edge.source, edge.target, **data)
            added += 1
        return added

    def iter_edges(self, graph: nx.Graph) -> Iterator[Relationship]:
        """Yield each graph edge as a :class:`Relationship`."""
        for source, target, data in graph.edges(data=True):
            type_name = data.get("type")
            if type_name is None:
                continue
            try:
                rtype = RelationshipType(type_name)
            except ValueError:
                continue
            yield Relationship(
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
                created_at=float(data.get("created_at", time.time())),
            )

    # ----------------------------------------------------- internals
    def _fire_register(self, descriptor: RelationshipDescriptor) -> None:
        for callback in list(self._on_register):
            try:
                callback(descriptor)
            except Exception:
                # Listener failures must never block registration.
                pass

    def _register_builtins(self) -> None:
        builtins: Iterable[Tuple[RelationshipType, Direction, Optional[RelationshipType], str]] = (
            (RelationshipType.RELATED_TO, Direction.SYMMETRIC, None,
             "Generic symmetric association between two entities."),
            (RelationshipType.PART_OF, Direction.DIRECTED, None,
             "Composition edge; target contains the source."),
            (RelationshipType.DEPENDS_ON, Direction.DIRECTED, None,
             "Functional dependency from source to target."),
            (RelationshipType.DERIVED_FROM, Direction.DIRECTED, None,
             "Lineage edge; source derives from target."),
            (RelationshipType.INSTANCE_OF, Direction.DIRECTED, None,
             "Type membership: source is an instance of target."),
            (RelationshipType.EQUIVALENT_TO, Direction.SYMMETRIC, None,
             "Semantic equivalence between two entities."),
            (RelationshipType.SIMILAR_TO, Direction.SYMMETRIC, None,
             "Soft similarity relationship."),
            (RelationshipType.CONTRADICTS, Direction.SYMMETRIC, None,
             "Tension or conflict relationship."),
            (RelationshipType.CAUSES, Direction.DIRECTED, None,
             "Causal link from source to target."),
            (RelationshipType.PRECEDES, Direction.DIRECTED, None,
             "Temporal ordering: source precedes target."),
            (RelationshipType.REFERENCES, Direction.DIRECTED, None,
             "Citation or pointer from source to target."),
            (RelationshipType.MENTIONS, Direction.DIRECTED, None,
             "Soft textual reference."),
            (RelationshipType.USED_BY, Direction.INVERSE, RelationshipType.RELATED_TO,
             "Resource usage; inverse is a generic related_to."),
            (RelationshipType.BELONGS_TO, Direction.DIRECTED, None,
             "Grouping membership."),
            (RelationshipType.AUTHORED_BY, Direction.DIRECTED, None,
             "Provenance edge — source authored by target."),
            (RelationshipType.KNOWS, Direction.SYMMETRIC, None,
             "Entity-to-entity acquaintance."),
            (RelationshipType.LOCATED_IN, Direction.DIRECTED, None,
             "Spatial containment."),
            (RelationshipType.OCCURRED_AT, Direction.DIRECTED, None,
             "Temporal anchor."),
            (RelationshipType.TRIGGERS, Direction.DIRECTED, None,
             "Behavioural trigger from source to target."),
            (RelationshipType.CUSTOM, Direction.DIRECTED, None,
             "Escape-hatch for caller-registered custom relationships."),
        )
        for type_, direction, inverse, description in builtins:
            self._catalog[type_] = RelationshipDescriptor(
                type=type_,
                direction=direction,
                inverse=inverse,
                description=description,
            )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        with self._lock:
            return (
                f"<RelationshipManager types={len(self._catalog)} "
                f"edges_built={self._stats.edges_built}>"
            )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RelationshipStats:
    types_registered: int = 0
    edges_built: int = 0
    invalid_rejected: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "types_registered": self.types_registered,
            "edges_built": self.edges_built,
            "invalid_rejected": self.invalid_rejected,
        }


# ---------------------------------------------------------------------------
# Public API (supplemental re-exports)
# ---------------------------------------------------------------------------


__all__ += [
    "RelationshipType",
    "Direction",
    "RelationshipDescriptor",
    "Relationship",
    "RelationshipManager",
]
