"""
Background task runner — provides a single, reusable entry point for
processing meetings outside the HTTP request lifecycle.

This module exists because FastAPI's `BackgroundTasks` run *after* the
HTTP response is sent, meaning the request-scoped SQLAlchemy session
(from `Depends(get_db)`) is already closed.  We solve this by opening
a dedicated session that lives for the entire duration of the pipeline.
"""
from __future__ import annotations

import structlog

from src.db.session import async_session_factory
from src.observability.logging import get_logger

log = get_logger(__name__)


async def run_meeting_pipeline(meeting_id: str) -> None:
    """
    Process a meeting in a fully isolated database session.

    Designed to be used as:
        background_tasks.add_task(run_meeting_pipeline, meeting.id)

    The session is committed per-operation inside the repository layer,
    so we do NOT add an extra commit here — that would mask errors.
    """
    from src.services.meeting_service import MeetingService  # avoid circular

    structlog.contextvars.bind_contextvars(
        meeting_id=meeting_id, context="background_pipeline"
    )

    async with async_session_factory() as session:
        try:
            service = MeetingService(session)
            await service.process_meeting(meeting_id)
        except Exception:
            log.exception("background_pipeline_fatal", meeting_id=meeting_id)
            # mark_failed is already called inside process_meeting's except,
            # so we just ensure the session is rolled back cleanly.
            await session.rollback()
        finally:
            structlog.contextvars.clear_contextvars()
