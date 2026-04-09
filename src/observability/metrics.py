"""Custom Prometheus metrics for the meeting pipeline."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator



meetings_processed_total = Counter(
    "meetings_processed_total",
    "Total meetings processed by final status",
    ["status"],
)

meeting_processing_duration = Histogram(
    "meeting_processing_duration_seconds",
    "End-to-end pipeline processing time",
    buckets=[10, 30, 60, 120, 300, 600],
)

transcription_duration = Histogram(
    "transcription_duration_seconds",
    "STT transcription time by provider",
    ["provider"],
    buckets=[5, 10, 30, 60, 120, 300],
)

analysis_duration = Histogram(
    "analysis_duration_seconds",
    "LLM analysis time by provider",
    ["provider"],
    buckets=[2, 5, 10, 20, 40, 60],
)

circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state per service (0=closed, 1=half_open, 2=open)",
    ["service"],
)

fallback_activations_total = Counter(
    "fallback_activations_total",
    "Number of times a fallback provider was activated",
    ["service", "tier"],
)

budget_spent_usd = Gauge(
    "budget_spent_usd",
    "Cumulative OpenAI API spend in USD",
)

budget_remaining_usd = Gauge(
    "budget_remaining_usd",
    "Remaining OpenAI budget in USD",
)

provider_tier_requests_total = Counter(
    "provider_tier_requests_total",
    "Requests served by provider tier",
    ["tier", "operation"],  # tier: premium|free|offline, operation: stt|llm
)

cost_per_meeting = Histogram(
    "cost_per_meeting_usd",
    "Distribution of per-meeting API costs in USD",
    buckets=[0.0, 0.05, 0.10, 0.20, 0.30, 0.50, 1.00],
)

active_background_jobs = Gauge(
    "active_background_jobs",
    "Number of currently running background processing jobs",
)


def setup_metrics(app: object) -> Instrumentator:  # type: ignore[return]
    """Attach Prometheus instrumentation to the FastAPI app."""
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        should_instrument_requests_inprogress=True,
        excluded_handlers=["/health", "/metrics"],
        body_handlers=[],
    )
    instrumentator.instrument(app)  # type: ignore[arg-type]
    return instrumentator
