"""
Live meeting API endpoints — Google Meet bot join, status, stop, and WebSocket stream.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db
from src.db.repository import MeetingRepository
from src.live.session_manager import create_live_meeting, get_session
from src.live.ws_handler import live_transcript_ws
from src.observability.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["live"])

GOOGLE_MEET_PATTERN = re.compile(r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}$")


# ── Request / Response schemas ─────────────────────────────────────────────

class JoinLiveRequest(BaseModel):
    meeting_url: str
    bot_name: str = "Transcriber"
    title: str | None = None

    @field_validator("meeting_url")
    @classmethod
    def validate_google_meet_url(cls, v: str) -> str:
        if not GOOGLE_MEET_PATTERN.match(v.strip()):
            raise ValueError(
                "Invalid Google Meet URL. Expected format: "
                "https://meet.google.com/xxx-xxxx-xxx"
            )
        return v.strip()


class JoinLiveResponse(BaseModel):
    meeting_id: str
    status: str
    ws_url: str
    status_url: str
    stop_url: str
    message: str


class LiveStatusResponse(BaseModel):
    meeting_id: str
    status: str
    elapsed_seconds: float
    caption_count: int
    word_count: int
    interim_insights_available: bool
    error: str | None


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post(
    "/join-live",
    response_model=JoinLiveResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Join a live Google Meet and start real-time transcription",
    description=(
        "Launches a headless Chromium bot that joins the given Google Meet URL as a guest, "
        "enables captions, and streams the live transcript via WebSocket. "
        "Processing is asynchronous — poll /live-status or connect to the WebSocket."
    ),
)
async def join_live_meeting(
    request: JoinLiveRequest,
    db: AsyncSession = Depends(get_db),
) -> JoinLiveResponse:
    """
    Start a live meeting session.

    Returns immediately with a meeting_id. The bot joins the meeting in the background.
    Connect to the WebSocket URL to receive real-time caption and insight events.
    """
    try:
        meeting_id = await create_live_meeting(
            meeting_url=request.meeting_url,
            db_session=db,
            bot_name=request.bot_name,
            title=request.title,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not start live meeting session: {exc}",
        ) from exc

    log.info("live_meeting_join_requested", meeting_id=meeting_id, url=request.meeting_url)

    return JoinLiveResponse(
        meeting_id=meeting_id,
        status="LIVE_JOINING",
        ws_url=f"/api/v1/meetings/{meeting_id}/live",
        status_url=f"/api/v1/meetings/{meeting_id}/live-status",
        stop_url=f"/api/v1/meetings/{meeting_id}/stop-live",
        message=(
            f"Bot '{request.bot_name}' is joining the meeting. "
            "If your meeting requires host approval, the bot will wait up to 2 minutes. "
            f"Connect to the WebSocket at ws://HOST/api/v1/meetings/{meeting_id}/live "
            "for real-time captions."
        ),
    )


@router.get(
    "/{meeting_id}/live-status",
    response_model=LiveStatusResponse,
    summary="Get real-time status of a live meeting session",
)
async def get_live_status(
    meeting_id: str,
    db: AsyncSession = Depends(get_db),
) -> LiveStatusResponse:
    repo = MeetingRepository(db)
    meeting = await repo.get_by_id(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id!r} not found")

    if not meeting.is_live:
        raise HTTPException(
            status_code=400,
            detail="This meeting was not created as a live session. Use /status instead.",
        )

    session = get_session(meeting_id)
    word_count = 0
    caption_count = meeting.live_caption_count or 0
    if session:
        word_count = session._state.word_count()
        caption_count = len(session._state.captions)

    has_interim = meeting.insights_json is not None

    return LiveStatusResponse(
        meeting_id=meeting_id,
        status=meeting.status,
        elapsed_seconds=round(meeting.live_elapsed_seconds or 0.0),
        caption_count=caption_count,
        word_count=word_count,
        interim_insights_available=has_interim,
        error=meeting.error_message,
    )


@router.post(
    "/{meeting_id}/stop-live",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stop a live meeting session and generate the final report",
    description=(
        "Triggers the bot to leave the meeting, runs final AI analysis "
        "on the complete transcript, and marks the meeting as COMPLETED. "
        "The full report is then available at /api/v1/meetings/{id}/report."
    ),
)
async def stop_live_meeting(
    meeting_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    repo = MeetingRepository(db)
    meeting = await repo.get_by_id(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id!r} not found")

    if not meeting.is_live:
        raise HTTPException(status_code=400, detail="Not a live meeting session")

    session = get_session(meeting_id)
    if session is None:
        # Session may have already ended naturally
        return {
            "meeting_id": meeting_id,
            "status": meeting.status,
            "message": "Session already ended. Check /report for the final result.",
        }

    # Trigger graceful stop (runs in background — stop() is async)
    import asyncio
    asyncio.create_task(session.stop())

    log.info("live_meeting_stop_requested", meeting_id=meeting_id)

    return {
        "meeting_id": meeting_id,
        "status": "LIVE_FINALIZING",
        "message": (
            "Bot is leaving the meeting and running final analysis. "
            f"Check /api/v1/meetings/{meeting_id}/report in ~30 seconds."
        ),
    }


@router.websocket("/{meeting_id}/live")
async def websocket_live(websocket: WebSocket, meeting_id: str) -> None:
    """
    WebSocket endpoint for real-time live meeting events.

    Events received by the client:
    - {"type": "connected",        "meeting_id": "..."}
    - {"type": "caption",          "speaker": "Alice", "text": "...", "ts": 1234.5, "caption_count": 12}
    - {"type": "status",           "status": "LIVE_TRANSCRIBING", "elapsed_seconds": 60}
    - {"type": "interim_insights", "insights": {...}, "elapsed_seconds": 300, "degraded": false}
    - {"type": "meeting_ended",    "meeting_id": "...", "status": "COMPLETED", "report_url": "..."}
    - {"type": "error",            "message": "..."}

    Send any text message to keep the connection alive (ping).
    """
    await live_transcript_ws(websocket, meeting_id)
