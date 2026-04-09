"""
Pydantic v2 schemas — single source of truth for:
  1. API request/response serialization
  2. OpenAI GPT-4o-mini structured output JSON schema
  3. Test assertion shapes
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# --- Transcript ---

class TranscriptSegment(BaseModel):
    speaker: str = Field(description="Speaker identifier, e.g. 'Speaker 1'")
    start: float = Field(description="Segment start time in seconds")
    end: float = Field(description="Segment end time in seconds")
    text: str = Field(description="Spoken text in this segment")


class TranscriptData(BaseModel):
    full_text: str
    segments: list[TranscriptSegment] = Field(default_factory=list)
    word_count: int = Field(default=0)
    language: str = Field(default="en")


# --- Insights (doubles as GPT-4o-mini JSON schema via .model_json_schema()) ---

class ActionItem(BaseModel):
    task: str = Field(description="Clear, actionable task description")
    owner: str = Field(description="Speaker identifier or 'Unknown'")
    priority: str = Field(description="high | medium | low")
    deadline_mentioned: str | None = Field(
        default=None, description="Any deadline mentioned, or null"
    )


class DiscussionTopic(BaseModel):
    topic: str = Field(description="Name of the discussion topic")
    time_spent_percent: int = Field(
        description="Estimated percentage of meeting time spent on this topic", ge=0, le=100
    )
    resolution: str = Field(description="resolved | ongoing | deferred")


class ProductivityAssessment(BaseModel):
    score: str = Field(description="Productive | Not Productive")
    reasoning: str = Field(description="2-3 sentence justification for the score")
    confidence: float = Field(description="Confidence score 0.0-1.0", ge=0.0, le=1.0)
    improvement_suggestions: list[str] = Field(
        default_factory=list, description="Actionable suggestions to improve future meetings"
    )


class MeetingInsights(BaseModel):
    """
    This schema is serialized via .model_json_schema() and passed directly
    to GPT-4o-mini as the response_format JSON schema.
    Pydantic model IS the contract — no drift possible.
    """
    summary: str = Field(description="2-3 paragraph executive summary of the meeting")
    key_decisions: list[str] = Field(description="Major decisions made during the meeting")
    action_items: list[ActionItem] = Field(description="Concrete next steps with owners")
    discussion_topics: list[DiscussionTopic] = Field(
        description="Main topics discussed with time allocation"
    )
    productivity: ProductivityAssessment
    sentiment: str = Field(description="Positive | Neutral | Negative | Mixed")
    follow_up_meeting_needed: bool = Field(
        description="Whether a follow-up meeting is recommended"
    )


# --- Request / Response ---

class UploadMeetingResponse(BaseModel):
    meeting_id: str
    status: str
    estimated_duration_seconds: int = Field(default=60)
    provider_tier: str = Field(default="premium")
    tracking_url: str


class AnalyzeMeetingRequest(BaseModel):
    meeting_id: str | None = Field(default=None)
    transcript: str | None = Field(default=None)

    model_config = {"json_schema_extra": {
        "examples": [
            {"meeting_id": "550e8400-e29b-41d4-a716-446655440000"},
            {"transcript": "Alice: Let's start the meeting. Bob: Agreed..."}
        ]
    }}


class AnalyzeMeetingResponse(BaseModel):
    meeting_id: str
    status: str
    tracking_url: str


# --- Status ---

class MeetingStatusResponse(BaseModel):
    meeting_id: str
    status: str
    progress_percent: int
    current_step: str | None
    provider_tier: str | None
    error: str | None


# --- Report ---

class ReportMetadata(BaseModel):
    status: str
    provider_stt: str | None
    provider_llm: str | None
    tier_used: str | None
    degraded: bool
    cost_usd: float
    processing_time_seconds: float | None
    created_at: datetime
    completed_at: datetime | None


class MeetingReport(BaseModel):
    meeting_id: str
    title: str | None
    duration_seconds: float | None
    duration_formatted: str | None
    transcript: TranscriptData | None
    insights: MeetingInsights | None
    metadata: ReportMetadata


# --- Budget ---

class BudgetResponse(BaseModel):
    total_budget_usd: float
    spent_usd: float
    remaining_usd: float
    meetings_processed: int
    avg_cost_per_meeting_usd: float
    estimated_meetings_remaining: int
    current_tier: str
    breakdown: dict[str, float]


# --- Health ---

class ServiceCheck(BaseModel):
    status: str  # ok | degraded | error
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    version: str


class ReadinessResponse(BaseModel):
    status: str  # ready | degraded | not_ready
    checks: dict[str, Any]


# --- Error (RFC 7807) ---

class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str | None = None
    timestamp: datetime | None = None
