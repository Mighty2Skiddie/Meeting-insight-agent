"""
Data Access Layer — all database operations go through this repository.
Zero business logic — only CRUD and queries.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import CostLedger, Meeting, MeetingStatus


class MeetingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, meeting: Meeting) -> Meeting:
        self._session.add(meeting)
        await self._session.commit()
        await self._session.refresh(meeting)
        return meeting

    async def get_by_id(self, meeting_id: str) -> Meeting | None:
        result = await self._session.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        meeting_id: str,
        status: MeetingStatus,
        progress: int | None = None,
        current_step: str | None = None,
    ) -> None:
        meeting = await self.get_by_id(meeting_id)
        if meeting:
            meeting.status = status
            if progress is not None:
                meeting.progress_percent = progress
            if current_step is not None:
                meeting.current_step = current_step
            await self._session.commit()

    async def save_transcript(
        self,
        meeting_id: str,
        full_text: str,
        segments: list[dict[str, Any]],
        duration_seconds: float,
        language: str,
        provider: str,
    ) -> None:
        meeting = await self.get_by_id(meeting_id)
        if meeting:
            meeting.transcript_full_text = full_text
            meeting.transcript_segments_json = json.dumps(segments)
            meeting.audio_duration_seconds = duration_seconds
            meeting.language = language
            meeting.provider_stt = provider
            meeting.status = MeetingStatus.ANALYZING
            meeting.progress_percent = 50
            meeting.current_step = "Transcript ready — running AI analysis"
            await self._session.commit()

    async def save_insights(
        self,
        meeting_id: str,
        insights: dict[str, Any] | None,
        provider_llm: str,
        tier_used: str,
        degraded: bool,
        total_cost_usd: float,
        processing_time_seconds: float,
    ) -> None:
        meeting = await self.get_by_id(meeting_id)
        if meeting:
            # Guard: only serialize insights if they are non-None
            if insights is not None:
                meeting.insights_json = json.dumps(insights)
            meeting.provider_llm = provider_llm
            meeting.tier_used = tier_used
            meeting.degraded = degraded
            meeting.total_cost_usd = total_cost_usd
            meeting.processing_time_seconds = processing_time_seconds
            meeting.status = MeetingStatus.COMPLETED
            meeting.progress_percent = 100
            meeting.current_step = "Analysis complete"
            meeting.completed_at = datetime.now(timezone.utc)
            await self._session.commit()

    async def mark_failed(self, meeting_id: str, error: str) -> None:
        meeting = await self.get_by_id(meeting_id)
        if meeting:
            meeting.status = MeetingStatus.FAILED
            meeting.error_message = error
            meeting.current_step = "Processing failed"
            await self._session.commit()

    async def list_all(self, limit: int = 50, offset: int = 0) -> list[Meeting]:
        result = await self._session.execute(
            select(Meeting).order_by(Meeting.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())


class CostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_entry(self, entry: CostLedger) -> CostLedger:
        self._session.add(entry)
        await self._session.commit()
        return entry

    async def get_total_spent(self) -> float:
        result = await self._session.execute(
            select(func.sum(CostLedger.cost_usd))
        )
        return float(result.scalar() or 0.0)

    async def get_breakdown_by_provider(self) -> dict[str, float]:
        result = await self._session.execute(
            select(CostLedger.provider, func.sum(CostLedger.cost_usd))
            .group_by(CostLedger.provider)
        )
        return {row[0]: float(row[1]) for row in result.all()}

    async def get_meeting_count(self) -> int:
        result = await self._session.execute(
            select(func.count(Meeting.id)).where(Meeting.status == MeetingStatus.COMPLETED)
        )
        return int(result.scalar() or 0)
