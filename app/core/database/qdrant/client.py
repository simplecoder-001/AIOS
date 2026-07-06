# app/core/database/qdrant/client.py
"""
Qdrant client wrapper and lifecycle manager.
=============================================
Wraps the upstream ``qdrant_client.QdrantClient`` so that the rest of AIOS
deals with a single, opinionated, thread-safe service instead of raw SDK
calls. Responsibilities:

* Build and configure the underlying :class:`QdrantClient` (local embedded
  mode by default — the AIOS vector memory lives entirely on-disk under
  ``data/memory/qdrant`` per FG2 §19 / FG6 secure memory isolation).
* Lifecycle: ``initialize()`` → ``healthy()`` → ``shutdown()`` with idempotent
  transitions guarded by a state flag and a reentrant lock.
* Provisioning: create every collection in
  :mod:`app.core.database.qdrant.collections` if it does not already exist.
* Health: a lightweight ``ping()`` returning a structured
  :class:`QdrantHealth` snapshot for the database health manager.
* Event publication on lifecycle transitions (database.connected /
  disconnected / health.failed) per the FG-wide event contract.
* DI registration helper that registers the client (and a default
  :class:`QdrantVectorStore`) into the application container.

The wrapper never exposes the raw client through ``__init__`` constants; it is
obtainable via :meth:`raw` for the rare case a caller needs SDK-specific
operations (e.g. snapshot creation). All public methods translate upstream
exceptions into the AIOS :class:`VectorStoreError` so callers don't need to
import the SDK's exception hierarchy.

Concurrency
----------
Qdrant's python client is safe to share across threads; the wrapper adds a
reentrant lock only around mutate-state operations (init/shutdown/provision)
to guarantee idempotency. Hot paths (search/upsert) rely on the SDK's own
thread safety and take no wrapper lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from app.core.constants.paths import QDRANT_DIR
from app.core.configs.config_manager import ConfigManager, get_config_manager
from app.core.event_bus import EventBus, EventPriority
from app.core.constants.events import LifecycleEvent, SystemEvent
from app.core.exceptions import VectorStoreError
from app.logging.logger_factory import LoggerFactory
from app.logging.logger import Logger, LogLevel

from app.core.database.qdrant.collections import (
    COLLECTION_NAMES,
    CollectionSpec,
    DEFAULT_COLLECTIONS,
    DistanceMetric,
    PayloadIndex,
    PayloadIndexType,
    get_collection_spec,
    is_known_collection,
    to_dict as spec_to_dict,
)

__all__ = [
    "QdrantHealth",
    "QdrantHealthStatus",
    "QdrantClientConfig",
    "QdrantClient",
    "register_qdrant_client",
]


# ---------------------------------------------------------------------------
# Health snapshot
# ---------------------------------------------------------------------------


class QdrantHealthStatus(str, Enum):
    """Health states reported by :meth:`QdrantClient.health`."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"     # responding but a collection is missing/failed
    UNREACHABLE = "unreachable"
    NOT_INITIALIZED = "not_initialized"


