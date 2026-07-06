# app/core/database/qdrant/embeddings.py
"""
Embedding generation abstraction for the Qdrant vector store.
============================================================
Encodes text into the dense vectors stored in Qdrant. Wraps
``sentence-transformers`` behind a thread-safe, lazily-loaded service so that:

* The model loads once and stays warm in memory; repeated embedding calls share
  the same encoder instance (FG2 §12 Context Builder, FG10 §Engine 8).
* The model is configurable via ``model_registry.yaml`` with a fallback chain
  (FG2 / FG4 — model swap is a config change, not a code change).
* GPU/CPU and device selection are derived from the model registry and the
  active :class:`ConfigManager`; callers never hardcode device strings.
* A pluggable interface (:class:`EmbeddingProvider`) lets the model be replaced
  without touching the vector store — important for FG10 model-optimization
  experiments and for tests (a deterministic :class:`HashingEmbedder` ships
  here so the vector store remains testable without downloading weights).
* Batch encoding is supported so memory long-term writes and search-cache
  population stay efficient.

The default provider (:class:`SentenceTransformerEmbedder`) loads
``multilingual-e5-small`` (384 dims) — the model chosen in
``model_registry.yaml`` — and matches the dimensionality declared in every
default :class:`CollectionSpec` (see :mod:`collections`).
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import numpy as np

# Top-level dependency import. Kept import-safe: when ``sentence-transformers``
# is not installed, the bare name is replaced with ``None`` and the
# :class:`SentenceTransformerEmbedder` raises a structured
# :class:`VectorStoreError` only when ``load()`` is actually called, so simply
# importing this module never crashes the process. This matches the pattern
# used by the SQLite/SQLCipher engines (``sqlite3`` is import-safe even when
# optional extensions are missing).
try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover - environment dependent
    SentenceTransformer = None  # type: ignore[assignment,misc]

from app.core.configs.config_manager import ConfigManager, get_config_manager
from app.core.constants.settings import RetryPolicy, DEFAULT_RETRY
from app.core.exceptions import VectorStoreError
from app.logging.logger import Logger, LogLevel
from app.logging.logger_factory import LoggerFactory

__all__ = [
    "EmbeddingProvider",
    "EmbeddingResult",
    "EmbedderConfig",
    "SentenceTransformerEmbedder",
    "HashingEmbedder",
    "Embeddings",
    "default_embeddings",
]


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    """Pluggable embedding backend.

    Implementations must be thread-safe (sentence-transformers models are). The
    :class:`Embeddings` service calls :meth:`encode` and :meth:`encode_batch`
    from many threads (FG2 context builder, FG10 memory engine, search cache).
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @property
    @abstractmethod
    def is_loaded(self) -> bool: ...

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...

    @abstractmethod
    def encode(self, text: str, *, normalize: bool = True) -> np.ndarray: ...

    @abstractmethod
    def encode_batch(
        self,
        texts: Sequence[str],
        *,
        normalize: bool = True,
    ) -> List[np.ndarray]: ...

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r} dim={self.dimension}>"


