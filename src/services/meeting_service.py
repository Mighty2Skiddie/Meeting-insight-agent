"""
Core pipeline orchestrator — coordinates upload, transcription,
analysis, and report retrieval. All AI calls go through ProviderRouter.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import Meeting, MeetingStatus
from src.db.repository import MeetingRepository
from src.observability.logging import get_logger
from src.observability.metrics import (
    active_background_jobs,
    analysis_duration,
    cost_per_meeting,
    meeting_processing_duration,
    meetings_processed_total,
    transcription_duration,
)
from src.schemas import (
    MeetingInsights,
    MeetingReport,
    MeetingStatusResponse,
    ReportMetadata,
    TranscriptData,
    TranscriptSegment,
)
from src.services.cost_tracker import CostTracker
from src.services.provider_router import ProviderRouter
from src.utils.audio_processor import (
    AudioProcessingError,
    cleanup_files,
    extract_audio_as_mp3,
    get_audio_duration,
    save_upload,
)

log = get_logger(__name__)
settings = get_settings()


class MeetingService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = MeetingRepository(session)
        self._cost_tracker = CostTracker(session)
        self._router = ProviderRouter(self._cost_tracker)

    async def create_meeting(
        self,
        file_bytes: bytes,
        original_filename: str,
        title: str | None = None,
    ) -> Meeting:
        """Save uploaded file and create meeting record. Returns immediately."""
        if len(file_bytes) > settings.max_upload_size_bytes:
            raise AudioProcessingError(
                f"File size {len(file_bytes) / (1024*1024):.1f}MB exceeds "
                f"maximum {settings.max_upload_size_mb}MB"
            )

        stored_name, _ = await save_upload(file_bytes, original_filename)

        meeting = Meeting(
            id=str(uuid.uuid4()),
            title=title or Path(original_filename).stem,
            original_filename=original_filename,
            stored_filename=stored_name,
            file_size_bytes=len(file_bytes),
            status=MeetingStatus.UPLOADED,
            progress_percent=5,
            current_step="File uploaded — queued for processing",
        )
        return await self._repo.create(meeting)

    async def process_meeting(self, meeting_id: str) -> None:
        """
        Full async pipeline: audio extraction → STT → LLM analysis → DB save.
        Designed to be run as a BackgroundTask.
        """
        active_background_jobs.inc()
        pipeline_start = time.monotonic()
        source_path: str | None = None
        converted_path: str | None = None

        # Bind request context to all logs in this task
        structlog.contextvars.bind_contextvars(meeting_id=meeting_id)

        try:
            meeting = await self._repo.get_by_id(meeting_id)
            if not meeting:
                log.error("meeting_not_found", meeting_id=meeting_id)
                return

            source_path = str(settings.upload_dir / meeting.stored_filename)

            # Step 1: Extract audio
            await self._repo.update_status(
                meeting_id, MeetingStatus.TRANSCRIBING, 10,
                "Extracting audio with FFmpeg"
            )
            converted_path = await extract_audio_as_mp3(source_path)
            duration = await get_audio_duration(converted_path)

            # Step 2: Transcribe
            await self._repo.update_status(
                meeting_id, MeetingStatus.TRANSCRIBING, 25,
                "Transcribing audio"
            )
            t_start = time.monotonic()
            stt_result = await self._router.transcribe(converted_path, meeting_id)
            transcription_duration.labels(provider=stt_result.provider).observe(
                time.monotonic() - t_start
            )

            await self._repo.save_transcript(
                meeting_id=meeting_id,
                full_text=stt_result.full_text,
                segments=stt_result.segments,
                duration_seconds=stt_result.duration_seconds or duration,
                language=stt_result.language,
                provider=stt_result.provider,
            )

            # Step 3: LLM analysis
            await self._repo.update_status(
                meeting_id, MeetingStatus.ANALYZING, 60,
                "Running AI analysis"
            )
            a_start = time.monotonic()
            analysis_result = await self._router.analyze(
                transcript=stt_result.full_text,
                duration_seconds=stt_result.duration_seconds or duration,
                meeting_id=meeting_id,
            )
            analysis_duration.labels(provider=analysis_result.provider).observe(
                time.monotonic() - a_start
            )

            # Step 4: Persist final results
            total_elapsed = time.monotonic() - pipeline_start
            total_cost = stt_result.cost_usd + analysis_result.cost_usd

            # Determine tier: if any OpenAI provider was used → premium
            is_premium = any(
                n in (stt_result.provider + analysis_result.provider)
                for n in ("openai", "gpt")
            )
            tier = "premium" if is_premium else (
                "offline" if analysis_result.provider == "rule_based_engine" else "free"
            )

            await self._repo.save_insights(
                meeting_id=meeting_id,
                insights=analysis_result.insights,
                provider_llm=analysis_result.provider,
                tier_used=tier,
                degraded=analysis_result.degraded or stt_result.degraded,
                total_cost_usd=total_cost,
                processing_time_seconds=total_elapsed,
            )

            # Prometheus metrics
            meeting_processing_duration.observe(total_elapsed)
            meetings_processed_total.labels(status="completed").inc()
            cost_per_meeting.observe(total_cost)

            log.info(
                "pipeline_completed",
                meeting_id=meeting_id,
                duration_s=round(total_elapsed, 2),
                provider_stt=stt_result.provider,
                provider_llm=analysis_result.provider,
                tier=tier,
                cost_usd=round(total_cost, 4),
            )

        except Exception as exc:
            log.exception("pipeline_failed", meeting_id=meeting_id, error=repr(exc))
            await self._repo.mark_failed(meeting_id, repr(exc)[:500])
            meetings_processed_total.labels(status="failed").inc()
        finally:
            active_background_jobs.dec()
            structlog.contextvars.clear_contextvars()
            # Always clean up converted temp file
            if converted_path and converted_path != source_path:
                cleanup_files(converted_path)

    async def get_status(self, meeting_id: str) -> MeetingStatusResponse:
        meeting = await self._repo.get_by_id(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting {meeting_id} not found")
        return MeetingStatusResponse(
            meeting_id=meeting.id,
            status=meeting.status,
            progress_percent=meeting.progress_percent,
            current_step=meeting.current_step,
            provider_tier=meeting.tier_used,
            error=meeting.error_message,
        )

    async def get_report(self, meeting_id: str) -> MeetingReport:
        meeting = await self._repo.get_by_id(meeting_id)
        if not meeting:
            raise ValueError(f"Meeting {meeting_id} not found")

        # Parse stored JSON
        transcript_data: TranscriptData | None = None
        if meeting.transcript_full_text:
            raw_segments = json.loads(meeting.transcript_segments_json or "[]")
            segments = [TranscriptSegment(**s) for s in raw_segments]
            transcript_data = TranscriptData(
                full_text=meeting.transcript_full_text,
                segments=segments,
                word_count=len(meeting.transcript_full_text.split()),
                language=meeting.language or "en",
            )

        # Parse insights with null-safety guard
        insights_data: MeetingInsights | None = None
        if meeting.insights_json:
            try:
                parsed = json.loads(meeting.insights_json)
                if parsed is not None:
                    insights_data = MeetingInsights.model_validate(parsed)
            except Exception as exc:
                log.warning(
                    "insights_parse_error",
                    meeting_id=meeting_id,
                    error=str(exc),
                    raw_length=len(meeting.insights_json) if meeting.insights_json else 0,
                )

        duration = meeting.audio_duration_seconds
        formatted = _format_duration(duration) if duration else None

        return MeetingReport(
            meeting_id=meeting.id,
            title=meeting.title,
            duration_seconds=duration,
            duration_formatted=formatted,
            transcript=transcript_data,
            insights=insights_data,
            metadata=ReportMetadata(
                status=meeting.status,
                provider_stt=meeting.provider_stt,
                provider_llm=meeting.provider_llm,
                tier_used=meeting.tier_used,
                degraded=meeting.degraded,
                cost_usd=meeting.total_cost_usd,
                processing_time_seconds=meeting.processing_time_seconds,
                created_at=meeting.created_at,
                completed_at=meeting.completed_at,
            ),
        )

    async def analyze_raw_transcript(
        self, transcript: str, meeting_id: str | None = None
    ) -> tuple[str, MeetingInsights | None]:
        """Analyze a raw transcript string — no file upload needed."""
        mid = meeting_id or str(uuid.uuid4())
        # Create a minimal meeting record
        meeting = Meeting(
            id=mid,
            title="Direct Transcript Analysis",
            original_filename="transcript.txt",
            stored_filename="transcript.txt",
            file_size_bytes=len(transcript.encode()),
            status=MeetingStatus.ANALYZING,
        )
        await self._repo.create(meeting)

        result = await self._router.analyze(
            transcript=transcript, duration_seconds=0, meeting_id=mid
        )

        # Safely validate insights — don't crash on partial LLM output
        insights: MeetingInsights | None = None
        if result.insights is not None:
            try:
                insights = MeetingInsights.model_validate(result.insights)
            except Exception as exc:
                log.warning("raw_transcript_insights_parse_error", error=str(exc))

        await self._repo.save_insights(
            meeting_id=mid,
            insights=result.insights,
            provider_llm=result.provider,
            tier_used="free" if result.degraded else "premium",
            degraded=result.degraded,
            total_cost_usd=result.cost_usd,
            processing_time_seconds=0,
        )
        return mid, insights


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    return f"{m}m {sec}s"
