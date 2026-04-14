"""
Local faster-whisper transcriber for Phase 2 — raw audio capture path.

Runs entirely offline. No API keys needed. Supports 100+ languages.
Uses Silero VAD to only transcribe speech (silence is skipped), preventing
Whisper from hallucinating text from background noise.

Usage:
    transcriber = StreamTranscriber(model_size="base", language=None)
    await transcriber.load()
    segments = await transcriber.process_chunk(pcm_bytes)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.observability.logging import get_logger

log = get_logger(__name__)

# PCM audio constants
SAMPLE_RATE = 16_000        # 16kHz — Whisper's native rate
BYTES_PER_SAMPLE = 2        # 16-bit PCM
MAX_BUFFER_SECONDS = 30     # Force transcribe if buffer exceeds this
VAD_THRESHOLD = 0.5         # Silero speech detection confidence threshold
VAD_CHUNK_SAMPLES = 512     # Silero processes 512 samples at a time (16kHz)


@dataclass
class LiveSegment:
    text: str
    language: str
    start_seconds: float
    end_seconds: float
    confidence: float = 1.0
    is_partial: bool = False


@dataclass
class _SpeechBuffer:
    """Accumulates PCM samples from speech-active periods."""
    samples: list[np.ndarray] = field(default_factory=list)
    start_offset_seconds: float = 0.0

    def append(self, chunk: np.ndarray) -> None:
        self.samples.append(chunk)

    def to_numpy(self) -> np.ndarray:
        if not self.samples:
            return np.array([], dtype=np.float32)
        return np.concatenate(self.samples)

    def duration_seconds(self) -> float:
        total_samples = sum(len(s) for s in self.samples)
        return total_samples / SAMPLE_RATE

    def clear(self) -> None:
        self.samples.clear()


class StreamTranscriber:
    """
    Real-time speech transcription using faster-whisper + Silero VAD.

    Args:
        model_size: Whisper model. Options:
            "tiny"    — fastest, ~75MB RAM, lower accuracy
            "base"    — balanced, ~150MB RAM             ← default
            "small"   — best CPU accuracy, ~500MB RAM
            "medium"  — higher accuracy, ~1.5GB RAM (needs 8GB+ RAM)
        language: ISO 639-1 code (e.g. "en", "hi", "fr") or None for auto-detect.
                  Auto-detect adds ~0.2s per segment but supports 100+ languages.
    """

    def __init__(self, model_size: str = "base", language: str | None = None) -> None:
        self._model_size = model_size
        self._language = language
        self._model: Any = None
        self._vad_model: Any = None
        self._vad_utils: Any = None
        self._buffer = _SpeechBuffer()
        self._total_audio_seconds = 0.0
        self._loaded = False

    async def load(self) -> None:
        """Load models in a thread pool (both are CPU-bound IO operations)."""
        await asyncio.to_thread(self._load_models)
        self._loaded = True
        log.info(
            "stream_transcriber_ready",
            model_size=self._model_size,
            language=self._language or "auto-detect",
        )

    def _load_models(self) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper not installed. "
                "Run: pip install faster-whisper"
            ) from e

        try:
            import torch  # type: ignore[import-not-found]
            vad_model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
            )
            self._vad_model = vad_model
            self._vad_utils = utils
            log.info("silero_vad_loaded")
        except Exception as exc:
            log.warning("silero_vad_load_failed", error=str(exc), fallback="energy_vad")
            self._vad_model = None

        # Load Whisper on CPU with int8 quantization for speed
        self._model = WhisperModel(
            self._model_size,
            device="cpu",
            compute_type="int8",
        )
        log.info("faster_whisper_loaded", model_size=self._model_size)

    async def process_chunk(self, pcm_bytes: bytes) -> list[LiveSegment]:
        """
        Process one chunk of raw 16kHz mono 16-bit PCM audio.

        Returns transcribed segments if a speech utterance completed,
        empty list if still accumulating or silence detected.
        """
        if not self._loaded:
            raise RuntimeError("Call load() before process_chunk()")

        # Convert bytes → float32 numpy array normalised to [-1, 1]
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._total_audio_seconds += len(samples) / SAMPLE_RATE

        has_speech = await asyncio.to_thread(self._run_vad, samples)

        if has_speech:
            self._buffer.append(samples)
        elif self._buffer.duration_seconds() > 0:
            # Speech just ended — transcribe the accumulated buffer
            result = await asyncio.to_thread(self._transcribe_buffer)
            self._buffer.clear()
            return result

        # Force transcribe if buffer is too long (prevents memory bloat)
        if self._buffer.duration_seconds() >= MAX_BUFFER_SECONDS:
            log.debug("force_transcribing_max_buffer")
            result = await asyncio.to_thread(self._transcribe_buffer)
            self._buffer.clear()
            return result

        return []

    async def flush(self) -> list[LiveSegment]:
        """Transcribe any remaining audio in the buffer (call at meeting end)."""
        if self._buffer.duration_seconds() == 0:
            return []
        result = await asyncio.to_thread(self._transcribe_buffer)
        self._buffer.clear()
        return result

    def _run_vad(self, samples: np.ndarray) -> bool:
        """Returns True if speech detected in the audio chunk."""
        if self._vad_model is None:
            # Fallback: simple energy-based VAD
            rms = float(np.sqrt(np.mean(samples ** 2)))
            return rms > 0.01

        try:
            import torch  # type: ignore[import-not-found]
            # Silero expects chunks of exactly 512 samples at 16kHz
            confidences: list[float] = []
            for i in range(0, len(samples) - VAD_CHUNK_SAMPLES, VAD_CHUNK_SAMPLES):
                chunk = torch.from_numpy(samples[i : i + VAD_CHUNK_SAMPLES])
                conf = float(self._vad_model(chunk, SAMPLE_RATE).item())
                confidences.append(conf)

            if not confidences:
                return False
            return max(confidences) >= VAD_THRESHOLD
        except Exception as exc:
            log.warning("vad_error", error=str(exc))
            return True  # Fail open — transcribe everything if VAD breaks

    def _transcribe_buffer(self) -> list[LiveSegment]:
        """Run faster-whisper on the accumulated speech buffer."""
        audio = self._buffer.to_numpy()
        if len(audio) == 0:
            return []

        start_offset = self._total_audio_seconds - len(audio) / SAMPLE_RATE

        try:
            segments_iter, info = self._model.transcribe(
                audio,
                language=self._language,     # None = auto-detect
                task="transcribe",
                beam_size=3,                 # Speed/accuracy trade-off for real-time
                vad_filter=False,            # We already filtered with Silero
                word_timestamps=False,
            )

            results: list[LiveSegment] = []
            for seg in segments_iter:
                text = seg.text.strip()
                if not text:
                    continue
                results.append(LiveSegment(
                    text=text,
                    language=info.language,
                    start_seconds=start_offset + seg.start,
                    end_seconds=start_offset + seg.end,
                    confidence=getattr(seg, "no_speech_prob", 0.0),
                ))
                log.debug("live_segment", text=text, language=info.language)

            return results

        except Exception as exc:
            log.error("transcribe_error", error=str(exc))
            return []

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def total_audio_seconds(self) -> float:
        return self._total_audio_seconds