# ---------------------------------------------------------------------------
# Embedding result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """A single encoded item with provenance metadata.

    Carrying the model name and dimension alongside the vector lets callers
    detect mismatches before upsert (vectors must match the collection's
    declared ``vector_size``).
    """

    text: str
    vector: np.ndarray
    model: str
    dimension: int

    def as_list(self) -> List[float]:
        """Return the vector as a plain ``list[float]`` (for JSON serialization)."""
        return self.vector.tolist()


# ---------------------------------------------------------------------------
# Embedder configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbedderConfig:
    """Configuration for the embedding service.

    Defaults are pulled from ``model_registry``:

    * ``model_registry.embedding`` — the canonical embedding model name.
    * ``model_registry.models[<name>].device`` — "gpu" | "cpu" | "cloud".
    * ``model_registry.models[<name>].engine`` — must be a supported backend.

    The dataclass layer allows per-instance overrides for tests and FG10
    experiments without rebuilding the global config snapshot.
    """

    model_name: str = "multilingual-e5-small"
    engine: str = "sentence-transformers"
    device: Optional[str] = None           # None => "auto" (GPU if available)
    dimension: int = 384                    # matches every default CollectionSpec
    normalize: bool = True                  # cosine distance requires unit vectors
    batch_size: int = 32
    convert_to_numpy: bool = True
    show_progress_bar: bool = False
    cache_folder: Optional[str] = None      # HF cache override; None = default
    retry: RetryPolicy = field(default_factory=lambda: DEFAULT_RETRY)

    @classmethod
    def from_config(
        cls,
        config: Optional[ConfigManager] = None,
        *,
        override_model: Optional[str] = None,
    ) -> "EmbedderConfig":
        """Build an :class:`EmbedderConfig` from the config snapshot."""
        cfg = config or get_config_manager()

        model_name = override_model or cfg.get_str("model_registry.embedding", "multilingual-e5-small")
        model_name = model_name or "multilingual-e5-small"

        engine = cfg.get_str(f"model_registry.models.{model_name}.engine", "sentence-transformers") or "sentence-transformers"
        device = cfg.get_str(f"model_registry.models.{model_name}.device", None)

        # Dimension is intrinsic to the model; keep an explicit override entry
        # so config can fix a wrong catalog value without a code patch.
        configured_dim = cfg.get_int(f"model_registry.models.{model_name}.dimension", None)
        dimension = int(configured_dim) if configured_dim else _DEFAULT_DIMS.get(model_name, 384)

        batch_size = int(cfg.get_int("qdrant.embedding.batch_size", 32) or 32)

        return cls(
            model_name=model_name,
            engine=engine,
            device=device,
            dimension=dimension,
            batch_size=batch_size,
        )


# Default vector sizes for the models referenced in model_registry.yaml.
_DEFAULT_DIMS: Dict[str, int] = {
    "multilingual-e5-small": 384,
    "all-MiniLM-L6-v2": 384,
    "all-mpnet-base-v2": 768,
    "bge-small-en-v1.5": 384,
    "bge-base-en-v1.5": 768,
}


# ---------------------------------------------------------------------------
# SentenceTransformers provider (default)
# ---------------------------------------------------------------------------


class SentenceTransformerEmbedder(EmbeddingProvider):
    """Embedding backend backed by the ``sentence-transformers`` library.

    The model is loaded lazily on first :meth:`encode` call so that simply
    constructing the object is cheap and import-safe. Subsequent calls share
    the warmed model. The wrapped model instance is guarded by a reentrant
    lock so concurrent encode calls from event-bus workers and the
    """
    _DEFAULT_NAME = "multilingual-e5-small"

    def __init__(
        self,
        config: EmbedderConfig,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self._config = config
        self._logger = logger or LoggerFactory().create_console_logger(
            "core.database.qdrant.embeddings", LogLevel.INFO,
        )
        self._lock = threading.RLock()
        self._model: Any = None
        self._loaded = False
        self._device_resolved: Optional[str] = None

    # -------------------------------------------------- properties
    @property
    def name(self) -> str:
        return self._config.model_name

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    # -------------------------------------------------- lifecycle
    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            if SentenceTransformer is None:
                raise VectorStoreError(
                    "load_embedding_model", collection=None,
                ).with_context(
                    model=self._config.model_name,
                    reason="sentence-transformers is not installed",
                )

            device = self._resolve_device()
            try:
                self._model = SentenceTransformer(
                    self._config.model_name,
                    device=device,
                    cache_folder=self._config.cache_folder,
                )
                self._device_resolved = device
                self._loaded = True
                self._logger.info(
                    "Embedding model loaded",
                    extra={
                        "model": self._config.model_name,
                        "device": str(device),
                        "dimension": self._config.dimension,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(
                    "load_embedding_model",
                    collection=None,
                    cause=exc,
                ).with_context(
                    model=self._config.model_name,
                    device=str(device),
                ) from exc

    def unload(self) -> None:
        with self._lock:
            if not self._loaded:
                return
            model = self._model
            self._model = None
            self._loaded = False
            self._device_resolved = None
        # Release GPU memory outside the lock.
        if model is not None:
            try:
                # sentence-transformers exposes a .to('cpu') plus del-friendly
                # release path; avoid importing torch here. Best-effort cleanup.
                release = getattr(model, "release_memory", None)
                if callable(release):
                    release()
            except Exception:  # noqa: BLE001
                pass
        self._logger.info(
            "Embedding model unloaded",
            extra={"model": self._config.model_name},
        )

    # -------------------------------------------------- encoding
    def encode(self, text: str, *, normalize: bool = True) -> np.ndarray:
        if not text:
            # A zero vector is a safe placeholder; callers should filter empty
            # texts upstream, but we never raise here to keep the embedding
            # path call-site simple.
            return np.zeros(self._config.dimension, dtype=np.float32)
        self._ensure_loaded()
        try:
            with self._lock:
                vector = self._model.encode(
                    text,
                    normalize_embeddings=normalize and self._config.normalize,
                    convert_to_numpy=self._config.convert_to_numpy,
                    show_progress_bar=False,
                )
            return np.asarray(vector, dtype=np.float32)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "encode", collection=None, cause=exc,
            ).with_context(
                model=self._config.model_name,
                text_preview=text[:200],
            ) from exc

    def encode_batch(
        self,
        texts: Sequence[str],
        *,
        normalize: bool = True,
    ) -> List[np.ndarray]:
        cleaned = [t or "" for t in texts]
        if not cleaned:
            return []
        self._ensure_loaded()
        try:
            with self._lock:
                vectors = self._model.encode(
                    list(cleaned),
                    batch_size=self._config.batch_size,
                    normalize_embeddings=normalize and self._config.normalize,
                    convert_to_numpy=self._config.convert_to_numpy,
                    show_progress_bar=self._config.show_progress_bar,
                )
            return [np.asarray(v, dtype=np.float32) for v in vectors]
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(
                "encode_batch", collection=None, cause=exc,
            ).with_context(model=self._config.model_name, count=len(cleaned)) from exc

    # -------------------------------------------------- helpers
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _resolve_device(self) -> str:
        """Resolve the device the model will run on.

        Precedence:
            1. ``EmbedderConfig.device`` (explicit override).
            2. The model-registry ``device`` field (e.g. "gpu" / "cpu").
            3. Auto-detect: GPU if a CUDA device is visible, else CPU.
        """
        if self._config.device:
            mapped = _normalize_device(self._config.device)
            if mapped is not None:
                return mapped
        # Auto-detect only when torch is importable; never crash otherwise.
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:  # noqa: BLE001
            pass
        return "cpu"


def _normalize_device(device: str) -> Optional[str]:
    """Translate the model-registry device vocabulary to torch's."""
    d = (device or "").strip().lower()
    if not d or d == "auto":
        return None
    if d in ("gpu", "cuda", "cuda:0"):
        return "cuda"
    if d == "cpu":
        return "cpu"
    # Allow passthrough for devices like "mps", "cuda:1", etc.
    return d


