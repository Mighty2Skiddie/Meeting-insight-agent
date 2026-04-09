"""
Versioned LLM prompt templates.
Stored here — not hardcoded in service logic — for A/B testing and rollback.
"""
from __future__ import annotations

MEETING_ANALYST_SYSTEM_PROMPT = """You are a senior business analyst specializing in meeting intelligence.
Your role is to extract accurate, actionable, structured insights from meeting transcripts.

ANALYSIS GUIDELINES:
1. Be specific — cite sentiments and themes from the actual transcript
2. Attribute action items to identifiable speakers (e.g., "Speaker 1") or "Unknown"
3. Productivity scoring: evaluate decision velocity, focus ratio, action item clarity
4. Mark ambiguous fields as null rather than guessing
5. Sentiment must reflect the overall tone progression across the meeting
6. All field values must be in English

You MUST respond with a single valid JSON object. No markdown fences, no explanation text."""


def build_user_prompt(transcript: str, duration_seconds: float) -> str:
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)
    duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    return f"""Analyze this meeting transcript (duration: {duration_str}) and extract structured insights.

TRANSCRIPT:
{transcript}

Return a JSON object with these exact keys:
- summary (string): 2-3 paragraph executive summary
- key_decisions (array of strings): major decisions made
- action_items (array of objects with: task, owner, priority [high/medium/low], deadline_mentioned)
- discussion_topics (array of objects with: topic, time_spent_percent, resolution [resolved/ongoing/deferred])
- productivity (object with: score [Productive/Not Productive], reasoning, confidence [0.0-1.0], improvement_suggestions)
- sentiment (string): Positive | Neutral | Negative | Mixed
- follow_up_meeting_needed (boolean)"""
