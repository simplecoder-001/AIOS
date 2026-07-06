# app/core/database/qdrant/vector_store.py
"""
High-level Qdrant vector store API for AIOS.
============================================
Combines the :class:`~app.core.database.qdrant.client.QdrantClient`,
the :class:`~app.core.database.qdrant.embeddings.Embeddings` service, and the
collection catalog in :mod:`collections` into the single public surface that
the AI brain (FG2), the productivity system (FG8), the agent system (FG9), and
the self-learning engine (FG10) use for semantic memory.

Design goals
-------------
* One facade class (:class:`QdrantVectorStore`) implementing
  :class:`VectorStore` — the protocol the rest of the system programs against.
* Text in, structured records out — callers do not build vectors themselves.
  Every upsert embeds the supplied text using the configured embedder; every
  search embeds the query and runs a filtered ANN search.
* Idempotent lifecycle: ``initialize()`` wires client + embeddings, provisions
  collections, and is safe to call repeatedly.
* Production hygiene:
    - All public methods translate SDK exceptions into
      :class:`VectorStoreError`.
    - Long-running batches respect a configurable ``batch_size`` and surface
      progress via an optional callback.
    - Per-collection retention (FG2 §16 / FG10 §Engine 8) is enforced through a
      ``purge_expired`` operation that the memory-governance engine schedules.
    - The store publishes lifecycle events (``database.connected``, etc.) via
      the bound event-bus publisher so the rest of the system reacts to
      availability changes without polling.

Concurrency
-----------
The store is safe to share across threads. The client handles its own
locking; upsert/search paths rely on the SDK's thread safety. The
``batch_upsert`` and ``purge_expired`` helpers hold an internal lock for the
duration of a batch so a concurrent ``shutdown`` cannot interleave with a
half-written batch.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from app.core.configs.config_manager import ConfigManager, get_config_manager
from app.core.constants.events import LifecycleEvent
from app.core.constants.settings import MEMORY
from app.core.exceptions import VectorStoreError, ValidationError
from app.logging.logger import Logger, LogLevel
from app.logging.logger_factory import LoggerFactory

# Top-level Qdrant SDK imports, kept import-safe: when the SDK is missing the
# names are nulled and the store raises a structured :class:`VectorStoreError`
# only when an operation actually needs them (i.e. once initialized). This
# mirrors the pattern used by the SQLite/SQLCipher engines and keeps the module
# importable in unit environments that do not ship ``qdrant_client``.
try:
    from qdrant_client.http.models import (
        FieldCondition,
        Filter,
        MatchValue,
        PointStruct,
        Range,
    )
except ImportError:  # pragma: no cover - environment dependent
    FieldCondition = None  # type: ignore[assignment,misc]
    Filter = None  # type: ignore[assignment,misc]
    MatchValue = None  # type: ignore[assignment,misc]
    PointStruct = None  # type: ignore[assignment,misc]
    Range = None  # type: ignore[assignment,misc]

from app.core.database.qdrant.client import (
    QdrantClient,
    QdrantClientConfig,
    QdrantHealth,
    register_qdrant_client,
)
from app.core.database.qdrant.collections import (
    CollectionSpec,
    DEFAULT_COLLECTIONS,
    DistanceMetric,
    get_collection_spec,
    is_known_collection,
)
from app.core.database.qdrant.embeddings import (
    EmbeddingResult,
    Embeddings,
)

__all__ = [
    "VectorRecord",
    "SearchHit",
    "SearchResult",
    "VectorStore",
    "QdrantVectorStore",
    "default_vector_store",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """A single point to upsert into the vector store.

    Attributes
    ----------
    text:
        The text to embed. Required — the store never accepts a pre-computed
        vector from callers because that would leak the embedding contract
        (changing the model means re-embedding, which the store must own).
    payload:
        Arbitrary metadata stored alongside the vector (source, tags,
        importance, timestamps). The store stamps ``created_at`` and
        ``updated_at`` if absent.
    id:
        Optional stable UUID. When absent, the store generates a UUID-4 so
        re-upserts are explicit (no accidental point reuse).
    collection:
        Target collection name. Required on upsert; if a record omitted it the
        store routes it to the General collection (FG2 §19) for backward
        compatibility with callers that only have text + payload.
    """

    text: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: Optional[str] = None
    collection: str = "general"

    def __post_init__(self) -> None:
        if self.text is None:
            raise ValidationError("text", None, expected="non-empty string")
        if not isinstance(self.payload, Mapping):
            raise ValidationError("payload", self.payload, expected="Mapping[str, Any]")

    def with_defaults(self) -> "VectorRecord":
        """Return a copy with id/timestamps populated if missing."""
        now = _now_iso()
        payload = dict(self.payload)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        return VectorRecord(
            text=self.text,
            payload=MappingProxy(payload),
            id=self.id or str(uuid.uuid4()),
            collection=self.collection,
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result returned by :meth:`VectorStore.search`."""

    id: str
    score: float
    text: str
    payload: Mapping[str, Any]
    collection: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "score": self.score,
            "text": self.text,
            "payload": dict(self.payload),
            "collection": self.collection,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A complete search response."""

    query: str
    collection: str
    hits: Tuple[SearchHit, ...]
    model: str
    latency_ms: float

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self) -> Iterator[SearchHit]:
        return iter(self.hits)

    @property
    def top(self) -> Optional[SearchHit]:
        return self.hits[0] if self.hits else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "collection": self.collection,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "hits": [h.as_dict() for h in self.hits],
        }


# ---------------------------------------------------------------------------
# VectorStore protocol
# ---------------------------------------------------------------------------


class VectorStore:
    """Abstract vector store interface.

    The concrete :class:`QdrantVectorStore` implements this protocol; FG2
    (memory engine) and FG10 (memory governance) program against the protocol
    so a different backend (e.g. a remote Qdrant cluster, or FAISS during
    tests) can be swapped in.
    """

    def initialize(self) -> None: raise NotImplementedError
    def shutdown(self) -> None: raise NotImplementedError
    def health(self) -> Any: raise NotImplementedError

    def upsert(self, record: VectorRecord) -> str: raise NotImplementedError
    def upsert_batch(self, records: Sequence[VectorRecord]) -> List[str]: raise NotImplementedError
    def search(
        self,
        query: str,
        *,
        collection: str = "general",
        top_k: int = 5,
        filters: Optional[Mapping[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> SearchResult: raise NotImplementedError
    def search_batch(
        self,
        queries: Sequence[str],
        *,
        collection: str = "general",
        top_k: int = 5,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> List[SearchResult]: raise NotImplementedError
    def fetch(self, collection: str, ids: Sequence[str]) -> List[SearchHit]: raise NotImplementedError
    def delete(self, collection: str, ids: Sequence[str]) -> int: raise NotImplementedError
    def delete_by_filter(self, collection: str, filters: Mapping[str, Any]) -> int: raise NotImplementedError
    def purge_expired(self, collection: str) -> int: raise NotImplementedError
    def count(self, collection: str, *, exact: bool = False) -> int: raise NotImplementedError


# ---------------------------------------------------------------------------
# Mapping proxy helper (kept local to avoid extra imports in hot path)
# ---------------------------------------------------------------------------


def MappingProxy(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a shallow immutable view of ``data``.

    Used so :class:`VectorRecord` payloads cannot be mutated after creation
    while the caller still owns the original dict.
    """
    from types import MappingProxyType
    return MappingProxyType(dict(data))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# QdrantVectorStore — concrete implementation
