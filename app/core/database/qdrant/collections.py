# app/core/database/qdrant/collections.py
"""
Qdrant collection catalog and schema management.
===============================================
Defines the vector collections used across AIOS (FG2 memory, FG10 learning,
FG8 productivity) and the policies for their creation, optimization, and
lifecycle. Collections are described declaratively so that the client can
recreate them idempotently on a fresh Qdrant instance and so that callers
never hardcode names or vector sizes elsewhere.

Each :class:`CollectionSpec` carries everything Qdrant needs to create a
collection:

* a stable name (the only string other modules should ever reference);
* the embedding dimension (must match the model used by ``embeddings.py``);
* the distance metric;
* an HNSW index configuration for ANN search performance;
* an optional payload schema for indexed scalar fields used in filters;
* a retention/importance policy for memory governance (FG2 §14-17, FG10).

The module is import-safe and side-effect free. The actual ``create_collection``
calls live in :mod:`app.core.database.qdrant.client`. The Qdrant SDK model
classes (``Distance``, ``VectorParams``, ``OptimizersConfigDiff``,
``ScalarQuantization``) are imported from ``qdrant_client.http.models`` and
exposed/translated through the spec dataclasses so callers get typed objects
instead of hand-rolled dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional, Tuple

from qdrant_client.http.models import (
    Distance,
    OptimizersConfigDiff,
    ScalarQuantization,
    VectorParams,
)

__all__ = [
    "DistanceMetric",
    "HnswConfig",
    "PayloadIndex",
    "PayloadIndexType",
    "CollectionSpec",
    "RetentionPolicy",
    "CollectionName",
    "DEFAULT_COLLECTIONS",
    "get_collection_spec",
    "is_known_collection",
    "COLLECTION_NAMES",
    "to_dict",
]


# ---------------------------------------------------------------------------
# Distance metric
# ---------------------------------------------------------------------------


class DistanceMetric(str, Enum):
    """Qdrant-supported vector distance metrics.

    The default for sentence-transformers models (all-MiniLM-L6-v2,
    multilingual-e5-small) is ``COSINE`` because normalized embeddings compare
    best by cosine similarity. The string values mirror the
    :class:`qdrant_client.http.models.Distance` enum members so the mapping is
    a direct attribute lookup.
    """

    COSINE = "Cosine"
    DOT = "Dot"
    EUCLID = "Euclid"
    MANHATTAN = "Manhattan"

    def to_qdrant(self) -> Distance:
        """Return the matching :class:`qdrant_client.http.models.Distance`."""
        return Distance[self.value.upper()]


# ---------------------------------------------------------------------------
# HNSW (Hierarchical Navigable Small World) index configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HnswConfig:
    """Tunable HNSW index parameters.

    Defaults follow Qdrant's recommended values for general-purpose semantic
    search workloads of ~1M points. ``m`` controls graph branching; a higher
    value improves recall at the cost of memory. ``ef_construct`` controls
    build-time accuracy; ``ef_search`` (a query-time parameter) is set on the
    client, not the collection, so it is not stored here.
    """

    m: int = 16
    ef_construct: int = 100
    full_scan_threshold: int = 10_000
    max_indexing_threads: int = 0  # 0 = auto (use all available cores)
    on_disk: bool = False  # keep the graph in RAM (in-memory mode)
    payload_m: Optional[int] = None  # per-field branching override

    def to_qdrant(self) -> dict[str, Any]:
        """Return a dict usable as the ``hnsw_config`` argument of
        ``create_collection``. The SDK accepts a plain dict for backward
        compatibility; this dict mirrors the HnswConfigDiff schema.
        """
        cfg: dict[str, Any] = {
            "m": self.m,
            "ef_construct": self.ef_construct,
            "full_scan_threshold": self.full_scan_threshold,
            "max_indexing_threads": self.max_indexing_threads,
            "on_disk": self.on_disk,
        }
        if self.payload_m is not None:
            cfg["payload_m"] = self.payload_m
        return cfg


DEFAULT_HNSW: Final[HnswConfig] = HnswConfig()


# ---------------------------------------------------------------------------
# Payload scalar indexes
# ---------------------------------------------------------------------------


class PayloadIndexType(str, Enum):
    """Qdrant payload index types used for filtering on metadata."""

    KEYWORD = "keyword"  # exact-match strings (collection, source, tags)
    INTEGER = "integer"  # importance score, version numbers
    FLOAT = "float"  # confidence, similarity scores
    BOOL = "bool"  # is_active, is_personal
    DATETIME = "datetime"  # created_at, expires_at (stored as RFC3339)
    TEXT = "text"  # full-text search on summaries


@dataclass(frozen=True, slots=True)
class PayloadIndex:
    """A scalar index over a payload field for fast filtering."""

    field: str
    index_type: PayloadIndexType
    is_tenant: bool = False  # optimize for tenant-style partitioning

    def to_qdrant(self) -> dict[str, Any]:
        """Return the field schema dict accepted by ``create_payload_index``.

        The SDK accepts a plain string (matching the ``PayloadSchemaType`` enum)
        for backward compatibility, so we return ``{"type": <name>}``.
        """
        return {"type": self.index_type.value}


# ---------------------------------------------------------------------------
# Retention policy (memory governance: FG2 §16, FG10 §Engine 8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How points in a collection expire or get archived.

    A ``max_age_days`` of ``None`` means the collection is permanent unless
    explicitly expired by the memory governance engine (FG2 §14 permanent
    memories, FG10 §Engine 8 archival). ``max_points`` triggers FIFO eviction
    when exceeded, keeping the collection bounded.
    """

    importance_min: int = 0
    importance_max: int = 5
    importance_default: int = 2
    max_age_days: Optional[int] = None
    max_points: Optional[int] = None
    archive_on_expire: bool = True


