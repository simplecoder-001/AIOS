# app/core/event_bus/events/voice_events.py

"""
Voice events for AIOS.

These events power the entire FG1 Voice Interaction System:

Audio Capture
    ↓
Wake Word Detection
    ↓
Speaker Verification
    ↓
Continuous Verification
    ↓
Voice Activity Detection
    ↓
Streaming STT
    ↓
Interrupt Detection
    ↓
Text-to-Speech

Design goals:
    - Immutable events
    - Thread-safe transport
    - Low allocation overhead
    - Event sourcing friendly
    - Supports streaming audio pipelines
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class VoiceEventType(StrEnum):
    """
    Canonical voice event names.
    """

    # Audio
    AUDIO_CAPTURE_STARTED = "voice.audio.started"
    AUDIO_CAPTURE_STOPPED = "voice.audio.stopped"
    AUDIO_CHUNK_RECEIVED = "voice.audio.chunk"

    # Wake Word
    WAKE_WORD_DETECTED = "voice.wake.detected"
    WAKE_WORD_TIMEOUT = "voice.wake.timeout"

    # Speaker Verification
    SPEAKER_VERIFICATION_STARTED = (
        "voice.verification.started"
    )
    SPEAKER_VERIFIED = "voice.verification.succeeded"
    SPEAKER_REJECTED = "voice.verification.rejected"

    # Continuous Verification
    IDENTITY_MONITOR_STARTED = (
        "voice.identity_monitor.started"
    )
    IDENTITY_CHANGED = "voice.identity.changed"

    # Voice Activity Detection
    SPEECH_STARTED = "voice.speech.started"
    SPEECH_ENDED = "voice.speech.ended"

    # STT
    TRANSCRIPTION_STARTED = "voice.stt.started"
    TRANSCRIPTION_PARTIAL = "voice.stt.partial"
    TRANSCRIPTION_FINAL = "voice.stt.final"
    TRANSCRIPTION_FAILED = "voice.stt.failed"

    # Interrupt
    INTERRUPT_DETECTED = "voice.interrupt.detected"

    # TTS
    TTS_STARTED = "voice.tts.started"
    TTS_CHUNK_GENERATED = "voice.tts.chunk"
    TTS_FINISHED = "voice.tts.finished"
    TTS_INTERRUPTED = "voice.tts.interrupted"

    # Errors
    VOICE_PIPELINE_ERROR = "voice.pipeline.error"


@dataclass(slots=True, kw_only=True)
class VoiceEvent(Event):
    """
    Base voice event.

    Shared metadata used throughout the
    real-time voice processing pipeline.
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.VOICE,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None

    session_id: str | None = None


# ============================================================================
# Audio Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class AudioCaptureStartedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.AUDIO_CAPTURE_STARTED,
        init=False,
    )

    sample_rate: int
    channels: int
    device_name: str | None = None


@dataclass(slots=True, kw_only=True)
class AudioCaptureStoppedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.AUDIO_CAPTURE_STOPPED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class AudioChunkReceivedEvent(VoiceEvent):
    """
    Broadcast audio chunk.

    Audio payload is intentionally typed as bytes
    so implementations remain framework agnostic
    (numpy array, PCM bytes, memoryview, etc.).
    """

    name: str = field(
        default=VoiceEventType.AUDIO_CHUNK_RECEIVED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    audio: bytes
    sample_rate: int
    channels: int
    chunk_id: int
    duration_ms: float


# ============================================================================
# Wake Word Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class WakeWordDetectedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.WAKE_WORD_DETECTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    wake_word: str
    confidence: float


@dataclass(slots=True, kw_only=True)
class WakeWordTimeoutEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.WAKE_WORD_TIMEOUT,
        init=False,
    )


# ============================================================================
# Speaker Verification Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SpeakerVerificationStartedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.SPEAKER_VERIFICATION_STARTED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class SpeakerVerifiedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.SPEAKER_VERIFIED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    speaker_id: str
    confidence: float


@dataclass(slots=True, kw_only=True)
class SpeakerRejectedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.SPEAKER_REJECTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    confidence: float | None = None
    reason: str | None = None


# ============================================================================
# Continuous Verification Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class IdentityMonitorStartedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.IDENTITY_MONITOR_STARTED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class IdentityChangedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.IDENTITY_CHANGED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    previous_identity: str | None = None
    current_identity: str | None = None
    confidence: float | None = None


# ============================================================================
# Voice Activity Detection Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SpeechStartedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.SPEECH_STARTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class SpeechEndedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.SPEECH_ENDED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    duration_ms: float | None = None


# ============================================================================
# STT Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class TranscriptionStartedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TRANSCRIPTION_STARTED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class TranscriptionPartialEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TRANSCRIPTION_PARTIAL,
        init=False,
    )

    text: str
    language: str | None = None
    confidence: float | None = None


@dataclass(slots=True, kw_only=True)
class TranscriptionFinalEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TRANSCRIPTION_FINAL,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    text: str
    language: str | None = None
    confidence: float | None = None


@dataclass(slots=True, kw_only=True)
class TranscriptionFailedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TRANSCRIPTION_FAILED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    reason: str


# ============================================================================
# Interrupt Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class InterruptDetectedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.INTERRUPT_DETECTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    command: str


# ============================================================================
# Text-to-Speech Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class TTSStartedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TTS_STARTED,
        init=False,
    )

    text_length: int
    language: str | None = None
    voice: str | None = None


@dataclass(slots=True, kw_only=True)
class TTSChunkGeneratedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TTS_CHUNK_GENERATED,
        init=False,
    )

    chunk_id: int
    audio: bytes
    sample_rate: int


@dataclass(slots=True, kw_only=True)
class TTSFinishedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TTS_FINISHED,
        init=False,
    )

    duration_ms: float | None = None


@dataclass(slots=True, kw_only=True)
class TTSInterruptedEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.TTS_INTERRUPTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    reason: str | None = None


# ============================================================================
# Error Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class VoicePipelineErrorEvent(VoiceEvent):
    name: str = field(
        default=VoiceEventType.VOICE_PIPELINE_ERROR,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.CRITICAL,
        init=False,
    )

    stage: str
    message: str
    exception_type: str | None = None
    recoverable: bool = True


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "VoiceEventType",
    "VoiceEvent",
    "AudioCaptureStartedEvent",
    "AudioCaptureStoppedEvent",
    "AudioChunkReceivedEvent",
    "WakeWordDetectedEvent",
    "WakeWordTimeoutEvent",
    "SpeakerVerificationStartedEvent",
    "SpeakerVerifiedEvent",
    "SpeakerRejectedEvent",
    "IdentityMonitorStartedEvent",
    "IdentityChangedEvent",
    "SpeechStartedEvent",
    "SpeechEndedEvent",
    "TranscriptionStartedEvent",
    "TranscriptionPartialEvent",
    "TranscriptionFinalEvent",
    "TranscriptionFailedEvent",
    "InterruptDetectedEvent",
    "TTSStartedEvent",
    "TTSChunkGeneratedEvent",
    "TTSFinishedEvent",
    "TTSInterruptedEvent",
    "VoicePipelineErrorEvent",
]