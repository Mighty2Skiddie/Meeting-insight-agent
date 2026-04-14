"""
Database models using SQLAlchemy 2.0 with async support.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class MeetingStatus(str, Enum):
    UPLOADED = "UPLOADED"
    TRANSCRIBING = "TRANSCRIBING"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # Live meeting states
    LIVE_JOINING = "LIVE_JOINING"
    LIVE_WAITING = "LIVE_WAITING"          # waiting for host to admit bot
    LIVE_TRANSCRIBING = "LIVE_TRANSCRIBING"
    LIVE_ANALYZING = "LIVE_ANALYZING"      # interim analysis in progress
    LIVE_FINALIZING = "LIVE_FINALIZING"


class ProviderTier(str, Enum):
    PREMIUM = "premium"
    FREE = "free"
    OFFLINE = "offline"


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_filename: Mapped[str] = mapped_column(String(500))  # UUID-based safe name
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default=MeetingStatus.UPLOADED)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Transcript
    transcript_full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_segments_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    audio_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Analysis insights (stored as JSON string)
    insights_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provider metadata
    provider_stt: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_llm: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tier_used: Mapped[str | None] = mapped_column(String(20), nullable=True)
    degraded: Mapped[bool] = mapped_column(default=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    processing_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Live meeting metadata (null for uploaded recordings)
    meeting_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_live: Mapped[bool] = mapped_column(default=False)
    live_caption_count: Mapped[int] = mapped_column(Integer, default=0)
    live_elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Relationships
    cost_entries: Mapped[list[CostLedger]] = relationship(
        "CostLedger", back_populates="meeting", lazy="select"
    )


class CostLedger(Base):
    """Immutable audit log of every API call cost."""

    __tablename__ = "cost_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(String(36), ForeignKey("meetings.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(100))         # e.g. 'openai_whisper'
    operation: Mapped[str] = mapped_column(String(50))          # 'transcription' | 'analysis'
    input_units: Mapped[float | None] = mapped_column(Float, nullable=True)  # minutes or tokens
    unit_type: Mapped[str | None] = mapped_column(String(20), nullable=True)   # 'minutes' | 'tokens'
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    budget_remaining_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    meeting: Mapped[Meeting] = relationship("Meeting", back_populates="cost_entries")