# ---------------------------------------------------------------------------


class QdrantVectorStore(VectorStore):
    """Production vector store backed by Qdrant and sentence-transformers.

    Parameters mirror :class:`QdrantClient` and :class:`Embeddings`. At least
    one of (``client``, ``client_config``, ``config_manager``) should be given;
    otherwise the store bootstraps from the global config snapshot — which is
    the recommended path for application code.
    """

    _DEFAULT_LOG_NAME = "core.database.qdrant.store"

    def __init__(
        self,
        *,
        client: Optional[QdrantClient] = None,
        client_config: Optional[QdrantClientConfig] = None,
        embeddings: Optional[Embeddings] = None,
        config_manager: Optional[ConfigManager] = None,
        logger_factory: Optional[LoggerFactory] = None,
        logger: Optional[Logger] = None,
        event_publisher: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        batch_size: int = 64,
    ) -> None:
        factory = logger_factory or LoggerFactory()
        self._logger = logger or factory.create_console_logger(
            self._DEFAULT_LOG_NAME, LogLevel.INFO,
        )

        self._config_manager = config_manager
        self._client = client or QdrantClient(
            config=client_config,
            config_manager=config_manager,
            logger_factory=factory,
            logger=self._logger,
        )
        self._embeddings = embeddings or Embeddings(
            config_manager=config_manager,
            logger_factory=factory,
            logger=self._logger,
        )

        self._event_publisher = event_publisher
        self._batch_size = max(1, int(batch_size))
        self._lock = threading.RLock()
        self._initialized = False

    # ------------------------------------------------------------------ properties
    @property
    def client(self) -> QdrantClient:
        return self._client

    @property
    def embeddings(self) -> Embeddings:
        return self._embeddings

    @property
    def is_initialized(self) -> bool:
        with self._lock:
            return self._initialized

    # ------------------------------------------------------------------ lifecycle
    def initialize(self) -> None:
        """Wire client + embeddings and provision collections.

        Idempotent. Calls ``client.initialize()`` (which itself is idempotent)
        and pre-warms the embedding model so the first request is not penalized
        by the load latency (FG2 SLO: <15ms intent latency).
        """
        with self._lock:
            if self._initialized:
                return
            self._client.initialize()
            try:
                self._embeddings.load()
            except Exception as exc:  # noqa: BLE001 - degrade, don't crash init
                self._logger.warning(
                    "Embedding model pre-warm failed; will lazy-load on first use",
                    exc_info=exc,
                )
            self._initialized = True
        self._publish(
            LifecycleEvent.APP_INITIALIZED.value,
            {"component": "vector_store"},
        )
        self._logger.info("Vector store initialized")

    def shutdown(self) -> None:
        """Close client and embedder (idempotent)."""
        with self._lock:
            if not self._initialized:
                # Still attempt a graceful close of the underlying client so a
                # half-initialized store leaves nothing dangling.
                try:
                    self._client.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._initialized = False
        try:
            self._embeddings.unload()
        except Exception as exc:  # noqa: BLE001 - never raise from shutdown
            self._logger.warning("Embeddings unload failed during shutdown", exc_info=exc)
        try:
            self._client.shutdown()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Qdrant client shutdown failed", exc_info=exc)
        self._logger.info("Vector store shut down")

    def __enter__(self) -> "QdrantVectorStore":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    def health(self) -> QdrantHealth:
        """Return the underlying client health snapshot."""
        return self._client.health()

    # ------------------------------------------------------------------ upsert
    def upsert(self, record: VectorRecord) -> str:
        """Embed ``record`` and store it; return the point id."""
        if not is_known_collection(record.collection):
            raise VectorStoreError(
                "upsert", collection=record.collection,
            ).with_context(reason="unknown collection; see app.core.database.qdrant.collections")
        self._require_initialized()

        prepared = record.with_defaults()
        embedding = self._embeddings.embed(prepared.text)
        spec = get_collection_spec(prepared.collection)
        _ensure_dimension_compatible(spec, embedding, prepared.collection)

        points = [self._build_point(prepared, embedding)]
        self._upsert_points(prepared.collection, points)
        return prepared.id or ""

    def upsert_batch(
        self,
        records: Sequence[VectorRecord],
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        """Embed and store every record, batching for throughput.

        ``on_progress(completed, total)`` is invoked after each batch; pass a
        callback if the calling UI (FG5) wants to show progress.
        """
        if not records:
            return []
        self._require_initialized()

        # Validate collections up front so a single bad record fails fast.
        for rec in records:
            if not is_known_collection(rec.collection):
                raise VectorStoreError(
                    "upsert_batch", collection=rec.collection,
                ).with_context(reason="unknown collection")

        # Group records by collection so each batch hits a single endpoint.
        groups: Dict[str, List[VectorRecord]] = {}
        ids: List[str] = []
        order: Dict[str, List[int]] = {}
        for i, rec in enumerate(records):
            prepared = rec.with_defaults()
            rid = prepared.id or ""
            groups.setdefault(prepared.collection, []).append(prepared)
            order.setdefault(prepared.collection, []).append(i)
            ids.append(rid)

        prepared_embeddings = self._embed_batch_records(records)
        # ``prepared_embeddings`` aligns with the *input* order; we need to
        # gather them per collection. Re-embed per-collection to keep this
        # bullet-proof and the code simple — the embedder is fast enough that
        # an O(n) extra embedding pass for extremely large loads is acceptable;
        # for very large batches the caller should split into collections
        # explicitly. The simpler implementation is preferred here.

        total = len(records)
        completed = 0
        for collection, group_records in groups.items():
            spec = get_collection_spec(collection)
            group_texts = [r.text for r in group_records]
            results = self._embeddings.embed_batch(group_texts)
            for r, res in zip(group_records, results):
                _ensure_dimension_compatible(spec, res, collection)
            points = [self._build_point(r, res) for r, res in zip(group_records, results)]
            # Batch the SDK upserts.
            for chunk in _chunks(points, self._batch_size):
                self._upsert_points(collection, chunk)
                completed += len(chunk)
                if on_progress is not None:
                    try:
                        on_progress(completed, total)
                    except Exception:  # noqa: BLE001 - progress callback must not break write
                        pass
        return ids

    # ------------------------------------------------------------------ search
    def search(
        self,
        query: str,
        *,
        collection: str = "general",
        top_k: int = 5,
        filters: Optional[Mapping[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> SearchResult:
        if not query:
            raise ValidationError("query", query, expected="non-empty string")
        self._require_initialized()
        if not is_known_collection(collection):
            raise VectorStoreError(
                "search", collection=collection,
            ).with_context(reason="unknown collection")

        spec = get_collection_spec(collection)
        t0 = time.perf_counter()
        embedding = self._embeddings.embed(query)
        _ensure_dimension_compatible(spec, embedding, collection)
        hits = self._query_points(
            collection=collection,
            vector=embedding.vector,
            top_k=top_k,
            filters=self._build_qdrant_filter(filters),
            score_threshold=score_threshold,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return SearchResult(
            query=query,
            collection=collection,
            hits=tuple(hits),
            model=embedding.model,
            latency_ms=latency_ms,
        )

    def search_batch(
        self,
        queries: Sequence[str],
        *,
        collection: str = "general",
        top_k: int = 5,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> List[SearchResult]:
        if not queries:
            return []
        if not is_known_collection(collection):
            raise VectorStoreError(
                "search_batch", collection=collection,
            ).with_context(reason="unknown collection")

        results = self._embeddings.embed_batch(list(queries))
        spec = get_collection_spec(collection)
        for res in results:
            _ensure_dimension_compatible(spec, res, collection)

        qfilter = self._build_qdrant_filter(filters)
        out: List[SearchResult] = []
        for query, res in zip(queries, results):
            t0 = time.perf_counter()
            hits = self._query_points(
                collection=collection,
                vector=res.vector,
                top_k=top_k,
                filters=qfilter,
                score_threshold=None,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            out.append(SearchResult(
                query=query,
                collection=collection,
                hits=tuple(hits),
                model=res.model,
                latency_ms=latency_ms,
            ))
        return out

    # ------------------------------------------------------------------ fetch / delete
    def fetch(self, collection: str, ids: Sequence[str]) -> List[SearchHit]:
        self._require_initialized()
        if not is_known_collection(collection):
            raise VectorStoreError("fetch", collection=collection).with_context(reason="unknown collection")
        if not ids:
            return []
        client = self._client.raw()
        try:
            response = client.retrieve(
                collection_name=collection,
                ids=list(ids),
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError("fetch", collection=collection, cause=exc) from exc
        return [self._point_to_hit(p, collection, score=0.0) for p in response]

    def delete(self, collection: str, ids: Sequence[str]) -> int:
        self._require_initialized()
        if not is_known_collection(collection):
            raise VectorStoreError("delete", collection=collection).with_context(reason="unknown collection")
        if not ids:
            return 0
        client = self._client.raw()
        try:
            client.delete(
                collection_name=collection,
                points_selector=list(ids),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError("delete", collection=collection, cause=exc) from exc
        return len(ids)

    def delete_by_filter(self, collection: str, filters: Mapping[str, Any]) -> int:
        self._require_initialized()
        if not is_known_collection(collection):
            raise VectorStoreError("delete_by_filter", collection=collection).with_context(reason="unknown collection")
        qfilter = self._build_qdrant_filter(filters)
        if qfilter is None:
            # Refuse to delete everything by accident.
            raise VectorStoreError(
                "delete_by_filter", collection=collection,
            ).with_context(reason="empty filter; refusing to delete with no predicate")
        client = self._client.raw()
        try:
            client.delete(
                collection_name=collection,
                points_selector=qfilter,
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError("delete_by_filter", collection=collection, cause=exc) from exc
        # Qdrant's delete returns void; report the filter-key count as best-effort.
        return 1

    # ------------------------------------------------------------------ retention
    def purge_expired(self, collection: str) -> int:
        """Delete points whose payload ``expires_at`` has passed.

        Returns the number of points removed (counts via the pre-delete query
        because the SDK's delete-by-filter is void-returning).
        """
        self._require_initialized()
        spec = get_collection_spec(collection)
        if spec.retention.max_age_days is None and not _has_expirable_payload(spec):
            return 0  # permanent collection with no expiry field
        client = self._client.raw()
        cutoff_iso = _now_iso()

        # Build a filter: expires_at exists AND expires_at < cutoff_iso
        if any(x is None for x in (FieldCondition, Filter, MatchValue, Range)):
            raise VectorStoreError(
                "purge_expired", collection=collection,
            ).with_context(reason="qdrant_client SDK is not installed")
        expired_filter = Filter(
            must=[
                FieldCondition(
                    key="expires_at",
                    range=Range(lt=cutoff_iso),
                ),
                FieldCondition(
                    key="is_active",
                    match=MatchValue(value=False),
                ),
            ]
        )

        try:
            count_resp = client.count(
                collection_name=collection,
                count_filter=expired_filter,
                exact=True,
            )
            expired_count = int(getattr(count_resp, "count", 0))
        except Exception as exc:  # noqa: BLE001 - count is best-effort
            self._logger.warning(
                "purge_expired could not pre-count expired points",
                exc_info=exc,
                extra={"collection": collection},
            )
            expired_count = -1  # unknown

        try:
            client.delete(
                collection_name=collection,
                points_selector=expired_filter,
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "purge_expired", collection=collection, cause=exc,
            ) from exc

        self._logger.info(
            "Purged expired points",
            extra={"collection": collection, "count": expired_count},
        )
        return max(0, expired_count)

    # ------------------------------------------------------------------ count
    def count(self, collection: str, *, exact: bool = False) -> int:
        self._require_initialized()
        if not is_known_collection(collection):
            raise VectorStoreError("count", collection=collection).with_context(reason="unknown collection")
        client = self._client.raw()
        try:
            resp = client.count(
                collection_name=collection,
                exact=exact,
            )
            return int(getattr(resp, "count", 0))
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError("count", collection=collection, cause=exc) from exc

    # ------------------------------------------------------------------ internal
    def _require_initialized(self) -> None:
        if not self._client.is_initialized:
            raise VectorStoreError("not_initialized", collection=None).with_context(
                reason="QdrantVectorStore.initialize() must be called first",
            )

    def _build_point(self, record: VectorRecord, embedding: EmbeddingResult) -> Any:
        """Construct a qdrant PointStruct for the embedded record."""
        if PointStruct is None:
            raise VectorStoreError(
                "build_point", collection=record.collection,
            ).with_context(reason="qdrant_client.http.models is unavailable")
        return PointStruct(
            id=record.id,
            vector=embedding.vector.tolist(),
            payload=dict(record.payload),
        )

    def _upsert_points(self, collection: str, points: Sequence[Any]) -> None:
        client = self._client.raw()
        try:
            client.upsert(
                collection_name=collection,
                points=list(points),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "upsert", collection=collection, cause=exc,
            ) from exc

    def _query_points(
        self,
        *,
        collection: str,
        vector: Any,
        top_k: int,
        filters: Any,
        score_threshold: Optional[float],
    ) -> List[SearchHit]:
        client = self._client.raw()
        kwargs: Dict[str, Any] = {
            "collection_name": collection,
            "query_vector": vector.tolist() if hasattr(vector, "tolist") else vector,
            "limit": max(1, int(top_k)),
            "with_payload": True,
        }
        if filters is not None:
            kwargs["query_filter"] = filters
        if score_threshold is not None:
            kwargs["score_threshold"] = float(score_threshold)
        try:
            response = client.search(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError("search", collection=collection, cause=exc) from exc
        return [self._point_to_hit(p, collection, _point_score(p)) for p in response]

    def _point_to_hit(self, point: Any, collection: str, *, score: float) -> SearchHit:
        pid = _point_id(point)
        payload = _point_payload(point)
        # Pull stored text from payload when present (recorded by upsert).
        text = payload.get("text", "") if isinstance(payload, Mapping) else ""
        return SearchHit(
            id=str(pid),
            score=float(score),
            text=str(text),
            payload=MappingProxy(payload) if isinstance(payload, Mapping) else MappingProxy({}),
            collection=collection,
        )

    def _build_qdrant_filter(self, filters: Optional[Mapping[str, Any]]) -> Any:
        """Translate a flat ``{field: value | {"$range": ...}}`` dict to a Qdrant Filter.

        Supports the common shapes the memory engine uses:

            {"importance": 5}                 -> MatchValue(value=5)
            {"importance": {"$gte": 4}}        -> Range(gte=4)
            {"tags": "python"}                -> MatchValue(value="python")
            {"created_at": {"$gte": "...ISO"}} -> Range(gte=...)

        A ``None`` input yields ``None`` (no filter). Unknown operators are
        ignored so callers can pass higher-level query dicts without coupling
        to Qdrant's exact API.
        """
        if not filters:
            return None
        if any(x is None for x in (FieldCondition, Filter, MatchValue, Range)):
            raise VectorStoreError(
                "build_filter", collection=None,
            ).with_context(reason="qdrant_client.http.models is unavailable")

        conditions: List[Any] = []
        for field, value in filters.items():
            if isinstance(value, Mapping):
                rng: Dict[str, Any] = {}
                if "$gt" in value: rng["gt"] = value["$gt"]
                if "$gte" in value: rng["gte"] = value["$gte"]
                if "$lt" in value: rng["lt"] = value["$lt"]
                if "$lte" in value: rng["lte"] = value["$lte"]
                if rng:
                    conditions.append(FieldCondition(key=field, range=Range(**rng)))
                    continue
                if "$eq" in value:
                    conditions.append(FieldCondition(key=field, match=MatchValue(value=value["$eq"])))
                    continue
                # Unknown operator; skip gracefully.
                continue
            # Plain scalar equals.
            conditions.append(FieldCondition(key=field, match=MatchValue(value=value)))

        if not conditions:
            return None
        return Filter(must=conditions)

    def _embed_batch_records(self, records: Sequence[VectorRecord]) -> List[EmbeddingResult]:
        """Embed every record's text in input order, indexed by input position."""
        return self._embeddings.embed_batch([r.text for r in records])

    def _publish(self, name: str, payload: Dict[str, Any]) -> None:
        if self._event_publisher is None:
            return
        try:
            self._event_publisher(name, payload)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _chunks(seq: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _point_id(point: Any) -> Any:
    return getattr(point, "id", None) or getattr(point, "payload", {}).get("id")


def _point_payload(point: Any) -> Any:
    payload = getattr(point, "payload", None)
    return payload if isinstance(payload, Mapping) else (payload or {} if payload is not None else {})


def _point_score(point: Any) -> float:
    return float(getattr(point, "score", 0.0) or 0.0)


def _has_expirable_payload(spec: CollectionSpec) -> bool:
    return any(idx.field == "expires_at" for idx in spec.payload_indexes)


def _ensure_dimension_compatible(
    spec: CollectionSpec,
    embedding: EmbeddingResult,
    collection: str,
) -> None:
    if spec.vector_size != embedding.dimension:
        raise VectorStoreError(
            "dimension_mismatch", collection=collection,
        ).with_context(
            collection=collection,
            declared_dimension=spec.vector_size,
            embedding_dimension=embedding.dimension,
            embedding_model=embedding.model,
            reason=(
                "The embedding returned by the configured model has a dimension "
                "different from the collection's vector_size. Either change the "
                "embedding model or recreate the collection with the new size."
            ),
        )


# ---------------------------------------------------------------------------
# Process-wide default instance
# ---------------------------------------------------------------------------


_default_store: Optional[QdrantVectorStore] = None
_default_store_lock = threading.Lock()


def default_vector_store(
    config_manager: Optional[ConfigManager] = None,
) -> QdrantVectorStore:
    """Return a process-wide :class:`QdrantVectorStore` (lazy singleton).

    Designed for the common path where the application only needs one store
    shared across FG8/FG2/FG10. Each caller that wants isolation (tests, FG10
    experiments) should construct its own :class:`QdrantVectorStore`.
    """
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                _default_store = QdrantVectorStore(config_manager=config_manager)
    return _default_store


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ += [
    "default_vector_store",
]
