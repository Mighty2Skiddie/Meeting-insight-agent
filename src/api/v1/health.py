"""
Health and readiness probe endpoints.
/health  — liveness (process alive?)
/readiness — readiness (can it serve traffic?)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.session import get_db
from src.observability.logging import get_logger
from src.resilience import breakers
from src.schemas import HealthResponse, ReadinessResponse
from src.services.cost_tracker import CostTracker

log = get_logger(__name__)
settings = get_settings()
_start_time = time.monotonic()

router = APIRouter(tags=["Operational"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        uptime_seconds=round(time.monotonic() - _start_time, 1),
        version=settings.app_version,
    )


@router.get("/readiness", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(session: AsyncSession = Depends(get_db)) -> ReadinessResponse:
    checks: dict[str, object] = {}
    overall = "ready"

    # ── Database ─────────────────────────────────────────────────────────────
    try:
        t = time.monotonic()
        await session.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "latency_ms": round((time.monotonic() - t) * 1000, 2)}
    except Exception as exc:
        checks["database"] = {"status": "error", "detail": str(exc)[:100]}
        overall = "not_ready"

    # ── Circuit Breaker States ────────────────────────────────────────────────
    for name, breaker in breakers.items():
        state = breaker.current_state
        checks[f"{name}_api"] = {
            "status": "ok" if state == "closed" else "degraded",
            "circuit": state,
        }
        if state == "open":
            overall = "degraded" if overall == "ready" else overall

    # ── Budget ───────────────────────────────────────────────────────────────
    try:
        tracker = CostTracker(session)
        remaining = await tracker.get_remaining()
        checks["budget"] = {
            "status": "ok" if remaining > settings.budget_reserve_usd else "low",
            "remaining_usd": round(remaining, 4),
            "active_tier": "premium" if remaining > settings.budget_reserve_usd else "free",
        }
    except Exception:
        checks["budget"] = {"status": "unknown"}

    # ── API Key Presence ──────────────────────────────────────────────────────
    checks["api_keys"] = {
        "openai": "configured" if settings.has_openai_key else "missing",
        "groq": "configured" if settings.has_groq_key else "missing",
        "gemini": "configured" if settings.has_gemini_key else "missing",
    }
    if not settings.has_openai_key and not settings.has_groq_key:
        overall = "not_ready"

    return ReadinessResponse(status=overall, checks=checks)
