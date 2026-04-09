"""Gemini Flash 2.0 LLM fallback. Tier 3: free, 1M token context window."""
from __future__ import annotations

import json

import google.generativeai as genai
from google.api_core.exceptions import GoogleAPIError, ResourceExhausted, ServiceUnavailable

from src.config import get_settings
from src.observability.logging import get_logger
from src.providers.base import AnalysisResult, LLMProvider
from src.providers.groq_provider import _extract_partial_json
from src.resilience import TransientError, call_with_retry
from src.utils.prompts import MEETING_ANALYST_SYSTEM_PROMPT, build_user_prompt

log = get_logger(__name__)
settings = get_settings()


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (ResourceExhausted, ServiceUnavailable, GoogleAPIError))


class GeminiLLMProvider(LLMProvider):
    """Gemini 2.0 Flash — free tier, 1M token context."""

    def __init__(self) -> None:
        genai.configure(api_key=settings.gemini_api_key)
        self._model = genai.GenerativeModel(
            model_name=settings.gemini_llm_model,
            system_instruction=MEETING_ANALYST_SYSTEM_PROMPT,
        )

    @property
    def name(self) -> str:
        return "gemini_2_0_flash"

    @property
    def cost_per_1k_tokens(self) -> float:
        return 0.0  # Free tier

    async def analyze(self, transcript: str, duration_seconds: float) -> AnalysisResult:
        async def _call() -> AnalysisResult:
            try:
                prompt = build_user_prompt(transcript, duration_seconds)
                # Instruct JSON output explicitly in the prompt
                prompt += "\n\nRespond ONLY with a valid JSON object. No markdown, no explanation."

                response = await self._model.generate_content_async(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.2,
                        max_output_tokens=2048,
                        response_mime_type="application/json",
                    ),
                )
                raw = response.text or "{}"
                try:
                    insights = json.loads(raw)
                except json.JSONDecodeError:
                    insights = _extract_partial_json(raw)

                log.info("gemini_flash_success", provider=self.name)
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

        result = await call_with_retry(_call, service_name="gemini")
        return result  # type: ignore[return-value]
