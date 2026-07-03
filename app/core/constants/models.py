# app/core/constants/models.py
"""
AI model registry constants for AIOS.

Defines the closed set of model *roles*, concrete model identifiers, execution
devices, quantization options, and default primary/fallback routing used by:
    * FG2 Model Manager   (Gemma, Groq, MiniLM, embeddings, Qdrant embeddings)
    * FG4 Language Core   (Whisper, Meta Omnilingual CTC, MarianMT, Kokoro, Piper)
    * FG3 Vision Tiers    (YOLOv26, PaddleOCR, GoClick/Florence-2)
    * FG6 Authentication  (CAMP++, ECAPA-TDNN, Silero VAD)

The editable, environment-specific bindings live in
`configs/model_registry.yaml`; this module fixes the vocabulary those configs
may reference so the config validator can reject unknown models at load time.

Design rules:
    * `str`-based enums; immutable spec maps frozen with MappingProxyType.
    * Resource figures are advisory targets for the reference hardware
      (Ryzen 7 + RTX 5050 / 8 GB VRAM) — used for scheduling, not hard limits.
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping, Optional


# ---------------------------------------------------------------------------
# Model roles and execution devices
# ---------------------------------------------------------------------------


class ModelRole(str, Enum):
    """The functional slot a model fills within the pipeline."""

    INTENT = "intent"
    LOCAL_LLM = "local_llm"
    CLOUD_LLM = "cloud_llm"
    EMBEDDING = "embedding"
    MULTILINGUAL_EMBEDDING = "multilingual_embedding"
    ASR = "asr"
    LANGUAGE_DETECT = "language_detect"
    TRANSLATION = "translation"
    TTS = "tts"
    SPEAKER_VERIFICATION = "speaker_verification"
    VAD = "vad"
    WAKE_WORD = "wake_word"
    VISION_DETECT = "vision_detect"
    OCR = "ocr"
    VISION_LANGUAGE = "vision_language"


class Device(str, Enum):
    """Preferred execution device. Model Manager may fall back CPU<->GPU."""

    CPU = "cpu"
    GPU = "gpu"
    AUTO = "auto"


class Quantization(str, Enum):
    """Supported quantization formats managed by the Model Manager."""

    NONE = "none"
    FP16 = "fp16"
    INT8 = "int8"
    Q4_K_M = "q4_k_m"   # llama.cpp GGUF quant for local Gemma
    Q5_K_M = "q5_k_m"


# ---------------------------------------------------------------------------
# Concrete model identifiers (as named in the SDDs)
# ---------------------------------------------------------------------------


class ModelId(str, Enum):
    """Canonical identifiers for every model referenced by the architecture."""

    # Intent / semantic
    MINILM_L6_V2 = "all-MiniLM-L6-v2"
    E5_MULTILINGUAL_SMALL = "multilingual-e5-small"

    # LLMs
    GEMMA_4_E2B = "gemma-4-e2b"          # Local, via llama.cpp
    GROQ = "groq"                        # Cloud API

    # Speech recognition / language detection
    WHISPER_SMALL = "whisper-small"
    META_OMNILINGUAL_CTC = "meta-omnilingual-ctc"

    # Translation
    MARIANMT_INT8 = "marianmt-int8"      # via CTranslate2

    # Text-to-speech
    KOKORO_82M = "kokoro-82m"
    PIPER = "piper"
    INDIC_TTS = "indic-tts"

    # Speaker verification / VAD / wake word
    CAMPPLUSPLUS = "campplusplus"
    ECAPA_TDNN = "ecapa-tdnn"
    SILERO_VAD = "silero-vad"
    OPENWAKEWORD = "openwakeword"

    # Vision tiers (FG3)
    YOLOV26_MEDIUM = "yolov26-medium"
    PADDLEOCR = "paddleocr"
    GOCLICK_FLORENCE2 = "goclick-florence2"


# ---------------------------------------------------------------------------
# Model specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Immutable descriptor for a single model.

    Attributes:
        model_id:        Canonical identifier.
        role:            Functional slot filled.
        device:          Preferred execution device.
        offline:         True if runnable without network access.
        approx_vram_mb:  Advisory VRAM footprint on the reference GPU (0 = CPU).
        approx_ram_mb:   Advisory system RAM footprint.
        quantizations:   Supported quant formats (first = recommended default).
        runtime:         Backing runtime/engine (informational).
    """

    model_id: ModelId
    role: ModelRole
    device: Device
    offline: bool
    approx_vram_mb: int = 0
    approx_ram_mb: int = 0
    quantizations: tuple[Quantization, ...] = field(default_factory=tuple)
    runtime: Optional[str] = None


