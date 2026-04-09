"""Offline fallback (Tier 4). Regex + heuristics — zero API dependency, always works."""
from __future__ import annotations

import re

from src.observability.logging import get_logger
from src.providers.base import AnalysisResult, LLMProvider

log = get_logger(__name__)

# Common action item patterns
_ACTION_PATTERNS = [
    r"(?:will|going to|should|need to|must|have to|action[:\s]+)\s+(.{10,80})",
    r"(?:follow[\s-]?up|next step)[:\s]+(.{10,80})",
    r"(?:TODO|ACTION|TASK)[:\s]+(.{10,80})",
]

# Decision patterns
_DECISION_PATTERNS = [
    r"(?:decided|agreed|resolved|confirmed|approved)[:\s]+(.{10,100})",
    r"(?:decision|conclusion)[:\s]+(.{10,100})",
    r"(?:we will|we are going to)\s+(.{10,80})",
]

# Productivity keywords
_PRODUCTIVE_SIGNALS = [
    "decided", "agreed", "approved", "resolved", "action item",
    "next steps", "completed", "delivered", "launched", "shipped",
]
_UNPRODUCTIVE_SIGNALS = [
    "unclear", "confused", "off-topic", "going around in circles",
    "no conclusion", "no decision", "rabbit hole", "tangent",
]


class RuleBasedProvider(LLMProvider):
    """
    Offline fallback — always works, basic quality.
    Produces a valid insights dict using regex + heuristics.
    """

    @property
    def name(self) -> str:
        return "rule_based_engine"

    @property
    def cost_per_1k_tokens(self) -> float:
        return 0.0

    async def analyze(self, transcript: str, duration_seconds: float) -> AnalysisResult:
        log.warning("rule_based_fallback_activated", reason="all_cloud_providers_unavailable")
        sentences = [s.strip() for s in re.split(r"[.!?]", transcript) if len(s.strip()) > 20]
        words = transcript.split()

        # Extract action items
        action_items: list[dict[str, object]] = []
        for pattern in _ACTION_PATTERNS:
            for match in re.finditer(pattern, transcript, re.IGNORECASE):
                task = match.group(1).strip().rstrip(".,;")
                if len(task) > 10:
                    action_items.append({
                        "task": task[:200],
                        "owner": "Unknown",
                        "priority": "medium",
                        "deadline_mentioned": None,
                    })
        action_items = action_items[:8]   # Cap at 8

        # Extract decisions
        decisions: list[str] = []
        for pattern in _DECISION_PATTERNS:
            for match in re.finditer(pattern, transcript, re.IGNORECASE):
                d = match.group(1).strip().rstrip(".,;")
                if d:
                    decisions.append(d[:200])
        decisions = decisions[:5]

        # Build summary from first and last sentences
        summary_parts = sentences[:3] + (["..."] if len(sentences) > 6 else []) + sentences[-2:]
        summary = " ".join(summary_parts)[:600]

        # Productivity score
        lower = transcript.lower()
        prod_hits = sum(1 for w in _PRODUCTIVE_SIGNALS if w in lower)
        unprod_hits = sum(1 for w in _UNPRODUCTIVE_SIGNALS if w in lower)
        is_productive = prod_hits >= unprod_hits
        score = "Productive" if is_productive else "Not Productive"
        reasoning = (
            f"Detected {prod_hits} productive signal(s) and {unprod_hits} "
            f"unproductive signal(s) in the transcript. "
            f"{'Meeting appears goal-oriented.' if is_productive else 'Meeting may lack clear direction.'}"
        )

        # Topics — unique noun-phrase chunks (simple heuristic)
        topics = list({s[:60] for s in sentences[:10] if len(s) > 15})[:5]
        discussion_topics = [
            {"topic": t, "time_spent_percent": 100 // max(len(topics), 1), "resolution": "ongoing"}
            for t in topics
        ]

        duration_minutes = duration_seconds / 60
        insights: dict[str, object] = {
            "summary": summary or "Unable to generate summary — transcript may be too short.",
            "key_decisions": decisions or ["No clear decisions detected."],
            "action_items": action_items,
            "discussion_topics": discussion_topics,
            "productivity": {
                "score": score,
                "reasoning": reasoning,
                "confidence": 0.4,
                "improvement_suggestions": [
                    "Ensure clear agenda before the meeting.",
                    "Assign action items with owners and deadlines.",
                ],
            },
            "sentiment": "Neutral",
            "follow_up_meeting_needed": len(action_items) > 3,
        }

        return AnalysisResult(
            insights=insights,
            provider=self.name,
            cost_usd=0.0,
            degraded=True,
        )