@dataclass(frozen=True, slots=True)
class QdrantHealth:
    """Structured health snapshot returned by the client.

    Designed to be JSON-serializable so the database health manager and the
    developer dashboard (FG5) can render it directly.
    """

    status: QdrantHealthStatus
    collections_total: int = 0
    collections_ready: int = 0
    missing_collections: Tuple[str, ...] = ()
    latency_ms: Optional[float] = None
    detail: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "collections_total": self.collections_total,
            "collections_ready": self.collections_ready,
            "missing_collections": list(self.missing_collections),
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QdrantClientConfig:
    """Configuration for the embedded Qdrant client.

    Defaults read from ``configs/app_config.yaml`` with the keys:
    ``qdrant.path``, ``qdrant.host``, ``qdrant.port``, ``qdrant.api_key``,
    ``qdrant.timeout_seconds``, ``qdrant.prefer_grpc``,
    ``qdrant.auto_provision``. Anything missing falls back to local embedded
    mode using :data:`app.core.constants.paths.QDRANT_DIR`.
    """

    mode: str = "local"                     # "local" (embedded) | "server"
    path: Optional[str] = str(QDRANT_DIR)   # on-disk storage path
    host: Optional[str] = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    api_key: Optional[str] = None
    prefer_grpc: bool = False
    timeout_seconds: int = 10
    auto_provision: bool = True             # create collections on init
    collections: Tuple[CollectionSpec, ...] = DEFAULT_COLLECTIONS

    @classmethod
    def from_config(
        cls,
        config: Optional[ConfigManager] = None,
        collections: Optional[Sequence[CollectionSpec]] = None,
    ) -> "QdrantClientConfig":
        """Build a :class:`QdrantClientConfig` from the config snapshot.

        Missing keys fall back to the dataclass defaults. The signature follows
        the same pattern used by the SQLite/SQLCipher engines: a single
        ``from_config`` classmethod that other subsystems can call.
        """
        cfg = config or get_config_manager()

        mode = cfg.get_str("qdrant.mode", "local") or "local"
        path = cfg.get_str("qdrant.path", str(QDRANT_DIR))
        host = cfg.get_str("qdrant.host", "localhost")
        port = int(cfg.get_int("qdrant.port", 6333) or 6333)
        grpc_port = int(cfg.get_int("qdrant.grpc_port", 6334) or 6334)
        api_key = cfg.get_str("qdrant.api_key", None)
        prefer_grpc = bool(cfg.get_bool("qdrant.prefer_grpc", False))
        timeout_seconds = int(cfg.get_int("qdrant.timeout_seconds", 10) or 10)
        auto_provision = bool(cfg.get_bool("qdrant.auto_provision", True))

        spec_tuple: Tuple[CollectionSpec, ...]
        if collections is None:
            spec_tuple = DEFAULT_COLLECTIONS
        else:
            spec_tuple = tuple(collections)

        return cls(
            mode=mode,
            path=path,
            host=host,
            port=port,
            grpc_port=grpc_port,
            api_key=api_key,
            prefer_grpc=prefer_grpc,
            timeout_seconds=timeout_seconds,
            auto_provision=auto_provision,
            collections=spec_tuple,
        )


# ---------------------------------------------------------------------------
# QdrantClient wrapper
# ---------------------------------------------------------------------------


