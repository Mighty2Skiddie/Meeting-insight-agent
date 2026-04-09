"""Budget enforcement and cost audit log."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import CostLedger
from src.db.repository import CostRepository
from src.observability.logging import get_logger
from src.observability.metrics import budget_remaining_usd, budget_spent_usd

log = get_logger(__name__)
settings = get_settings()


class CostTracker:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = CostRepository(session)

    async def get_total_spent(self) -> float:
        return await self._repo.get_total_spent()

    async def get_remaining(self) -> float:
        spent = await self._repo.get_total_spent()
        return max(0.0, settings.budget_limit_usd - spent)

    async def is_premium_available(self) -> bool:
        """Returns True if enough budget remains to use OpenAI premium tier."""
        remaining = await self.get_remaining()
        return remaining > settings.budget_reserve_usd

    async def record_cost(
        self,
        meeting_id: str,
        provider: str,
        operation: str,
        input_units: float,
        unit_type: str,
        cost_usd: float,
    ) -> None:
        """
        Record an API cost entry and update Prometheus budget metrics.
        """
        spent = await self._repo.get_total_spent()
        cumulative = spent + cost_usd
        remaining = max(0.0, settings.budget_limit_usd - cumulative)

        entry = CostLedger(
            meeting_id=meeting_id,
            provider=provider,
            operation=operation,
            input_units=input_units,
            unit_type=unit_type,
            cost_usd=cost_usd,
            cumulative_spend_usd=cumulative,
            budget_remaining_usd=remaining,
        )
        await self._repo.add_entry(entry)

        # Update Prometheus gauges
        budget_spent_usd.set(cumulative)
        budget_remaining_usd.set(remaining)

        log.info(
            "cost_recorded",
            meeting_id=meeting_id,
            provider=provider,
            operation=operation,
            cost_usd=round(cost_usd, 5),
            cumulative_usd=round(cumulative, 4),
            remaining_usd=round(remaining, 4),
        )

        if remaining < settings.budget_reserve_usd and cost_usd > 0:
            log.warning(
                "budget_reserve_breached",
                remaining_usd=remaining,
                reserve_threshold=settings.budget_reserve_usd,
            )

    async def get_budget_summary(self) -> dict[str, object]:
        spent = await self._repo.get_total_spent()
        remaining = max(0.0, settings.budget_limit_usd - spent)
        breakdown = await self._repo.get_breakdown_by_provider()
        meetings_done = await self._repo.get_meeting_count()
        avg_cost = round(spent / meetings_done, 4) if meetings_done > 0 else 0.0
        estimated_remaining = int(remaining / avg_cost) if avg_cost > 0 else 9999

        return {
            "total_budget_usd": settings.budget_limit_usd,
            "spent_usd": round(spent, 4),
            "remaining_usd": round(remaining, 4),
            "meetings_processed": meetings_done,
            "avg_cost_per_meeting_usd": avg_cost,
            "estimated_meetings_remaining": estimated_remaining,
            "current_tier": "premium" if remaining > settings.budget_reserve_usd else "free",
            "breakdown": {k: round(v, 4) for k, v in breakdown.items()},
        }
