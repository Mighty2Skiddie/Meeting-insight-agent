"""
FastAPI dependency injection bindings.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db
from src.services.meeting_service import MeetingService


async def get_meeting_service(
    session: AsyncSession = Depends(get_db),
) -> AsyncGenerator[MeetingService, None]:
    yield MeetingService(session)
