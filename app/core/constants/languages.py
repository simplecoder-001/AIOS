# app/core/constants/languages.py
"""
Language and multilingual constants for AIOS (FG4 Language Intelligence Core).

Source of truth for:
    * Supported language codes and display metadata
    * The Language Capability Registry (STT/TTS/translation/emotion per language)
    * Confidence thresholds for the Language Decision Engine (FG4)
    * Reply-language policy modes and conversation-state defaults

Concrete, editable bindings live in `configs/language_policy.yaml` and
`configs/language_config.yaml`; this module fixes the closed vocabulary those
configs may reference so unknown codes/modes are rejected at load time.

Design rules:
    * ISO 639-1 codes where available; `str`-based enums for serialization.
    * Immutable registries frozen with MappingProxyType.
    * Standard library only; import-safe; no cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, FrozenSet, Mapping


# ---------------------------------------------------------------------------
# Language codes
# ---------------------------------------------------------------------------


class LanguageCode(str, Enum):
    """Supported language codes (ISO 639-1 where applicable).

    "hinglish" is a code-mixed pseudo-language preserved intentionally by the
    Code-Mixed Understanding Engine (FG4) rather than normalized to en/hi.
    """

    ENGLISH = "en"
    HINDI = "hi"
    HINGLISH = "hinglish"
    ODIA = "or"
    BENGALI = "bn"
    TAMIL = "ta"
    TELUGU = "te"
    MARATHI = "mr"
    GUJARATI = "gu"
    KANNADA = "kn"
    MALAYALAM = "ml"
    PUNJABI = "pa"
    URDU = "ur"

    # Common foreign languages (supported when the configured models allow)
    SPANISH = "es"
    FRENCH = "fr"
    GERMAN = "de"
    CHINESE = "zh"
    JAPANESE = "ja"

    UNKNOWN = "unknown"


DEFAULT_LANGUAGE: Final[LanguageCode] = LanguageCode.ENGLISH

# Primary languages explicitly prioritized by FG4.
PRIMARY_LANGUAGES: Final[FrozenSet[LanguageCode]] = frozenset(
    {
        LanguageCode.ENGLISH,
        LanguageCode.HINDI,
        LanguageCode.HINGLISH,
        LanguageCode.ODIA,
    }
)

# Code-mixed languages that must NOT be force-translated before embedding.
CODE_MIXED_LANGUAGES: Final[FrozenSet[LanguageCode]] = frozenset(
    {LanguageCode.HINGLISH}
)


# ---------------------------------------------------------------------------
# Language capability registry (FG4 "Language Capability Registry")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LanguageCapabilities:
    """Per-language feature availability."""

    stt: bool
    tts: bool
    translation: bool
    emotion: bool


def _caps(stt: bool, tts: bool, translation: bool, emotion: bool) -> LanguageCapabilities:
    return LanguageCapabilities(stt=stt, tts=tts, translation=translation, emotion=emotion)


LANGUAGE_CAPABILITIES: Final[Mapping[LanguageCode, LanguageCapabilities]] = MappingProxyType(
    {
        LanguageCode.ENGLISH: _caps(True, True, True, True),
        LanguageCode.HINDI: _caps(True, True, True, True),
        LanguageCode.HINGLISH: _caps(True, True, True, True),
        LanguageCode.ODIA: _caps(True, True, True, True),
        LanguageCode.BENGALI: _caps(True, True, True, False),
        LanguageCode.TAMIL: _caps(True, True, True, False),
        LanguageCode.TELUGU: _caps(True, True, True, False),
        LanguageCode.MARATHI: _caps(True, True, True, False),
        LanguageCode.GUJARATI: _caps(True, True, True, False),
        LanguageCode.KANNADA: _caps(True, True, True, False),
        LanguageCode.MALAYALAM: _caps(True, True, True, False),
        LanguageCode.PUNJABI: _caps(True, True, True, False),
        LanguageCode.URDU: _caps(True, True, True, False),
        LanguageCode.SPANISH: _caps(True, True, True, False),
        LanguageCode.FRENCH: _caps(True, True, True, False),
        LanguageCode.GERMAN: _caps(True, True, True, False),
        LanguageCode.CHINESE: _caps(True, True, True, False),
        LanguageCode.JAPANESE: _caps(True, True, True, False),
    }
)


# ---------------------------------------------------------------------------
# Display metadata
# ---------------------------------------------------------------------------

LANGUAGE_DISPLAY_NAMES: Final[Mapping[LanguageCode, str]] = MappingProxyType(
    {
        LanguageCode.ENGLISH: "English",
        LanguageCode.HINDI: "हिन्दी",
        LanguageCode.HINGLISH: "Hinglish",
        LanguageCode.ODIA: "ଓଡ଼ିଆ",
        LanguageCode.BENGALI: "বাংলা",
        LanguageCode.TAMIL: "தமிழ்",
        LanguageCode.TELUGU: "తెలుగు",
        LanguageCode.MARATHI: "मराठी",
        LanguageCode.GUJARATI: "ગુજરાતી",
        LanguageCode.KANNADA: "ಕನ್ನಡ",
        LanguageCode.MALAYALAM: "മലയാളം",
        LanguageCode.PUNJABI: "ਪੰਜਾਬੀ",
        LanguageCode.URDU: "اردو",
        LanguageCode.SPANISH: "Español",
        LanguageCode.FRENCH: "Français",
        LanguageCode.GERMAN: "Deutsch",
        LanguageCode.CHINESE: "中文",
        LanguageCode.JAPANESE: "日本語",
        LanguageCode.UNKNOWN: "Unknown",
    }
)


# ---------------------------------------------------------------------------
# Confidence-Based Language Decision Engine (FG4)
# ---------------------------------------------------------------------------


class LanguageConfidence(str, Enum):
    """Confidence band produced by the Language Decision Engine."""

    VERY_HIGH = "very_high"     # > 0.90
    LIKELY = "likely"           # 0.70 - 0.90
    MIXED = "mixed"             # 0.50 - 0.70
    UNKNOWN = "unknown"         # < 0.50


# Thresholds taken verbatim from the FG4 decision rules.
CONFIDENCE_VERY_HIGH: Final[float] = 0.90
CONFIDENCE_LIKELY: Final[float] = 0.70
CONFIDENCE_MIXED: Final[float] = 0.50


def classify_confidence(score: float) -> LanguageConfidence:
    """Map a detection confidence score to its FG4 confidence band."""
    if score > CONFIDENCE_VERY_HIGH:
        return LanguageConfidence.VERY_HIGH
    if score >= CONFIDENCE_LIKELY:
        return LanguageConfidence.LIKELY
    if score >= CONFIDENCE_MIXED:
        return LanguageConfidence.MIXED
    return LanguageConfidence.UNKNOWN


# ---------------------------------------------------------------------------
# Reply-language policy (FG4 Language Policy Engine)
# ---------------------------------------------------------------------------


class LanguagePolicyMode(str, Enum):
    """How the assistant chooses its reply language."""

    ADMIN = "admin"       # Reply in detected conversation language
    NORMAL = "normal"     # Reply in configured default language
    SMART = "smart"       # Follow dominant language, respect explicit requests


DEFAULT_POLICY_MODE: Final[LanguagePolicyMode] = LanguagePolicyMode.SMART


class ConversationStyle(str, Enum):
    """User conversation-style preference (persistent profile)."""

    FORMAL = "formal"
    CASUAL = "casual"
    MIXED = "mixed"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def capabilities_for(language: LanguageCode) -> LanguageCapabilities:
    """Return capabilities for a language, defaulting to all-False if unknown."""
    return LANGUAGE_CAPABILITIES.get(
        language, LanguageCapabilities(False, False, False, False)
    )


def supports_stt(language: LanguageCode) -> bool:
    return capabilities_for(language).stt


def supports_tts(language: LanguageCode) -> bool:
    return capabilities_for(language).tts


def supports_translation(language: LanguageCode) -> bool:
    return capabilities_for(language).translation


def supports_emotion(language: LanguageCode) -> bool:
    return capabilities_for(language).emotion


def display_name(language: LanguageCode) -> str:
    return LANGUAGE_DISPLAY_NAMES.get(language, language.value)


def is_supported(code: str) -> bool:
    """Return True if a raw string code maps to a supported language."""
    return code in {lang.value for lang in LanguageCode if lang is not LanguageCode.UNKNOWN}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "LanguageCode",
    "DEFAULT_LANGUAGE",
    "PRIMARY_LANGUAGES",
    "CODE_MIXED_LANGUAGES",
    "LanguageCapabilities",
    "LANGUAGE_CAPABILITIES",
    "LANGUAGE_DISPLAY_NAMES",
    "LanguageConfidence",
    "CONFIDENCE_VERY_HIGH",
    "CONFIDENCE_LIKELY",
    "CONFIDENCE_MIXED",
    "classify_confidence",
    "LanguagePolicyMode",
    "DEFAULT_POLICY_MODE",
    "ConversationStyle",
    "capabilities_for",
    "supports_stt",
    "supports_tts",
    "supports_translation",
    "supports_emotion",
    "display_name",
    "is_supported",
]
