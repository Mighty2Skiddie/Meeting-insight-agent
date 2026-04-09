"""
Provider Router — cost-aware, circuit-breaker-gated provider selection.
Implements the 4-tier provider priority matrix from the architecture plan.
"""
from __future__ import annotations

from src.resilience import CircuitOpenError, breakers

from src.config import get_settings
from src.observability.logging import get_logger
from src.observability.metrics import fallback_activations_total, provider_tier_requests_total
from src.providers import (
    AnalysisResult,
    GeminiLLMProvider,
    GroqLLMProvider,
    GroqSTTProvider,
    LLMProvider,
    OpenAILLMProvider,
    OpenAISTTProvider,
    RuleBasedProvider,
    STTProvider,
    TranscriptionResult,
)
from src.services.cost_tracker import CostTracker

log = get_logger(__name__)
settings = get_settings()


class ProviderRouter:
    """
    Selects the best available provider for each operation based on:
    1. Budget availability (premium → free tier)
    2. Circuit breaker state (avoid degraded services)
    3. Configured fallback priority order
    """

    def __init__(self, cost_tracker: CostTracker) -> None:
        self._cost_tracker = cost_tracker

        # STT provider chain: [OpenAI] → [Groq] (no local fallback — cloud-only)
        self._stt_chain: list[STTProvider] = []
        if settings.has_openai_key:
            self._stt_chain.append(OpenAISTTProvider())
        if settings.has_groq_key:
            self._stt_chain.append(GroqSTTProvider())

        # LLM provider chain: [OpenAI] → [Gemini] → [Groq LLM] → [Rule-based]
        self._llm_chain: list[LLMProvider] = []
        if settings.has_openai_key:
            self._llm_chain.append(OpenAILLMProvider())
        if settings.has_gemini_key:
            self._llm_chain.append(GeminiLLMProvider())
        if settings.has_groq_key:
            self._llm_chain.append(GroqLLMProvider())
        self._llm_chain.append(RuleBasedProvider())  # Always available

    def _breaker_closed(self, service: str) -> bool:
        b = breakers.get(service)
        if b is None:
            return True
        return b.current_state != "open"

    async def _is_premium_ok(self) -> bool:
        return await self._cost_tracker.is_premium_available()

    async def transcribe(self, audio_path: str, meeting_id: str) -> TranscriptionResult:
        """Try STT providers in order, skip degraded/budget-exhausted ones."""
        budget_ok = await self._is_premium_ok()
        last_exc: Exception | None = None

        for provider in self._stt_chain:
            is_openai = "openai" in provider.name
            service_key = "openai" if is_openai else "groq"

            # Skip premium if budget guard is active
            if is_openai and not budget_ok:
                log.info("stt_skip_budget", provider=provider.name)
                fallback_activations_total.labels(service="stt", tier="budget_guard").inc()
                continue

            # Skip if circuit is open
            if not self._breaker_closed(service_key):
                log.info("stt_skip_circuit_open", provider=provider.name)
                fallback_activations_total.labels(service="stt", tier="circuit_open").inc()
                continue

            try:
                log.info("stt_attempt", provider=provider.name)
                result = await provider.transcribe(audio_path)
                tier = "premium" if is_openai else "free"
                provider_tier_requests_total.labels(tier=tier, operation="stt").inc()

                if result.cost_usd > 0:
                    await self._cost_tracker.record_cost(
                        meeting_id=meeting_id,
                        provider=result.provider,
                        operation="transcription",
                        input_units=round(result.duration_seconds / 60, 3),
                        unit_type="minutes",
                        cost_usd=result.cost_usd,
                    )
                return result
            except (CircuitOpenError, Exception) as exc:
                last_exc = exc  # type: ignore[assignment]
                log.warning("stt_provider_failed", provider=provider.name, error=str(exc))
                continue

        raise RuntimeError(
            f"All STT providers failed. Last error: {last_exc}"
        ) from last_exc

    async def analyze(self, transcript: str, duration_seconds: float, meeting_id: str) -> AnalysisResult:
        """Try LLM providers in order, fallback to rule-based if everything fails."""
        budget_ok = await self._is_premium_ok()
        last_exc: Exception | None = None

        for provider in self._llm_chain:
            is_openai = "openai" in provider.name or "gpt" in provider.name
            is_rule_based = provider.name == "rule_based_engine"
            service_key = "openai" if is_openai else ("gemini" if "gemini" in provider.name else "groq")

            # Skip premium if budget guard
            if is_openai and not budget_ok:
                log.info("llm_skip_budget", provider=provider.name)
                fallback_activations_total.labels(service="llm", tier="budget_guard").inc()
                continue

            # Skip cloud providers with open circuits (rule-based always allowed)
            if not is_rule_based and not self._breaker_closed(service_key):
                log.info("llm_skip_circuit_open", provider=provider.name)
                fallback_activations_total.labels(service="llm", tier="circuit_open").inc()
                continue

            try:
                log.info("llm_attempt", provider=provider.name)
                result = await provider.analyze(transcript, duration_seconds)
                tier = "offline" if is_rule_based else ("premium" if is_openai else "free")
                provider_tier_requests_total.labels(tier=tier, operation="llm").inc()

                if result.cost_usd > 0:
                    await self._cost_tracker.record_cost(
                        meeting_id=meeting_id,
                        provider=result.provider,
                        operation="analysis",
                        input_units=len(transcript.split()),
                        unit_type="tokens",
                        cost_usd=result.cost_usd,
                    )
                return result
            except (CircuitOpenError, Exception) as exc:
                last_exc = exc  # type: ignore[assignment]
                log.warning("llm_provider_failed", provider=provider.name, error=str(exc))
                continue

        # Final safety net — rule-based should never raise, but just in case:
        raise RuntimeError(
            f"All LLM providers including rule-based failed. Last error: {last_exc}"
        ) from last_exc
