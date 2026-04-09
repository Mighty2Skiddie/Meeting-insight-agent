"""Groq Whisper v3 (STT) + Llama 3.3 70B (LLM). Tier 2: free, fast inference."""
from __future__ import annotations

import json

import httpx
from groq import AsyncGroq, APIConnectionError, APIStatusError, RateLimitError

from src.config import get_settings
from src.observability.logging import get_logger
from src.providers.base import AnalysisResult, LLMProvider, STTProvider, TranscriptionResult
from src.resilience import TransientError, call_with_retry
from src.utils.prompts import MEETING_ANALYST_SYSTEM_PROMPT, build_user_prompt

log = get_logger(__name__)
settings = get_settings()


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in {429, 500, 502, 503}:
        return True
    if isinstance(exc, APIConnectionError):
        return True
    return False


class GroqSTTProvider(STTProvider):
    """Groq Whisper Large v3 — free tier, ~10x realtime speed."""

    def __init__(self) -> None:
        self._client = AsyncGroq(
            api_key=settings.groq_api_key,
            http_client=httpx.AsyncClient(timeout=settings.request_timeout_seconds),
        )

    @property
    def name(self) -> str:
        return "groq_whisper_v3"

    @property
    def cost_per_minute(self) -> float:
        return 0.0  # Free tier

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        async def _call() -> TranscriptionResult:
            try:
                with open(audio_path, "rb") as f:
                    response = await self._client.audio.transcriptions.create(
                        model=settings.groq_stt_model,
                        file=f,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                    )

                duration = float(getattr(response, "duration", 0.0) or 0.0)
                segments = []
                for i, seg in enumerate(getattr(response, "segments", []) or []):
                    segments.append({
                        "speaker": f"Speaker {(i % 3) + 1}",
                        "start": round(float(seg.get("start", 0)), 2),
                        "end": round(float(seg.get("end", 0)), 2),
                        "text": seg.get("text", "").strip(),
                    })

                log.info("groq_whisper_success", duration_s=duration, segments=len(segments))
                return TranscriptionResult(
                    full_text=response.text,
                    segments=segments,
                    duration_seconds=duration,
                    language=getattr(response, "language", "en") or "en",
                    provider=self.name,
                    cost_usd=0.0,
                    degraded=True,
                )
            except Exception as exc:
                if _is_transient(exc):
                    raise TransientError(str(exc)) from exc
                raise

        result = await call_with_retry(_call, service_name="groq")
        return result  # type: ignore[return-value]


class GroqLLMProvider(LLMProvider):
    """Groq Llama 3.3 70B — free tier, ultra-fast LLM inference."""

    def __init__(self) -> None:
        self._client = AsyncGroq(
            api_key=settings.groq_api_key,
            http_client=httpx.AsyncClient(timeout=settings.request_timeout_seconds),
        )

    @property
    def name(self) -> str:
        return "groq_llama_3_3_70b"

    @property
    def cost_per_1k_tokens(self) -> float:
        return 0.0

    async def analyze(self, transcript: str, duration_seconds: float) -> AnalysisResult:
        async def _call() -> AnalysisResult:
            try:
                response = await self._client.chat.completions.create(
                    model=settings.groq_llm_model,
                    messages=[
                        {"role": "system", "content": MEETING_ANALYST_SYSTEM_PROMPT},
                        {"role": "user", "content": build_user_prompt(transcript, duration_seconds)},
                    ],
                    temperature=0.2,
                    max_tokens=2048,
                    response_format={"type": "json_object"},
                )

                raw = response.choices[0].message.content or "{}"
                try:
                    insights = json.loads(raw)
                except json.JSONDecodeError:
                    insights = _extract_partial_json(raw)

                log.info("groq_llm_success", provider=self.name)
                return AnalysisResult(
                    insights=insights,
                    provider=self.name,
                    cost_usd=0.0,
                    degraded=True,
                )
            except Exception as exc:
                if _is_transient(exc):
                    raise TransientError(str(exc)) from exc
                raise

        result = await call_with_retry(_call, service_name="groq")
        return result  # type: ignore[return-value]


def _extract_partial_json(raw: str) -> dict[str, object]:
    """Best-effort extraction if LLM outputs text around JSON."""
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end])  # type: ignore[return-value]
        except json.JSONDecodeError:
            pass
    return {"summary": raw[:500], "key_decisions": [], "action_items": []}
