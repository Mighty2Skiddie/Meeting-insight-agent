"""
Unit tests for the provider router — budget guard and fallback logic.
All external API calls are mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.providers.base import AnalysisResult, TranscriptionResult


@pytest.mark.asyncio
async def test_router_uses_openai_when_budget_available(test_session):
    from src.services.cost_tracker import CostTracker
    from src.services.provider_router import ProviderRouter

    tracker = CostTracker(test_session)
    router = ProviderRouter(tracker)

    mock_result = TranscriptionResult(
        full_text="Hello world", segments=[], duration_seconds=60.0,
        language="en", provider="openai_whisper", cost_usd=0.006,
    )

    with patch.object(router._cost_tracker, "is_premium_available", new=AsyncMock(return_value=True)):
        with patch.object(router._stt_chain[0], "transcribe", new=AsyncMock(return_value=mock_result)):
            result = await router.transcribe("fake_path.mp3", "meeting-123")

    assert result.provider == "openai_whisper"
    assert result.cost_usd == 0.006


@pytest.mark.asyncio
async def test_router_falls_back_to_groq_on_circuit_open(test_session):
    from src.services.cost_tracker import CostTracker
    from src.services.provider_router import ProviderRouter
    from src.resilience import breakers, CBState

    tracker = CostTracker(test_session)
    router = ProviderRouter(tracker)

    # Force the openai circuit breaker into OPEN state directly
    breakers["openai"]._state = CBState.OPEN
    breakers["openai"]._opened_at = 0.0  # opened long ago — will not auto-reset

    mock_groq = TranscriptionResult(
        full_text="Groq transcript", segments=[], duration_seconds=30.0,
        language="en", provider="groq_whisper_v3", cost_usd=0.0, degraded=True,
    )
    if len(router._stt_chain) > 1:
        with patch.object(router._stt_chain[1], "transcribe", new=AsyncMock(return_value=mock_groq)):
            result = await router.transcribe("fake.mp3", "meeting-456")
        assert result.provider == "groq_whisper_v3"

    # Reset breaker back to CLOSED for other tests
    breakers["openai"]._state = CBState.CLOSED
    breakers["openai"]._failure_count = 0


@pytest.mark.asyncio
async def test_rule_based_always_returns_insights(test_session):
    from src.providers.rule_engine import RuleBasedProvider

    provider = RuleBasedProvider()
    result = await provider.analyze(
        "Alice: We should launch by Friday. Bob: Agreed. Let's do it.",
        duration_seconds=120.0,
    )
    assert result.provider == "rule_based_engine"
    assert "summary" in result.insights
    assert "action_items" in result.insights
    assert result.cost_usd == 0.0


@pytest.mark.asyncio
async def test_cost_tracker_records_and_updates_remaining(test_session):
    from src.services.cost_tracker import CostTracker

    tracker = CostTracker(test_session)
    remaining_before = await tracker.get_remaining()

    await tracker.record_cost(
        meeting_id="test-meeting",
        provider="openai_whisper",
        operation="transcription",
        input_units=5.0,
        unit_type="minutes",
        cost_usd=0.03,
    )

    remaining_after = await tracker.get_remaining()
    assert abs((remaining_before - 0.03) - remaining_after) < 0.001
