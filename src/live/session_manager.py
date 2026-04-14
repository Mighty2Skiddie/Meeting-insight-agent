"""
Live meeting session — orchestrates bot lifecycle, caption accumulation,
interim analysis, and final report generation.

One LiveMeetingSession per active meeting. Uses the existing ProviderRouter
and MeetingRepository unchanged — only the input source is new.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Meeting, MeetingStatus
from src.db.repository import MeetingRepository
from src.live.browser_bot import CaptionEvent, MeetingBot
from src.live.ws_handler import broadcast
from src.observability.logging import get_logger
from src.services.cost_tracker import CostTracker
from src.services.provider_router import ProviderRouter

log = get_logger(__name__)


def _err(exc: Exception) -> str:
    """Return a non-empty error string even for bare exceptions like NotImplementedError."""
    return str(exc) or repr(exc) or type(exc).__name__

# How often to run interim AI analysis during a live meeting
INTERIM_ANALYSIS_INTERVAL_SECONDS = 300   # 5 minutes
# How often to emit a status heartbeat over WebSocket
HEARTBEAT_INTERVAL_SECONDS = 10
# Minimum transcript length before running analysis
MIN_TRANSCRIPT_WORDS_FOR_ANALYSIS = 30


@dataclass
class SessionState:
    meeting_id: str
    meeting_url: str
    bot_name: str
    captions: list[CaptionEvent] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    last_analysis_at: float = field(default_factory=time.monotonic)
    is_active: bool = True
    error: str | None = None

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def seconds_since_analysis(self) -> float:
        return time.monotonic() - self.last_analysis_at

    def transcript_text(self) -> str:
        """Concatenate all caption events into a readable transcript."""
        lines: list[str] = []
        for cap in self.captions:
            lines.append(f"{cap.speaker}: {cap.text}")
        return "\n".join(lines)

    def word_count(self) -> int:
        return len(self.transcript_text().split())


class LiveMeetingSession:
    """
    Manages the full lifecycle of one live meeting:
    join -> wait for admission -> capture captions -> interim analysis -> finalize.

    Design:
    - The browser bot runs in a background asyncio task
    - A heartbeat task emits WebSocket status events every 10 seconds
    - An analysis task fires every 5 minutes (or more if quiet)
    - On finalize(), runs the full analysis on the complete transcript
      using the same ProviderRouter that handles uploaded files
    """

    def __init__(
        self,
        meeting_id: str,
        meeting_url: str,
        session: AsyncSession,
        bot_name: str = "InsightBot",
    ) -> None:
        self._state = SessionState(
            meeting_id=meeting_id,
            meeting_url=meeting_url,
            bot_name=bot_name,
        )
        self._repo = MeetingRepository(session)
        self._cost_tracker = CostTracker(session)
        self._router = ProviderRouter(self._cost_tracker)
        self._bot = MeetingBot()
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    # -- Public API -----------------------------------------------------------

    async def start(self) -> None:
        """Entry point: join the meeting and launch background monitoring tasks."""
        try:
            await self._update_status(MeetingStatus.LIVE_JOINING, 5, "Joining meeting...")

            await self._bot.join_meeting(
                self._state.meeting_url,
                self._state.bot_name,
            )
            await self._update_status(MeetingStatus.LIVE_WAITING, 10, "Waiting to be admitted...")
            await broadcast(self._state.meeting_id, {
                "type": "status",
                "status": "LIVE_WAITING",
                "elapsed_seconds": 0,
            })

            admitted = await self._bot.wait_for_admission(timeout_seconds=120.0)
            if not admitted:
                raise RuntimeError("Bot was not admitted to the meeting within 2 minutes.")

            await self._bot.enable_captions()
            await self._bot.start_caption_capture(self._on_caption)

            await self._update_status(MeetingStatus.LIVE_TRANSCRIBING, 15, "Capturing live captions...")
            await broadcast(self._state.meeting_id, {
                "type": "status",
                "status": "LIVE_TRANSCRIBING",
                "elapsed_seconds": 0,
            })

            log.info("live_session_started", meeting_id=self._state.meeting_id)

            # Launch monitoring loop
            loop_task = asyncio.create_task(self._monitoring_loop())
            self._tasks.append(loop_task)
            await loop_task

        except Exception as exc:
            self._state.error = _err(exc)
            self._state.is_active = False
            log.error("live_session_failed", meeting_id=self._state.meeting_id, error=_err(exc))
            await self._repo.mark_failed(self._state.meeting_id, _err(exc))
            await broadcast(self._state.meeting_id, {"type": "error", "message": str(exc)})

    async def stop(self) -> None:
        """Called by POST /stop-live or when meeting ends naturally."""
        self._state.is_active = False
        for task in self._tasks:
            task.cancel()
        await self.finalize()

    # -- Internal lifecycle ---------------------------------------------------

    async def _monitoring_loop(self) -> None:
        """Main loop: checks meeting liveness, sends heartbeats, triggers interim analysis."""
        heartbeat_counter = 0
        while self._state.is_active:
            await asyncio.sleep(5)

            # Check if the meeting has ended
            if not await self._bot.is_meeting_active():
                log.info("meeting_ended_naturally", meeting_id=self._state.meeting_id)
                self._state.is_active = False
                break

            # Heartbeat every HEARTBEAT_INTERVAL_SECONDS
            heartbeat_counter += 5
            if heartbeat_counter >= HEARTBEAT_INTERVAL_SECONDS:
                heartbeat_counter = 0
                await self._emit_heartbeat()

            # Interim analysis every INTERIM_ANALYSIS_INTERVAL_SECONDS
            if (
                self._state.seconds_since_analysis() >= INTERIM_ANALYSIS_INTERVAL_SECONDS
                and self._state.word_count() >= MIN_TRANSCRIPT_WORDS_FOR_ANALYSIS
            ):
                await self._run_interim_analysis()

        # Meeting ended -- run final analysis
        await self.finalize()

    async def _on_caption(self, event: CaptionEvent) -> None:
        """Called for every caption emitted by the browser bot."""
        self._state.captions.append(event)

        # Persist latest transcript to DB
        meeting = await self._repo.get_by_id(self._state.meeting_id)
        if meeting:
            meeting.transcript_full_text = self._state.transcript_text()
            meeting.live_caption_count = len(self._state.captions)
            meeting.live_elapsed_seconds = self._state.elapsed_seconds()

        # Broadcast to WebSocket clients
        await broadcast(self._state.meeting_id, {
            "type": "caption",
            "speaker": event.speaker,
            "text": event.text,
            "ts": event.timestamp,
            "caption_count": len(self._state.captions),
        })

        log.debug("caption_received", speaker=event.speaker, words=len(event.text.split()))

    async def _emit_heartbeat(self) -> None:
        await broadcast(self._state.meeting_id, {
            "type": "status",
            "status": "LIVE_TRANSCRIBING",
            "elapsed_seconds": round(self._state.elapsed_seconds()),
            "caption_count": len(self._state.captions),
            "word_count": self._state.word_count(),
        })

    async def _run_interim_analysis(self) -> None:
        """Run AI analysis on the current transcript without ending the meeting."""
        self._state.last_analysis_at = time.monotonic()
        transcript = self._state.transcript_text()
        elapsed = self._state.elapsed_seconds()

        await self._update_status(MeetingStatus.LIVE_ANALYZING, None, "Running interim analysis...")
        log.info("interim_analysis_start", meeting_id=self._state.meeting_id, words=self._state.word_count())

        try:
            result = await self._router.analyze(
                transcript=transcript,
                duration_seconds=elapsed,
                meeting_id=self._state.meeting_id,
            )
            await broadcast(self._state.meeting_id, {
                "type": "interim_insights",
                "insights": result.insights,
                "elapsed_seconds": round(elapsed),
                "degraded": result.degraded,
            })
            log.info("interim_analysis_done", meeting_id=self._state.meeting_id)
        except Exception as exc:
            log.error("interim_analysis_error", error=str(exc))
        finally:
            await self._update_status(
                MeetingStatus.LIVE_TRANSCRIBING, None, "Capturing live captions..."
            )

    async def finalize(self) -> None:
        """Run final analysis on the complete transcript and mark meeting COMPLETED."""
        transcript = self._state.transcript_text()
        elapsed = self._state.elapsed_seconds()

        log.info(
            "live_session_finalizing",
            meeting_id=self._state.meeting_id,
            total_captions=len(self._state.captions),
            total_words=self._state.word_count(),
            elapsed_seconds=round(elapsed),
        )

        await self._update_status(MeetingStatus.LIVE_FINALIZING, 90, "Running final analysis...")
        await broadcast(self._state.meeting_id, {
            "type": "status",
            "status": "LIVE_FINALIZING",
            "elapsed_seconds": round(elapsed),
        })

        # Leave the meeting cleanly
        try:
            await self._bot.leave_meeting()
        except Exception as exc:
            log.warning("leave_meeting_error", error=_err(exc))

        if self._state.word_count() < MIN_TRANSCRIPT_WORDS_FOR_ANALYSIS:
            log.warning(
                "transcript_too_short_for_analysis",
                word_count=self._state.word_count(),
            )
            await self._repo.mark_failed(
                self._state.meeting_id,
                f"Transcript too short ({self._state.word_count()} words). "
                "Meeting may have been too brief or captions were not captured.",
            )
            await broadcast(self._state.meeting_id, {
                "type": "meeting_ended",
                "meeting_id": self._state.meeting_id,
                "status": "FAILED",
            })
            return

        try:
            t0 = time.monotonic()
            result = await self._router.analyze(
                transcript=transcript,
                duration_seconds=elapsed,
                meeting_id=self._state.meeting_id,
            )

            # Build segment list from caption events
            segments = [
                {
                    "speaker": cap.speaker,
                    "start": round(cap.timestamp - self._state.started_at, 1),
                    "end": round(cap.timestamp - self._state.started_at + 3.0, 1),
                    "text": cap.text,
                }
                for cap in self._state.captions
            ]

            await self._repo.save_transcript(
                meeting_id=self._state.meeting_id,
                full_text=transcript,
                segments=segments,
                duration_seconds=elapsed,
                language="en",
                provider="google_meet_captions",
            )
            await self._repo.save_insights(
                meeting_id=self._state.meeting_id,
                insights=result.insights,
                provider_llm=result.provider,
                tier_used="free" if result.degraded else "premium",
                degraded=result.degraded,
                total_cost_usd=result.cost_usd,
                processing_time_seconds=time.monotonic() - t0,
            )

            await broadcast(self._state.meeting_id, {
                "type": "meeting_ended",
                "meeting_id": self._state.meeting_id,
                "status": "COMPLETED",
                "report_url": f"/api/v1/meetings/{self._state.meeting_id}/report",
            })

            log.info("live_session_completed", meeting_id=self._state.meeting_id)

        except Exception as exc:
            log.error("finalize_error", meeting_id=self._state.meeting_id, error=_err(exc))
            await self._repo.mark_failed(self._state.meeting_id, _err(exc))
            await broadcast(self._state.meeting_id, {
                "type": "error",
                "message": f"Final analysis failed: {_err(exc)}",
            })

    async def _update_status(
        self,
        status: MeetingStatus,
        progress: int | None = None,
        step: str | None = None,
    ) -> None:
        await self._repo.update_status(
            self._state.meeting_id,
            status=status,
            progress=progress,
            current_step=step,
        )


# -- Module-level session registry --------------------------------------------
# Tracks active live sessions so /stop-live can reach them

_active_sessions: dict[str, LiveMeetingSession] = {}


def get_session(meeting_id: str) -> LiveMeetingSession | None:
    return _active_sessions.get(meeting_id)


def register_session(session: LiveMeetingSession) -> None:
    _active_sessions[session._state.meeting_id] = session


def unregister_session(meeting_id: str) -> None:
    _active_sessions.pop(meeting_id, None)


async def create_live_meeting(
    meeting_url: str,
    db_session: AsyncSession,
    bot_name: str = "InsightBot",
    title: str | None = None,
) -> str:
    """
    Factory function called by the API endpoint.

    Creates a Meeting record, spawns a background asyncio task for the
    LiveMeetingSession, and returns the meeting_id immediately.
    """
    meeting_id = str(uuid.uuid4())
    repo = MeetingRepository(db_session)

    meeting = Meeting(
        id=meeting_id,
        title=title or f"Live Meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        original_filename="live_capture",
        stored_filename="live_capture",
        file_size_bytes=0,
        status=MeetingStatus.LIVE_JOINING,
        progress_percent=0,
        meeting_url=meeting_url,
        is_live=True,
    )
    await repo.create(meeting)
    log.info("live_meeting_created", meeting_id=meeting_id, url=meeting_url)

    # Import here to avoid circular import at module load time
    from src.db.session import async_session_factory  # noqa: PLC0415

    async def _run_in_own_session() -> None:
        """Background task with its own DB session (same pattern as background.py)."""
        async with async_session_factory() as bg_session:
            live_session = LiveMeetingSession(
                meeting_id=meeting_id,
                meeting_url=meeting_url,
                session=bg_session,
                bot_name=bot_name,
            )
            register_session(live_session)
            try:
                await live_session.start()
            finally:
                unregister_session(meeting_id)

    asyncio.create_task(_run_in_own_session())
    return meeting_id