MODEL_SPECS: Final[Mapping[ModelId, ModelSpec]] = MappingProxyType(
    {
        ModelId.MINILM_L6_V2: ModelSpec(
            ModelId.MINILM_L6_V2, ModelRole.INTENT, Device.CPU,
            offline=True, approx_ram_mb=120,
            quantizations=(Quantization.NONE,), runtime="sentence-transformers",
        ),
        ModelId.E5_MULTILINGUAL_SMALL: ModelSpec(
            ModelId.E5_MULTILINGUAL_SMALL, ModelRole.MULTILINGUAL_EMBEDDING, Device.GPU,
            offline=True, approx_vram_mb=300,
            quantizations=(Quantization.FP16, Quantization.NONE),
            runtime="sentence-transformers",
        ),
        ModelId.GEMMA_4_E2B: ModelSpec(
            ModelId.GEMMA_4_E2B, ModelRole.LOCAL_LLM, Device.GPU,
            offline=True, approx_vram_mb=3200, approx_ram_mb=1500,
            quantizations=(Quantization.Q4_K_M, Quantization.Q5_K_M),
            runtime="llama.cpp",
        ),
        ModelId.GROQ: ModelSpec(
            ModelId.GROQ, ModelRole.CLOUD_LLM, Device.CPU,
            offline=False, runtime="Groq API",
        ),
        ModelId.WHISPER_SMALL: ModelSpec(
            ModelId.WHISPER_SMALL, ModelRole.ASR, Device.GPU,
            offline=True, approx_vram_mb=1000,
            quantizations=(Quantization.FP16, Quantization.INT8),
            runtime="openai-whisper",
        ),
        ModelId.META_OMNILINGUAL_CTC: ModelSpec(
            ModelId.META_OMNILINGUAL_CTC, ModelRole.ASR, Device.GPU,
            offline=True, approx_vram_mb=1200,
            quantizations=(Quantization.INT8,), runtime="Sherpa-ONNX",
        ),
        ModelId.MARIANMT_INT8: ModelSpec(
            ModelId.MARIANMT_INT8, ModelRole.TRANSLATION, Device.GPU,
            offline=True, approx_vram_mb=450,
            quantizations=(Quantization.INT8,), runtime="CTranslate2",
        ),
        ModelId.KOKORO_82M: ModelSpec(
            ModelId.KOKORO_82M, ModelRole.TTS, Device.GPU,
            offline=True, approx_vram_mb=400,
            quantizations=(Quantization.FP16,), runtime="Sherpa-ONNX / Kokoro",
        ),
        ModelId.PIPER: ModelSpec(
            ModelId.PIPER, ModelRole.TTS, Device.CPU,
            offline=True, approx_ram_mb=200, runtime="Piper",
        ),
        ModelId.INDIC_TTS: ModelSpec(
            ModelId.INDIC_TTS, ModelRole.TTS, Device.GPU,
            offline=True, approx_vram_mb=400, runtime="Indic TTS",
        ),
        ModelId.CAMPPLUSPLUS: ModelSpec(
            ModelId.CAMPPLUSPLUS, ModelRole.SPEAKER_VERIFICATION, Device.GPU,
            offline=True, approx_vram_mb=200, runtime="Sherpa-ONNX",
        ),
        ModelId.ECAPA_TDNN: ModelSpec(
            ModelId.ECAPA_TDNN, ModelRole.SPEAKER_VERIFICATION, Device.GPU,
            offline=True, approx_vram_mb=250, runtime="Sherpa-ONNX",
        ),
        ModelId.SILERO_VAD: ModelSpec(
            ModelId.SILERO_VAD, ModelRole.VAD, Device.CPU,
            offline=True, approx_ram_mb=60, runtime="Silero",
        ),
        ModelId.OPENWAKEWORD: ModelSpec(
            ModelId.OPENWAKEWORD, ModelRole.WAKE_WORD, Device.CPU,
            offline=True, approx_ram_mb=80, runtime="OpenWakeWord",
        ),
        ModelId.YOLOV26_MEDIUM: ModelSpec(
            ModelId.YOLOV26_MEDIUM, ModelRole.VISION_DETECT, Device.GPU,
            offline=True, approx_vram_mb=450, runtime="Ultralytics",
        ),
        ModelId.PADDLEOCR: ModelSpec(
            ModelId.PADDLEOCR, ModelRole.OCR, Device.GPU,
            offline=True, approx_vram_mb=300, runtime="PaddleOCR",
        ),
        ModelId.GOCLICK_FLORENCE2: ModelSpec(
            ModelId.GOCLICK_FLORENCE2, ModelRole.VISION_LANGUAGE, Device.GPU,
            offline=True, approx_vram_mb=500, runtime="Florence-2",
        ),
    }
)


