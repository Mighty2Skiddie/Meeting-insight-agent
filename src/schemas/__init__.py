"""
Pydantic v2 schemas — single source of truth for:
  1. API request/response serialization
  2. OpenAI GPT-4o-mini structured output JSON schema
  3. Test assertion shapes

LLM-facing models use `extra="ignore"` + sensible defaults + field
validators so that Groq/Gemini output quirks don't crash validation.
Common LLM quirks handled:
  - confidence returned as percentage (85) instead of fraction (0.85)
  - action_items returned as list of strings instead of objects
  - action_items missing required fields like "task"
  - key_decisions returned as list of dicts instead of strings
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
#
# All insight sub-models use extra="ignore" so unexpected LLM fields
# (e.g. "id", "metadata") don't crash Pydantic validation.  Defaults
# are provided for every optional-ish field so partial LLM responses
# still parse successfully.

class ActionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task: str = Field(default="", description="Clear, actionable task description")
    owner: str = Field(default="Unknown", description="Speaker identifier or 'Unknown'")
    priority: str = Field(default="medium", description="high | medium | low")
    deadline_mentioned: str | None = Field(
        default=None, description="Any deadline mentioned, or null"
    )


class DiscussionTopic(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str = Field(default="", description="Name of the discussion topic")
    time_spent_percent: int = Field(
        default=0,
        description="Estimated percentage of meeting time spent on this topic",
    )
    resolution: str = Field(default="ongoing", description="resolved | ongoing | deferred")

    @field_validator("time_spent_percent", mode="before")
    @classmethod
    def coerce_time_percent(cls, v: object) -> int:
        """Groq sometimes returns null for time_spent_percent."""
        if v is None:
            return 0
        try:
            return max(0, min(100, int(float(v))))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0


class ProductivityAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: str = Field(default="Not Productive", description="Productive | Not Productive")
    reasoning: str = Field(
        default="Unable to assess productivity.",
        description="2-3 sentence justification for the score",
    )
    confidence: float = Field(default=0.5, description="Confidence score 0.0-1.0")
    improvement_suggestions: list[str] = Field(
        default_factory=list,
        description="Actionable suggestions to improve future meetings",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: object) -> float:
        """LLMs sometimes return confidence as a percentage (85) not a fraction (0.85)."""
        try:
            f = float(v)  # type: ignore[arg-type]
            if f > 1.0:
                f = f / 100.0   # normalize 85 → 0.85
            return max(0.0, min(1.0, f))
        except (TypeError, ValueError):
            return 0.5

    @field_validator("improvement_suggestions", mode="before")
    @classmethod
    def ensure_suggestions_list(cls, v: object) -> list[str]:
        """Tolerate a single string, Python None, or the string 'None' instead of a list.
        Groq sometimes literally returns the string 'None' instead of [] or null.
        """
        if v is None or v == "None" or v == "none":
            return []
        if isinstance(v, str):
            return [v]  # single suggestion string — wrap it
        return list(v)  # type: ignore[arg-type]


class MeetingInsights(BaseModel):
    """
    This schema is serialized via .model_json_schema() and passed directly
    to GPT-4o-mini as the response_format JSON schema.
    Pydantic model IS the contract — no drift possible.

    extra="ignore" + field validators ensure Groq/Gemini output quirks
    are normalized rather than crashing validation.
    """
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(
        default="No summary available.",
        description="2-3 paragraph executive summary of the meeting",
    )
    key_decisions: list[str] = Field(
        default_factory=list,
        description="Major decisions made during the meeting",
    )
    action_items: list[ActionItem] = Field(
        default_factory=list, description="Concrete next steps with owners"
    )
    discussion_topics: list[DiscussionTopic] = Field(
        default_factory=list,
        description="Main topics discussed with time allocation",
    )
    productivity: ProductivityAssessment = Field(default_factory=ProductivityAssessment)
    sentiment: str = Field(
        default="Neutral", description="Positive | Neutral | Negative | Mixed"
    )
    follow_up_meeting_needed: bool = Field(
        default=False,
        description="Whether a follow-up meeting is recommended",
    )

    @field_validator("key_decisions", mode="before")
    @classmethod
    def normalize_key_decisions(cls, v: object) -> list[str]:
        """Some LLMs return decisions as dicts instead of strings."""
        if v is None:
            return []
        result = []
        for item in v:  # type: ignore[union-attr]
            if isinstance(item, dict):
                # Extract from common dict shapes: {"decision": "..."} or {"text": "..."}
                result.append(str(
                    item.get("decision") or item.get("text") or item.get("description") or str(item)
                ))
            else:
                result.append(str(item))
        return result

    @field_validator("action_items", mode="before")
    @classmethod
    def normalize_action_items(cls, v: object) -> list[dict[str, object]]:
        """LLMs sometimes return action_items as strings, or dicts missing 'task'."""
        if v is None:
            return []
        result = []
        for item in v:  # type: ignore[union-attr]
            if isinstance(item, str):
                result.append({"task": item, "owner": "Unknown", "priority": "medium"})
            elif isinstance(item, dict):
                if "task" not in item or not item["task"]:
                    # Try to find the task text from alternative field names
                    item["task"] = (
                        item.get("action") or item.get("description")
                        or item.get("item") or "(no task text)"
                    )
                result.append(item)
        return result

    @field_validator("discussion_topics", mode="before")
    @classmethod
    def normalize_discussion_topics(cls, v: object) -> list[dict[str, object]]:
        """Tolerate discussion_topics as list of strings."""
        if v is None:
            return []
        result = []
        for item in v:  # type: ignore[union-attr]
            if isinstance(item, str):
                result.append({"topic": item, "time_spent_percent": 0, "resolution": "ongoing"})
            else:
                result.append(item)
        return result

    @field_validator("productivity", mode="before")
    @classmethod
    def normalize_productivity(cls, v: object) -> object:
        """Tolerate productivity returned as a string ('Productive') instead of object."""
        if isinstance(v, str):
            return {"score": v, "reasoning": "", "confidence": 0.5}
        return v


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

    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {"meeting_id": "550e8400-e29b-41d4-a716-446655440000"},
            {"transcript": "Alice: Let's start the meeting. Bob: Agreed..."}
        ]
    })


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
