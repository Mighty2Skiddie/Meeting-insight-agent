"""
Budget monitoring endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db
from src.schemas import BudgetResponse
from src.services.cost_tracker import CostTracker

router = APIRouter(tags=["Budget"])


@router.get(
    "/budget",
    response_model=BudgetResponse,
    summary="Real-time API cost and budget tracking",
    description="Shows cumulative OpenAI spend, remaining budget, per-provider breakdown, and estimated remaining meetings.",
)
async def get_budget(session: AsyncSession = Depends(get_db)) -> BudgetResponse:
    tracker = CostTracker(session)
    summary = await tracker.get_budget_summary()
    return BudgetResponse(**summary)  # type: ignore[arg-type]
