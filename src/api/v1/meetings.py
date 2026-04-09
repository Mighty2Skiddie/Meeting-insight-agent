"""Meeting endpoints — upload, analyze, status, report."""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

from src.api.dependencies import get_meeting_service
from src.services.background import run_meeting_pipeline
from src.observability.logging import get_logger
from src.schemas import (
    AnalyzeMeetingRequest,
    AnalyzeMeetingResponse,
    BudgetResponse,
    MeetingReport,
    MeetingStatusResponse,
    ProblemDetail,
    UploadMeetingResponse,
)
from src.services.cost_tracker import CostTracker
from src.services.meeting_service import MeetingService
from src.utils.audio_processor import ALLOWED_EXTENSIONS, AudioProcessingError

log = get_logger(__name__)
router = APIRouter(prefix="/meetings", tags=["Meetings"])


def _problem(status_code: int, title: str, detail: str, instance: str, request_id: str = "") -> JSONResponse:
    p = ProblemDetail(
        type=f"https://github.com/Mighty2Skiddie/Meeting-insight-agent/blob/main/docs/errors#{title.lower().replace(' ', '-')}",
        title=title,
        status=status_code,
        detail=detail,
        instance=instance,
        request_id=request_id or "unknown",
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=status_code, content=p.model_dump(mode="json"))



@router.post(
    "/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadMeetingResponse,
    summary="Upload a meeting audio/video file",
    description=(
        "Accepts any common audio or video format. "
        "Processing is asynchronous — poll `/meetings/{id}/status` for progress."
    ),
)
async def upload_meeting(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
    title: str | None = None,
    service: MeetingService = Depends(get_meeting_service),
) -> UploadMeetingResponse:
    request_id = request.headers.get("X-Request-ID", "")
    structlog.contextvars.bind_contextvars(request_id=request_id)

    ext = (file.filename or "").lower().rsplit(".", 1)[-1]
    if f".{ext}" not in ALLOWED_EXTENSIONS:
        return _problem(  # type: ignore[return-value]
            415, "Unsupported Media Type",
            f"File type '.{ext}' is not supported. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            "/api/v1/meetings/upload", request_id,
        )

    try:
        file_bytes = await file.read()
        meeting = await service.create_meeting(
            file_bytes=file_bytes,
            original_filename=file.filename or "upload",
            title=title,
        )
    except AudioProcessingError as exc:
        return _problem(413, "File Too Large", str(exc), "/api/v1/meetings/upload", request_id)  # type: ignore[return-value]

    background_tasks.add_task(run_meeting_pipeline, meeting.id)

    log.info("upload_accepted", meeting_id=meeting.id, filename=file.filename)
    return UploadMeetingResponse(
        meeting_id=meeting.id,
        status="PROCESSING",
        estimated_duration_seconds=60,
        provider_tier="premium",
        tracking_url=f"/api/v1/meetings/{meeting.id}/status",
    )

@router.post(
    "/analyze",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AnalyzeMeetingResponse,
    summary="Analyze an existing meeting or raw transcript",
    description="Pass a `meeting_id` to re-analyze an uploaded meeting, or provide a `transcript` string directly.",
)
async def analyze_meeting(
    request: Request,
    background_tasks: BackgroundTasks,
    body: AnalyzeMeetingRequest,
    service: MeetingService = Depends(get_meeting_service),
) -> AnalyzeMeetingResponse:
    request_id = request.headers.get("X-Request-ID", "")

    if body.meeting_id:
        # Re-analyze an existing uploaded meeting
        try:
            status_resp = await service.get_status(body.meeting_id)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Meeting {body.meeting_id} not found")

        background_tasks.add_task(run_meeting_pipeline, body.meeting_id)
        return AnalyzeMeetingResponse(
            meeting_id=body.meeting_id,
            status="ANALYZING",
            tracking_url=f"/api/v1/meetings/{body.meeting_id}/status",
        )

    elif body.transcript:
        meeting_id, _ = await service.analyze_raw_transcript(body.transcript)
        return AnalyzeMeetingResponse(
            meeting_id=meeting_id,
            status="COMPLETED",
            tracking_url=f"/api/v1/meetings/{meeting_id}/status",
        )

    raise HTTPException(
        status_code=422,
        detail="Either 'meeting_id' or 'transcript' must be provided",
    )


@router.get(
    "/{meeting_id}/status",
    response_model=MeetingStatusResponse,
    summary="Poll processing status",
)
async def get_status(
    meeting_id: str,
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingStatusResponse:
    try:
        return await service.get_status(meeting_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Meeting '{meeting_id}' not found")


@router.get(
    "/{meeting_id}/report",
    response_model=MeetingReport,
    summary="Get the full structured meeting report",
    description="Returns transcript, AI-generated insights, action items, and processing metadata.",
)
async def get_report(
    meeting_id: str,
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingReport:
    try:
        report = await service.get_report(meeting_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Meeting '{meeting_id}' not found")

    if report.metadata.status == "FAILED":
        raise HTTPException(
            status_code=422,
            detail=f"Meeting processing failed: {report.metadata}",
        )
    if report.metadata.status not in ("COMPLETED",):
        raise HTTPException(
            status_code=202,
            detail=f"Meeting is still processing. Current status: {report.metadata.status}",
        )
    return report