# ---------------------------------------------------------------------------
# Default primary / fallback routing per role
# ---------------------------------------------------------------------------
# Mirrors the model registries in FG2 §32 and FG4. `model_registry.yaml`
# overrides these at runtime; the tuple order is (primary, *fallbacks).

DEFAULT_MODEL_ROUTING: Final[Mapping[ModelRole, tuple[ModelId, ...]]] = MappingProxyType(
    {
        ModelRole.INTENT: (ModelId.MINILM_L6_V2,),
        ModelRole.LOCAL_LLM: (ModelId.GEMMA_4_E2B,),
        ModelRole.CLOUD_LLM: (ModelId.GROQ,),
        ModelRole.MULTILINGUAL_EMBEDDING: (ModelId.E5_MULTILINGUAL_SMALL,),
        ModelRole.ASR: (ModelId.WHISPER_SMALL, ModelId.META_OMNILINGUAL_CTC),
        ModelRole.LANGUAGE_DETECT: (ModelId.WHISPER_SMALL, ModelId.META_OMNILINGUAL_CTC),
        ModelRole.TRANSLATION: (ModelId.MARIANMT_INT8,),
        ModelRole.TTS: (ModelId.KOKORO_82M, ModelId.PIPER),
        ModelRole.SPEAKER_VERIFICATION: (ModelId.CAMPPLUSPLUS, ModelId.ECAPA_TDNN),
        ModelRole.VAD: (ModelId.SILERO_VAD,),
        ModelRole.WAKE_WORD: (ModelId.OPENWAKEWORD,),
        ModelRole.VISION_DETECT: (ModelId.YOLOV26_MEDIUM,),
        ModelRole.OCR: (ModelId.PADDLEOCR,),
        ModelRole.VISION_LANGUAGE: (ModelId.GOCLICK_FLORENCE2,),
    }
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def get_spec(model_id: ModelId) -> ModelSpec:
    """Return the immutable spec for a model, or raise KeyError if unknown."""
    return MODEL_SPECS[model_id]


def primary_model(role: ModelRole) -> ModelId:
    """Return the default primary model for a role."""
    return DEFAULT_MODEL_ROUTING[role][0]


def fallback_models(role: ModelRole) -> tuple[ModelId, ...]:
    """Return the ordered fallback models for a role (may be empty)."""
    return DEFAULT_MODEL_ROUTING[role][1:]


def models_for_role(role: ModelRole) -> tuple[ModelId, ...]:
    """Return every model registered for a role, primary first."""
    return DEFAULT_MODEL_ROUTING.get(role, ())


def offline_models() -> tuple[ModelId, ...]:
    """Return all models runnable without network access."""
    return tuple(mid for mid, spec in MODEL_SPECS.items() if spec.offline)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ModelRole",
    "Device",
    "Quantization",
    "ModelId",
    "ModelSpec",
    "MODEL_SPECS",
    "DEFAULT_MODEL_ROUTING",
    "get_spec",
    "primary_model",
    "fallback_models",
    "models_for_role",
    "offline_models",
]
