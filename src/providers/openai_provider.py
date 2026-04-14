"""OpenAI Whisper (STT) + GPT-4o-mini (LLM). Tier 1: paid, highest quality."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, RateLimitError

from src.config import get_settings
from src.observability.logging import get_logger
from src.providers.base import AnalysisResult, LLMProvider, STTProvider, TranscriptionResult
from src.resilience import TransientError, call_with_retry
from src.utils.prompts import MEETING_ANALYST_SYSTEM_PROMPT, build_user_prompt

log = get_logger(__name__)
settings = get_settings()

# Pricing (2024 rates, USD)
WHISPER_COST_PER_MINUTE = 0.006
GPT4O_MINI_COST_PER_1K_INPUT = 0.00015   # $0.15 / 1M tokens
GPT4O_MINI_COST_PER_1K_OUTPUT = 0.00060  # $0.60 / 1M tokens
ESTIMATED_OUTPUT_TOKENS = 800


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in {429, 500, 502, 503}:
        return True
    if isinstance(exc, APIConnectionError):
        return True
    return False


class OpenAISTTProvider(STTProvider):
    """OpenAI Whisper API — gold-standard transcription with timestamps."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            http_client=httpx.AsyncClient(timeout=settings.request_timeout_seconds),
        )

    @property
    def name(self) -> str:
        return "openai_whisper"

    @property
    def cost_per_minute(self) -> float:
        return WHISPER_COST_PER_MINUTE

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        async def _call() -> TranscriptionResult:
            try:
                with open(audio_path, "rb") as f:
                    response = await self._client.audio.transcriptions.create(
                        model=settings.openai_stt_model,
                        file=f,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                    )

                duration = float(getattr(response, "duration", 0.0) or 0.0)
                segments = []
                for i, seg in enumerate(getattr(response, "segments", []) or []):
                    segments.append({
                        "speaker": f"Speaker {(i % 3) + 1}",  # pseudo-diarization
                        "start": round(float(seg.start), 2),
                        "end": round(float(seg.end), 2),
                        "text": seg.text.strip(),
                    })

                cost = (duration / 60.0) * WHISPER_COST_PER_MINUTE
                log.info(
                    "openai_whisper_success",
                    duration_s=duration,
                    segments=len(segments),
                    cost_usd=round(cost, 4),
                )
                return TranscriptionResult(
                    full_text=response.text,
                    segments=segments,
                    duration_seconds=duration,
                    language=getattr(response, "language", "en") or "en",
                    provider=self.name,
                    cost_usd=cost,
                )
            except Exception as exc:
                if _is_transient(exc):
                    raise TransientError(str(exc)) from exc
                raise

        result = await call_with_retry(_call, service_name="openai")
        return result  # type: ignore[return-value]


class OpenAILLMProvider(LLMProvider):
    """
    GPT-4o-mini with structured outputs.
    response_format=json_schema guarantees the output matches MeetingInsights schema.
    """

    def __init__(self) -> None:
        from src.schemas import MeetingInsights

        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            http_client=httpx.AsyncClient(timeout=settings.request_timeout_seconds),
        )
        # Build the JSON schema from the Pydantic model, then fix for OpenAI
        raw_schema = MeetingInsights.model_json_schema()
        self._json_schema = self._fix_schema_for_openai(raw_schema)

    @staticmethod
    def _fix_schema_for_openai(schema: dict) -> dict:
        """Recursively add 'additionalProperties': false to all objects.

        OpenAI's structured output API requires this on EVERY object
        in the JSON schema. Pydantic doesn't include it by default.
        Also resolves $defs references inline.
        """
        import copy
        schema = copy.deepcopy(schema)
        defs = schema.pop("$defs", {})

        def _resolve_and_fix(node: dict) -> dict:
            # Resolve $ref → inline the definition
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                if ref_name in defs:
                    node = copy.deepcopy(defs[ref_name])
                else:
                    return node

            # If this is an object type, add additionalProperties: false
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                # Recurse into properties
                for prop_name, prop_schema in node.get("properties", {}).items():
                    node["properties"][prop_name] = _resolve_and_fix(prop_schema)

            # Handle arrays — fix the items schema
            if node.get("type") == "array" and "items" in node:
                node["items"] = _resolve_and_fix(node["items"])

            # Handle anyOf / oneOf (Pydantic uses these for Optional fields)
            for key in ("anyOf", "oneOf"):
                if key in node:
                    node[key] = [_resolve_and_fix(opt) for opt in node[key]]

            return node

        schema = _resolve_and_fix(schema)
        return schema

    @property
    def name(self) -> str:
        return "gpt_4o_mini"

    @property
    def cost_per_1k_tokens(self) -> float:
        return GPT4O_MINI_COST_PER_1K_INPUT

    async def analyze(self, transcript: str, duration_seconds: float) -> AnalysisResult:
        async def _call() -> AnalysisResult:
            try:
                response = await self._client.chat.completions.create(
                    model=settings.openai_llm_model,
                    messages=[
                        {"role": "system", "content": MEETING_ANALYST_SYSTEM_PROMPT},
                        {"role": "user", "content": build_user_prompt(transcript, duration_seconds)},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "meeting_insights",
                            "schema": self._json_schema,
                            "strict": True,
                        },
                    },
                    temperature=0.2,
                    max_tokens=2048,
                )

                usage = response.usage
                input_tokens = usage.prompt_tokens if usage else 0
                output_tokens = usage.completion_tokens if usage else ESTIMATED_OUTPUT_TOKENS
                cost = (
                    (input_tokens / 1000) * GPT4O_MINI_COST_PER_1K_INPUT
                    + (output_tokens / 1000) * GPT4O_MINI_COST_PER_1K_OUTPUT
                )

                raw = response.choices[0].message.content or "{}"
                insights = json.loads(raw)

                log.info(
                    "gpt4o_mini_success",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=round(cost, 4),
                )
                return AnalysisResult(
                    insights=insights,
                    provider=self.name,
                    cost_usd=cost,
                )
            except Exception as exc:
                if _is_transient(exc):
                    raise TransientError(str(exc)) from exc
                raise

        result = await call_with_retry(_call, service_name="openai")
        return result  # type: ignore[return-value]