class QdrantClient:
    """Thread-safe AIOS wrapper around the upstream Qdrant SDK client.

    The wrapper owns exactly one underlying SDK client instance. Once
    :meth:`initialize` has succeeded, all search/upsert operations flow
    through this instance; once :meth:`shutdown` is called the wrapper
    refuses further operations until re-initialized.
    """

    _DEFAULT_LOG_NAME = "core.database.qdrant"

    def __init__(
        self,
        *,
        config: Optional[QdrantClientConfig] = None,
        config_manager: Optional[ConfigManager] = None,
        logger_factory: Optional[LoggerFactory] = None,
        logger: Optional[Logger] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self._config_manager = config_manager
        self._config = config or QdrantClientConfig.from_config(config_manager)

        factory = logger_factory or LoggerFactory()
        self._logger = logger or factory.create_composite_logger(
            name=self._DEFAULT_LOG_NAME,
            file_path=self._log_path(factory),
            level=LogLevel.INFO,
        )
        self._event_bus = event_bus
        self._publisher = event_bus.publisher("core.database.qdrant") if event_bus else None

        self._lock = threading.RLock()
        self._client: Any = None   # qdrant_client.QdrantClient instance
        self._initialized = False
        self._closed = False

    # ------------------------------------------------------------------ properties
    @property
    def config(self) -> QdrantClientConfig:
        return self._config

    @property
    def is_initialized(self) -> bool:
        with self._lock:
            return self._initialized

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def raw(self) -> Any:
        """Return the wrapped SDK client (or raise if uninitialized).

        Use only for SDK features the wrapper does not surface directly.
        """
        with self._lock:
            if self._client is None or not self._initialized:
                raise VectorStoreError(
                    "raw()", collection=None, cause=None,
                ).with_context(reason="qdrant client not initialized")
            return self._client

    # ------------------------------------------------------------------ lifecycle
    def initialize(self) -> None:
        """Create the underlying client and provision collections.

        Idempotent: calling twice is a no-op after the first success. If a
        previous shutdown happened, ``initialize`` re-opens a fresh client.
        """
        with self._lock:
            if self._initialized and not self._closed:
                return
            if self._closed:
                # Allow re-init after a previous close; reset the flag.
                self._closed = False

            try:
                self._client = self._build_underlying_client()
            except Exception as exc:  # noqa: BLE001 - normalize to VectorStoreError
                self._logger.error("Failed to create Qdrant client", exc_info=exc)
                self._publish(
                    SystemEvent.HEALTH_DEGRADED.value,
                    payload={"component": "qdrant", "reason": "init_failed"},
                )
                raise VectorStoreError(
                    "initialize", collection=None, cause=exc,
                ) from exc

            self._initialized = True
            self._logger.info(
                "Qdrant client initialized",
                extra={"mode": self._config.mode},
            )

        # Provision outside the write lock to avoid holding it during the
        # (potentially slow) collection creation loop.
        if self._config.auto_provision:
            try:
                self.provision_collections(self._config.collections)
            except VectorStoreError:
                # Provisioning failure is non-fatal at init: the client is
                # usable and missing collections can be created lazily. Log and
                # emit a degraded-health event.
                self._logger.warning("Auto-provisioning incomplete; client is degraded")
                self._publish(
                    SystemEvent.HEALTH_DEGRADED.value,
                    payload={"component": "qdrant", "reason": "provisioning_partial"},
                )

        self._publish(
            LifecycleEvent.APP_INITIALIZED.value,
            payload={"component": "qdrant", "mode": self._config.mode},
        )
        self._publish(
            "database.connected",
            payload={"backend": "qdrant", "mode": self._config.mode},
        )

    def shutdown(self) -> None:
        """Close the underlying client and release resources. Idempotent."""
        with self._lock:
            if self._closed:
                return
            client = self._client
            self._client = None
            self._initialized = False
            self._closed = True

        if client is not None:
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except Exception as exc:  # noqa: BLE001 - never raise from shutdown
                self._logger.warning("Error closing Qdrant client", exc_info=exc)

        self._logger.info("Qdrant client shut down")
        self._publish(
            "database.disconnected",
            payload={"backend": "qdrant"},
        )

    def __enter__(self) -> "QdrantClient":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ provisioning
    def provision_collections(
        self,
        collections: Sequence[CollectionSpec],
        *,
        recreate: bool = False,
    ) -> List[str]:
        """Create every collection in ``collections`` that does not exist.

        Returns the list of collection names that were created (or recreated).
        Existing collections are left untouched unless ``recreate`` is True.
        """
        created: List[str] = []
        for spec in collections:
            try:
                existed = self._collection_exists(spec.name)
                if existed and not recreate:
                    continue
                if existed and recreate:
                    self._delete_collection(spec.name)
                self._create_collection(spec)
                created.append(spec.name)
                self._logger.info(
                    "Qdrant collection provisioned",
                    extra={"collection": spec.name, "recreated": recreate},
                )
            except VectorStoreError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(
                    "provision_collections", collection=spec.name, cause=exc,
                ) from exc
        return created

    def ensure_collection(self, name: str) -> CollectionSpec:
        """Ensure the canonical collection ``name`` exists, creating it if needed.

        Resolves the spec from :mod:`collections` and delegates to
        :meth:`provision_collections`.
        """
        spec = get_collection_spec(name)
        self.provision_collections((spec,))
        return spec

    # ------------------------------------------------------------------ discovery
    def list_collections(self) -> List[str]:
        """Return the names of all collections currently on the server."""
        client = self._require_client()
        try:
            response = client.get_collections()
            # qdrant returns CollectionsResponse(collections=[Collection(name=...)])
            items = getattr(response, "collections", None) or []
            return [getattr(c, "name", str(c)) for c in items]
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "list_collections", collection=None, cause=exc,
            ) from exc

    def collection_info(self, name: str) -> Dict[str, Any]:
        """Return raw status info for ``name`` as a dict."""
        client = self._require_client()
        try:
            info = client.get_collection(name)
            return _coerce_info(info)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "collection_info", collection=name, cause=exc,
            ) from exc

    # ------------------------------------------------------------------ health
    def ping(self) -> bool:
        """Return True if the client can reach the server in under ``timeout``."""
        try:
            client = self._require_client()
            # The cheapest probe is calling get_collections; small payload.
            client.get_collections()
            return True
        except Exception:  # noqa: BLE001 - deliberate; ping is best-effort
            return False

    def health(self) -> QdrantHealth:
        """Return a structured health snapshot for the database health manager."""
        with self._lock:
            if not self._initialized or self._client is None:
                return QdrantHealth(
                    status=QdrantHealthStatus.NOT_INITIALIZED,
                    detail="QdrantClient.initialize() has not been called",
                )

        t0 = time.perf_counter()
        try:
            server_names = set(self.list_collections())
        except VectorStoreError as exc:
            self._publish(
                "database.health.failed",
                payload={"backend": "qdrant", "reason": str(exc)},
                priority=EventPriority.HIGH,
            )
            return QdrantHealth(
                status=QdrantHealthStatus.UNREACHABLE,
                detail=str(exc),
            )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        expected = {spec.name for spec in self._config.collections}
        missing = tuple(sorted(expected - server_names))
        ready = len(expected - set(missing))

        if missing:
            return QdrantHealth(
                status=QdrantHealthStatus.DEGRADED,
                collections_total=len(server_names),
                collections_ready=ready,
                missing_collections=missing,
                latency_ms=latency_ms,
                detail=f"{len(missing)} collection(s) missing",
            )
        return QdrantHealth(
            status=QdrantHealthStatus.HEALTHY,
            collections_total=len(server_names),
            collections_ready=ready,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------ internal
    def _build_underlying_client(self) -> Any:
        """Construct the upstream :class:`qdrant_client.QdrantClient`.

        The import is deferred to this method so that simply importing this
        module does not require ``qdrant_client`` to be installed (an
        import-safe pattern mirroring the rest of the database package).
        """
        try:
            from qdrant_client import QdrantClient as _SDKClient
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VectorStoreError(
                "initialize",
                collection=None,
                cause=exc,
            ).with_context(
                reason="qdrant_client package is not installed",
            ) from exc

        cfg = self._config
        if cfg.mode == "server":
            return _SDKClient(
                url=f"{cfg.host}:{cfg.port}" if cfg.host else None,
                api_key=cfg.api_key,
                prefer_grpc=cfg.prefer_grpc,
                timeout=cfg.timeout_seconds,
            )
        # local embedded mode (default): on-disk persistence
        path = str(cfg.path) if cfg.path else None
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
        return _SDKClient(path=path)

    def _require_client(self) -> Any:
        with self._lock:
            if not self._initialized or self._client is None:
                raise VectorStoreError(
                    "client_not_ready", collection=None,
                ).with_context(reason="QdrantClient.initialize() not called")
            return self._client

    def _collection_exists(self, name: str) -> bool:
        client = self._require_client()
        try:
            # The SDK raises if the collection does not exist; treat any
            # unexpected response as "missing".
            try:
                client.get_collection(name)
                return True
            except Exception as exc:  # noqa: BLE001
                # qdrant raises ``UnexpectedResponse`` for 404. Any other
                # exception is propagated below.
                msg = str(exc).lower()
                if "not found" in msg or "404" in msg or "doesn't exist" in msg:
                    return False
                raise
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "_collection_exists", collection=name, cause=exc,
            ) from exc

    def _create_collection(self, spec: CollectionSpec) -> None:
        """Create a single collection from its declarative spec.

        The SDK model objects (``VectorParams``, ``ScalarQuantization``) are
        produced by the spec's ``to_*`` helpers, which import from
        ``qdrant_client.http.models`` — this method only constructs keyword
        arguments and forwards them to ``client.create_collection``.
        """
        client = self._require_client()

        kwargs: Dict[str, Any] = {
            "collection_name": spec.name,
            "vectors_config": spec.to_vector_params(),
        }

        # HNSW configuration (plain dict is accepted by the SDK).
        kwargs["hnsw_config"] = spec.to_hnsw_config()

        # On-disk payload
        if spec.on_disk_payload:
            kwargs["on_disk_payload"] = True

        # Scalar quantization (memory saving for large collections). The spec
        # returns None when quantization is disabled or the SDK lacks the
        # required model, so we only forward a real config.
        quantization = spec.to_quantization_config()
        if quantization is not None:
            kwargs["quantization_config"] = quantization

        try:
            client.create_collection(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "create_collection", collection=spec.name, cause=exc,
            ) from exc

        # Create payload indexes (best-effort; failure here is non-fatal).
        for idx in spec.payload_indexes:
            self._create_payload_index(spec.name, idx)

    def _create_payload_index(self, collection: str, idx: PayloadIndex) -> None:
        """Create a single payload scalar index (best-effort, non-fatal)."""
        client = self._require_client()
        try:
            from qdrant_client.http.models import (
                KeywordIndexParams,
                IntegerIndexParams,
                FloatIndexParams,
                BoolIndexParams,
                DatetimeIndexParams,
                TextIndexParams,
                OptimizersConfigDiff,
            )
        except ImportError:  # pragma: no cover
            self._logger.warning(
                "Payload indexing requested but SDK models unavailable",
                extra={"collection": collection, "field": idx.field},
            )
            return

        params_map = {
            PayloadIndexType.KEYWORD: lambda: KeywordIndexParams(type="keyword"),
            PayloadIndexType.INTEGER: lambda: IntegerIndexParams(type="integer"),
            PayloadIndexType.FLOAT: lambda: FloatIndexParams(type="float"),
            PayloadIndexType.BOOL: lambda: BoolIndexParams(type="bool"),
            PayloadIndexType.DATETIME: lambda: DatetimeIndexParams(type="datetime"),
            PayloadIndexType.TEXT: lambda: TextIndexParams(type="text"),
        }
        builder = params_map.get(idx.index_type)
        if builder is None:
            return
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=idx.field,
                field_schema=builder(),
            )
            if idx.is_tenant:
                # Optimise for tenant-style partitioning where possible.
                try:
                    client.update_collection(
                        collection_name=collection,
                        optimizer_config=OptimizersConfigDiff(indexing_threshold=0),
                    )
                except Exception:  # noqa: BLE001 - optional optimisation
                    pass
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "Failed to create payload index",
                exc_info=exc,
                extra={"collection": collection, "field": idx.field},
            )

    def _delete_collection(self, name: str) -> None:
        client = self._require_client()
        try:
            client.delete_collection(name)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "delete_collection", collection=name, cause=exc,
            ) from exc

    # ------------------------------------------------------------------ events
    def _publish(
        self,
        name: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        priority: Optional[EventPriority] = None,
    ) -> None:
        """Publish a lifecycle event through the bound publisher (if any)."""
        if self._publisher is None:
            return
        try:
            self._publisher.emit(name, payload or {}, priority=priority)
        except Exception:  # noqa: BLE001 - never fail because of event publish
            pass

    def _log_path(self, factory: LoggerFactory) -> str:
        """Resolve a per-component log path under logs/system."""
        try:
            from app.core.constants.paths import LOG_SYSTEM_DIR
            return str(LOG_SYSTEM_DIR / "qdrant.log")
        except Exception:  # noqa: BLE001 - keep defensive at construction time
            return "logs/system/qdrant.log"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_info(info: Any) -> Dict[str, Any]:
    """Best-effort conversion of the raw Qdrant collection-info object."""
    if isinstance(info, dict):
        return dict(info)
    # Many SDK models expose a model_dump() (pydantic) or .dict() method.
    for method in ("model_dump", "dict", "to_dict"):
        fn = getattr(info, method, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:  # noqa: BLE001
                continue
    # Last resort: stringify the attrs we can introspect.
    coerced: Dict[str, Any] = {}
    for attr in ("status", "vectors_count", "points_count", "indexed_vectors_count"):
        if hasattr(info, attr):
            try:
                coerced[attr] = getattr(info, attr)
            except Exception:  # noqa: BLE001
                continue
    return coerced


# ---------------------------------------------------------------------------
# DI registration helper
# ---------------------------------------------------------------------------


def register_qdrant_client(
    container: Any,
    *,
    config: Optional[QdrantClientConfig] = None,
    config_manager: Optional[ConfigManager] = None,
    start: bool = True,
    auto_provision: Optional[bool] = None,
    collections: Optional[Sequence[CollectionSpec]] = None,
) -> QdrantClient:
    """Create, register, and optionally initialize a :class:`QdrantClient`.

    Registers the client as a singleton in ``container`` and returns the
    instance. ``auto_provision`` overrides the config-driven flag when given.
    """
    from app.dependency_injection.container import Container  # local import to avoid cycle

    if not isinstance(container, Container):  # defensive; cheap isinstance
        raise TypeError("register_qdrant_client requires a DI Container")

    cfg = config or QdrantClientConfig.from_config(config_manager, collections=collections)
    if auto_provision is not None:
        # dataclass is frozen; rebuild with the override.
        cfg = QdrantClientConfig(
            mode=cfg.mode,
            path=cfg.path,
            host=cfg.host,
            port=cfg.port,
            grpc_port=cfg.grpc_port,
            api_key=cfg.api_key,
            prefer_grpc=cfg.prefer_grpc,
            timeout_seconds=cfg.timeout_seconds,
            auto_provision=auto_provision,
            collections=cfg.collections if collections is None else tuple(collections),
        )

    event_bus = container.try_resolve(EventBus)
    logger_factory = container.try_resolve(LoggerFactory)

    client = QdrantClient(
        config=cfg,
        config_manager=config_manager,
        logger_factory=logger_factory,
        event_bus=event_bus,
    )

    container.register_instance(QdrantClient, client, replace=True)
    if start:
        client.initialize()
    return client