# ---------------------------------------------------------------------------
# Collection specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """A complete, declarative description of a Qdrant collection.

    Attributes
    ----------
    name:
        Stable collection name; the single source of truth other modules
        reference.
    description:
        Human-readable summary for the developer dashboard (FG5) and logs.
    vector_size:
        Dimensionality of the embedding vectors stored here. MUST match the
        encoder used to populate the collection or upserts will fail.
    distance:
        Distance metric used for similarity search. Stored as a
        :class:`DistanceMetric` (str enum) for serialization friendliness and
        translated to the SDK enum via :meth:`to_vector_params`.
    hnsw:
        HNSW graph index configuration.
    payload_indexes:
        Scalar indexes over payload fields, enabling filtered search
        (e.g. "important memories from the personal zone created after
        2026-01-01").
    retention:
        Memory governance policy attached to the collection.
    enable_quantization:
        When True, the client enables scalar quantization to reduce memory
        footprint at a small recall cost (recommended for large collections).
    on_disk_payload:
        When True, payload (metadata) is stored on disk rather than in RAM.
    """

    name: str
    description: str
    vector_size: int
    distance: DistanceMetric = DistanceMetric.COSINE
    hnsw: HnswConfig = field(default_factory=HnswConfig)
    payload_indexes: Tuple[PayloadIndex, ...] = ()
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    enable_quantization: bool = False
    on_disk_payload: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CollectionSpec.name must be a non-empty string")
        if self.vector_size <= 0:
            raise ValueError(
                f"CollectionSpec '{self.name}' vector_size must be positive "
                f"(got {self.vector_size})"
            )

    def payload_index_fields(self) -> Tuple[str, ...]:
        """Return the tuple of payload field names that are indexed."""
        return tuple(idx.field for idx in self.payload_indexes)

    def to_vector_params(self) -> VectorParams:
        """Build the :class:`VectorParams` for this collection's named vector.

        Returned via the canonical SDK model so the client never needs to
        construct vectors config manually — it simply forwards this object as
        the ``vectors_config`` argument of ``create_collection``.
        """
        return VectorParams(
            size=self.vector_size,
            distance=self.distance.to_qdrant(),
        )

    def to_hnsw_config(self) -> dict[str, Any]:
        """Return the HNSW config dict (mirrors :meth:`HnswConfig.to_qdrant`)."""
        return self.hnsw.to_qdrant()

    def to_optimizers_config(self) -> OptimizersConfigDiff:
        """Return an :class:`OptimizersConfigDiff` tuned for tenant-style
        indexed collections.

        When the spec uses tenant indexes (``is_tenant=True``), indexing is set
        to ``0`` so filters over the tenant field take effect immediately. For
        non-tenant collections the default optimizer config (``None``) is
        preferable, so we still return a diff whose fields the SDK may merge
        with its own defaults; we only set ``indexing_threshold`` when needed.
        """
        has_tenant = any(idx.is_tenant for idx in self.payload_indexes)
        if not has_tenant:
            return OptimizersConfigDiff()
        return OptimizersConfigDiff(indexing_threshold=0)

    def to_quantization_config(self) -> Optional[ScalarQuantization]:
        """Return a :class:`ScalarQuantization` config when enabled, else None.

        Scalar quantization (int8 with a 0.99 quantile kept in RAM) is the
        Qdrant-recommended way to reduce memory footprint for large
        collections at a small recall cost (FG2 knowledge base, FG10 learned
        patterns). Returned as the SDK model so the client passes it through
        unchanged.
        """
        if not self.enable_quantization:
            return None
        try:
            from qdrant_client.http.models import ScalarQuantizationConfig
        except ImportError:  # pragma: no cover - older SDK lacks the model
            return None
        return ScalarQuantization(
            scalar=ScalarQuantizationConfig(type="int8", quantile=0.99, always_ram=True),
        )


# ---------------------------------------------------------------------------
# Canonical collection names (single source of truth)
# ---------------------------------------------------------------------------


class CollectionName(str, Enum):
    """Stable, machine-readable names of every Qdrant collection in AIOS.

    These match the collections referenced in FG2 §19 (Vector Memory) and the
    FG10 learning memory tiers. Renaming a member here is a breaking change;
    add a new member rather than renumbering.
    """

    GENERAL = "general"          # generic semantic memories
    PERSONAL = "personal"          # encrypted personal memory (FG2 §15)
    PROJECTS = "projects"          # project knowledge, codebase context
    KNOWLEDGE = "knowledge"        # assistant knowledge base / docs
    RESEARCH = "research"          # FG9 research agent results
    PRODUCTIVITY = "productivity"  # FG8 productivity memories
    AGENT = "agent"                # FG9 agent long-term memory
    LEARNING = "learning"          # FG10 learned optimizations
    SEARCH_CACHE = "search_cache"  # semantic search result cache


# ---------------------------------------------------------------------------
# Default collection catalog
# ---------------------------------------------------------------------------
#
# The default embedding model across AIOS is all-MiniLM-L6-v2 (384 dims) for
# the general-purpose collections, and multilingual-e5-small (384 dims) for
# the multilingual collections (FG4). Both share the same dimensionality, so
# every default collection uses vector_size=384 and COSINE distance.
#
# These values can be overridden via configuration (configs/app_config.yaml),
# but the catalog here is the validated baseline that the client falls back to.

_GENERAL_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("source", PayloadIndexType.KEYWORD),
    PayloadIndex("importance", PayloadIndexType.INTEGER),
    PayloadIndex("is_active", PayloadIndexType.BOOL),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
    PayloadIndex("expires_at", PayloadIndexType.DATETIME, is_tenant=False),
    PayloadIndex("tags", PayloadIndexType.KEYWORD),
)

_PERSONAL_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("zone", PayloadIndexType.KEYWORD, is_tenant=True),
    PayloadIndex("importance", PayloadIndexType.INTEGER),
    PayloadIndex("confidence", PayloadIndexType.FLOAT),
    PayloadIndex("is_active", PayloadIndexType.BOOL),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
    PayloadIndex("expires_at", PayloadIndexType.DATETIME),
    PayloadIndex("owner", PayloadIndexType.KEYWORD),
)

_PROJECTS_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("project_id", PayloadIndexType.KEYWORD, is_tenant=True),
    PayloadIndex("language", PayloadIndexType.KEYWORD),
    PayloadIndex("importance", PayloadIndexType.INTEGER),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
    PayloadIndex("tags", PayloadIndexType.KEYWORD),
)

_KNOWLEDGE_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("category", PayloadIndexType.KEYWORD, is_tenant=True),
    PayloadIndex("language", PayloadIndexType.KEYWORD),
    PayloadIndex("source", PayloadIndexType.KEYWORD),
    PayloadIndex("summary", PayloadIndexType.TEXT),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
)

_RESEARCH_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("query_hash", PayloadIndexType.KEYWORD),
    PayloadIndex("provider", PayloadIndexType.KEYWORD),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
    PayloadIndex("is_cached", PayloadIndexType.BOOL),
)

_PRODUCTIVITY_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("task_type", PayloadIndexType.KEYWORD, is_tenant=True),
    PayloadIndex("importance", PayloadIndexType.INTEGER),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
    PayloadIndex("completed_at", PayloadIndexType.DATETIME),
)

_AGENT_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("agent_id", PayloadIndexType.KEYWORD, is_tenant=True),
    PayloadIndex("agent_type", PayloadIndexType.KEYWORD),
    PayloadIndex("importance", PayloadIndexType.INTEGER),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
)

_LEARNING_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("engine", PayloadIndexType.KEYWORD, is_tenant=True),
    PayloadIndex("version", PayloadIndexType.INTEGER),
    PayloadIndex("confidence", PayloadIndexType.FLOAT),
    PayloadIndex("is_active", PayloadIndexType.BOOL),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
)

_SEARCH_CACHE_PAYLOAD: Tuple[PayloadIndex, ...] = (
    PayloadIndex("query_hash", PayloadIndexType.KEYWORD),
    PayloadIndex("provider", PayloadIndexType.KEYWORD),
    PayloadIndex("created_at", PayloadIndexType.DATETIME),
    PayloadIndex("ttl_days", PayloadIndexType.INTEGER),
)


_PERMANENT: Final[RetentionPolicy] = RetentionPolicy(
    max_age_days=None,
    max_points=500_000,
    archive_on_expire=True,
)
_TEMPORARY: Final[RetentionPolicy] = RetentionPolicy(
    max_age_days=3,    # FG2 §15 temporary cache
    max_points=100_000,
    archive_on_expire=False,
)


DEFAULT_COLLECTIONS: Final[Tuple[CollectionSpec, ...]] = (
    CollectionSpec(
        name=CollectionName.GENERAL.value,
        description="General-purpose semantic memories for the AI brain.",
        vector_size=384,
        payload_indexes=_GENERAL_PAYLOAD,
        retention=_PERMANENT,
    ),
    CollectionSpec(
        name=CollectionName.PERSONAL.value,
        description="Encrypted personal memory (PII, finance, health, notes).",
        vector_size=384,
        payload_indexes=_PERSONAL_PAYLOAD,
        retention=_PERMANENT,
        on_disk_payload=False,  # keep payload in RAM for fast filtered search
    ),
    CollectionSpec(
        name=CollectionName.PROJECTS.value,
        description="Project knowledge and codebase context embeddings.",
        vector_size=384,
        payload_indexes=_PROJECTS_PAYLOAD,
        retention=_PERMANENT,
    ),
    CollectionSpec(
        name=CollectionName.KNOWLEDGE.value,
        description="Assistant knowledge base: docs, reference, how-tos.",
        vector_size=384,
        payload_indexes=_KNOWLEDGE_PAYLOAD,
        retention=_PERMANENT,
        enable_quantization=True,  # large collection, save RAM
        on_disk_payload=True,
    ),
    CollectionSpec(
        name=CollectionName.RESEARCH.value,
        description="FG9 research agent results and deep-search archives.",
        vector_size=384,
        payload_indexes=_RESEARCH_PAYLOAD,
        retention=RetentionPolicy(max_age_days=30, max_points=200_000),
        on_disk_payload=True,
    ),
    CollectionSpec(
        name=CollectionName.PRODUCTIVITY.value,
        description="FG8 productivity memories (tasks, habits, focus).",
        vector_size=384,
        payload_indexes=_PRODUCTIVITY_PAYLOAD,
        retention=_PERMANENT,
    ),
    CollectionSpec(
        name=CollectionName.AGENT.value,
        description="FG9 persistent agent long-term memory.",
        vector_size=384,
        payload_indexes=_AGENT_PAYLOAD,
        retention=_PERMANENT,
    ),
    CollectionSpec(
        name=CollectionName.LEARNING.value,
        description="FG10 self-learning optimizations and learned patterns.",
        vector_size=384,
        payload_indexes=_LEARNING_PAYLOAD,
        retention=_PERMANENT,
        enable_quantization=True,
    ),
    CollectionSpec(
        name=CollectionName.SEARCH_CACHE.value,
        description="Semantic search-result cache (3-day TTL).",
        vector_size=384,
        payload_indexes=_SEARCH_CACHE_PAYLOAD,
        retention=_TEMPORARY,
        on_disk_payload=True,
    ),
)


# Frozen registry for O(1) lookup by name.
_COLLECTION_MAP: Final[Mapping[str, CollectionSpec]] = MappingProxyType(
    {spec.name: spec for spec in DEFAULT_COLLECTIONS}
)

COLLECTION_NAMES: Final[Tuple[str, ...]] = tuple(
    spec.name for spec in DEFAULT_COLLECTIONS
)


# ---------------------------------------------------------------------------
# Public lookup helpers
# ---------------------------------------------------------------------------


def get_collection_spec(name: str) -> CollectionSpec:
    """Return the :class:`CollectionSpec` registered under ``name``.

    Raises
    ------
    KeyError
        If ``name`` is not in :data:`DEFAULT_COLLECTIONS`.
    """
    spec = _COLLECTION_MAP.get(name)
    if spec is None:
        raise KeyError(
            f"Unknown Qdrant collection: '{name}'. "
            f"Known collections: {sorted(COLLECTION_NAMES)}"
        )
    return spec


def is_known_collection(name: str) -> bool:
    """Return True if ``name`` is one of the canonical collections."""
    return name in _COLLECTION_MAP


def to_dict(spec: CollectionSpec) -> dict[str, Any]:
    """Serialize a :class:`CollectionSpec` to a plain dict.

    Useful for logging, the developer dashboard, and config persistence. The
    returned dict is a fresh copy each call and is safe to mutate.
    """
    return {
        "name": spec.name,
        "description": spec.description,
        "vector_size": spec.vector_size,
        "distance": spec.distance.value,
        "hnsw": spec.hnsw.to_qdrant(),
        "payload_indexes": [
            {
                "field": idx.field,
                "type": idx.index_type.value,
                "is_tenant": idx.is_tenant,
            }
            for idx in spec.payload_indexes
        ],
        "retention": {
            "importance_min": spec.retention.importance_min,
            "importance_max": spec.retention.importance_max,
            "importance_default": spec.retention.importance_default,
            "max_age_days": spec.retention.max_age_days,
            "max_points": spec.retention.max_points,
            "archive_on_expire": spec.retention.archive_on_expire,
        },
        "enable_quantization": spec.enable_quantization,
        "on_disk_payload": spec.on_disk_payload,
    }
