"""
Abstract base interfaces for all AI providers.
Adapter pattern — swap providers without touching service logic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TranscriptionResult:
    full_text: str
    segments: list[dict[str, object]] = field(default_factory=list)
    duration_seconds: float = 0.0
    language: str = "en"
    provider: str = ""
    cost_usd: float = 0.0
    degraded: bool = False


@dataclass
class AnalysisResult:
    insights: dict[str, object] = field(default_factory=dict)
    provider: str = ""
    cost_usd: float = 0.0
    degraded: bool = False


class STTProvider(ABC):
    """Speech-to-Text provider interface."""

    @abstractmethod
    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        """Transcribe audio file and return structured result."""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def cost_per_minute(self) -> float: ...


class LLMProvider(ABC):
    """Language Model provider interface."""

    @abstractmethod
    async def analyze(self, transcript: str, duration_seconds: float) -> AnalysisResult:
        """Analyze transcript and return structured meeting insights."""
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def cost_per_1k_tokens(self) -> float: ...