# ---------------------------------------------------------------------------
# Hashing embedder (test / fallback provider — deterministic, no network)
# ---------------------------------------------------------------------------


class HashingEmbedder(EmbeddingProvider):
    """Deterministic, dependency-free embedder for tests and offline fallback.

    Produces a unit-normalized float32 vector by hashing the text into the
    configured dimensionality. It is NOT a semantic encoder; the only guarantee
    is that identical inputs map to identical vectors. It exists so that:

    * The vector store and unit tests can run without downloading weights.
    * FG10 experiments can dry-run the embedding pipeline offline.
    * The FG2 semantic cache can degrade (rather than crash) when weights
      are unavailable, matching the FG2 "offline-first" design.
    """

    _NAME = "hashing-fallback"

    def __init__(
        self,
        dimension: int = 384,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        if dimension <= 0:
            raise ValueError("HashingEmbedder dimension must be positive")
        self._dimension = dimension
        self._logger = logger or LoggerFactory().create_console_logger(
            "core.database.qdrant.embeddings.hash", LogLevel.INFO,
        )
        self._lock = threading.RLock()
        self._loaded = True

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_loaded(self) -> bool:
        return True

    def load(self) -> None:
        pass

    def unload(self) -> None:
        pass

    # -------------------------------------------------- encoding
    def encode(self, text: str, *, normalize: bool = True) -> np.ndarray:
        dim = self._dimension
        vec = np.zeros(dim, dtype=np.float32)
        if not text:
            return vec
        # Use SHA256 to deterministically sprinkle bytes across the vector.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Walk the digest in 4-byte windows to fill the vector. If the digest
        # is shorter than the vector, keep replaying it with an offset salt so
        # every index receives data derived from the input.
        idx = 0
        salt = 0
        while idx < dim:
            chunk = hashlib.sha256(digest + salt.to_bytes(4, "big")).digest()
            for b in chunk:
                if idx >= dim:
                    break
                vec[idx] = float(b) - 127.5
                idx += 1
            salt += 1
        if normalize:
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
        return vec

    def encode_batch(
        self,
        texts: Sequence[str],
        *,
        normalize: bool = True,
    ) -> List[np.ndarray]:
        return [self.encode(t, normalize=normalize) for t in texts]


# ---------------------------------------------------------------------------
# Embeddings service
# ---------------------------------------------------------------------------


class Embeddings:
    """High-level embedding facade used by the vector store and search cache.

    Responsibilities:

    * Hold a single :class:`EmbeddingProvider`, swap it atomically, and validate
      that a swap preserves the declared dimension (or rejects it — to protect
      existing collections from silently incompatible vectors).
    * Expose ergonomic ``embed`` / ``embed_batch`` methods returning
      :class:`EmbeddingResult` objects that carry the model name and dimension.
    * Provide a graceful fallback to :class:`HashingEmbedder` when the primary
      provider fails to load — keeping the AI brain running offline (FG2).
    * Publish provider changes via callbacks so the vector store can rebuild
      collection configs if a dimension-changing swap is requested explicitly.

    The service is safe to share across threads (model loading and the actual
    encode calls are guarded by the provider itself; the wrapper lock only
    protects swap operations).
    """

    def __init__(
        self,
        *,
        provider: Optional[EmbeddingProvider] = None,
        config: Optional[EmbedderConfig] = None,
        config_manager: Optional[ConfigManager] = None,
        logger_factory: Optional[LoggerFactory] = None,
        logger: Optional[Logger] = None,
        fallback_on_failure: bool = True,
    ) -> None:
        factory = logger_factory or LoggerFactory()
        self._logger = logger or factory.create_console_logger(
            "core.database.qdrant.embeddings", LogLevel.INFO,
        )

        self._config_manager = config_manager
        self._config = config or EmbedderConfig.from_config(config_manager)
        self._fallback_on_failure = fallback_on_failure
        self._lock = threading.RLock()
        self._provider = provider or SentenceTransformerEmbedder(
            self._config, logger=self._logger,
        )
        self._fallback: Optional[HashingEmbedder] = None
        self._active_fallback = False

    # -------------------------------------------------- provider access
    @property
    def provider(self) -> EmbeddingProvider:
        with self._lock:
            return self._fallback if self._active_fallback else self._provider

    @property
    def config(self) -> EmbedderConfig:
        return self._config

    @property
    def dimension(self) -> int:
        return self.provider.dimension

    @property
    def model_name(self) -> str:
        return self.provider.name

    @property
    def is_fallback_active(self) -> bool:
        with self._lock:
            return self._active_fallback

    # -------------------------------------------------- lifecycle
    def load(self) -> None:
        """Pre-warm the active provider (best-effort with fallback)."""
        try:
            self._provider.load()
        except VectorStoreError as exc:
            if not self._fallback_on_failure:
                raise
            self._logger.warning(
                "Primary embedding provider failed to load; activating fallback",
                exc_info=exc,
            )
            self._activate_fallback()

    def unload(self) -> None:
        with self._lock:
            try:
                self._provider.unload()
            finally:
                if self._fallback is not None:
                    self._fallback.unload()
                self._active_fallback = False

    def swap_provider(
        self,
        provider: EmbeddingProvider,
        *,
        require_same_dimension: bool = True,
    ) -> None:
        """Replace the active provider.

        When ``require_same_dimension`` is True (the default), the new provider
        must match the existing dimension or the swap is rejected — silently
        changing the embedding size would corrupt every collection currently
        populated with the old vectors. To intentionally change dimensions
        (e.g. during an FG10 model-optimization experiment), pass
        ``require_same_dimension=False`` and ensure the caller recreates the
        affected collections.
        """
        with self._lock:
            if require_same_dimension and provider.dimension != self._config.dimension:
                raise VectorStoreError(
                    "swap_provider", collection=None,
                ).with_context(
                    expected_dimension=self._config.dimension,
                    new_dimension=provider.dimension,
                    new_provider=provider.name,
                    reason=(
                        "Refusing to swap to a provider with a different vector "
                        "dimension (would corrupt existing collections). Pass "
                        "require_same_dimension=False to override, and recreate "
                        "the affected collections afterwards."
                    ),
                )
            old = self._provider
            try:
                old.unload()
            except Exception:  # noqa: BLE001
                pass
            self._provider = provider
            self._active_fallback = False
            # Reflect the new dimension so future calls report consistently.
            self._config = EmbedderConfig(
                model_name=provider.name,
                engine=self._config.engine,
                device=self._config.device,
                dimension=provider.dimension,
                normalize=self._config.normalize,
                batch_size=self._config.batch_size,
                convert_to_numpy=self._config.convert_to_numpy,
                show_progress_bar=self._config.show_progress_bar,
                cache_folder=self._config.cache_folder,
                retry=self._config.retry,
            )
        self._logger.info(
            "Embedding provider swapped",
            extra={"new_provider": provider.name, "dimension": provider.dimension},
        )

    # -------------------------------------------------- encoding
    def embed(self, text: str) -> EmbeddingResult:
        provider = self.provider
        try:
            vector = provider.encode(text, normalize=self._config.normalize)
        except VectorStoreError:
            if not self._fallback_on_failure:
                raise
            self._activate_fallback()
            provider = self.provider
            vector = provider.encode(text, normalize=self._config.normalize)
        return EmbeddingResult(
            text=text,
            vector=vector,
            model=provider.name,
            dimension=provider.dimension,
        )

    def embed_batch(self, texts: Sequence[str]) -> List[EmbeddingResult]:
        provider = self.provider
        try:
            vectors = provider.encode_batch(texts, normalize=self._config.normalize)
        except VectorStoreError:
            if not self._fallback_on_failure:
                raise
            self._activate_fallback()
            provider = self.provider
            vectors = provider.encode_batch(texts, normalize=self._config.normalize)
        return [
            EmbeddingResult(
                text=texts[i] if i < len(texts) else "",
                vector=vectors[i],
                model=provider.name,
                dimension=provider.dimension,
            )
            for i in range(len(vectors))
        ]

    def embed_documents(self, documents: Sequence[Union[str, Dict[str, Any]]]) -> List[EmbeddingResult]:
        """Embed a heterogeneous list of strings or {text, metadata} dicts."""
        texts: List[str] = []
        for doc in documents:
            if isinstance(doc, str):
                texts.append(doc)
            elif isinstance(doc, Mapping):
                texts.append(str(doc.get("text", "")))
            else:
                texts.append(str(doc))
        return self.embed_batch(texts)

    # -------------------------------------------------- internal
    def _activate_fallback(self) -> None:
        with self._lock:
            if self._fallback is None:
                self._fallback = HashingEmbedder(
                    dimension=self._config.dimension, logger=self._logger,
                )
            self._active_fallback = True
        self._logger.warning(
            "Activated hashing fallback embedder — semantic search is degraded",
        )


# ---------------------------------------------------------------------------
# Default factory
# ---------------------------------------------------------------------------


_default_embeddings: Optional[Embeddings] = None
_default_lock = threading.Lock()


def default_embeddings(
    config_manager: Optional[ConfigManager] = None,
) -> Embeddings:
    """Return a process-wide :class:`Embeddings` service (lazy singleton)."""
    global _default_embeddings
    if _default_embeddings is None:
        with _default_lock:
            if _default_embeddings is None:
                _default_embeddings = Embeddings(config_manager=config_manager)
    return _default_embeddings
