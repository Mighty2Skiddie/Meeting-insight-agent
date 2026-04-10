"""
Centralized application configuration using Pydantic Settings.
All values are read from environment variables / .env file
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    environment: str = Field(default="development", pattern="^(development|staging|production)$")
    log_level: str = Field(default="INFO")
    app_version: str = Field(default="1.0.0")

    # ── AI Providers ──────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")

    # OpenAI Models
    openai_stt_model: str = Field(default="whisper-1")
    openai_llm_model: str = Field(default="gpt-4o-mini")

    # Groq Models
    groq_stt_model: str = Field(default="whisper-large-v3")
    groq_llm_model: str = Field(default="llama-3.3-70b-versatile")

    # Gemini Models
    gemini_llm_model: str = Field(default="gemini-2.0-flash")

    # ── Budget Guard ──────────────────────────────────────────────────────────
    budget_limit_usd: float = Field(default=2.00, gt=0)
    budget_reserve_usd: float = Field(default=0.20, gt=0)

    # ── Storage ───────────────────────────────────────────────────────────────
    database_url: str = Field(default="sqlite+aiosqlite:///./data/meetings.db")
    upload_dir: Path = Field(default=Path("./data/uploads"))
    max_upload_size_mb: int = Field(default=200, gt=0)

    # ── Resilience ────────────────────────────────────────────────────────────
    circuit_breaker_fail_max: int = Field(default=3, gt=0)
    circuit_breaker_reset_timeout: int = Field(default=120, gt=0)
    retry_max_attempts: int = Field(default=3, gt=0)
    request_timeout_seconds: int = Field(default=60, gt=0)

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_uploads: str = Field(default="10/minute")
    rate_limit_reads: str = Field(default="30/minute")

    # ── Observability ─────────────────────────────────────────────────────────
    otel_exporter_otlp_endpoint: str = Field(default="")
    otel_service_name: str = Field(default="meeting-insight-agent")

    @field_validator("upload_dir", mode="before")
    @classmethod
    def ensure_upload_dir(cls, v: str | Path) -> Path:
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.startswith("sk-"))

    @property
    def has_groq_key(self) -> bool:
        return bool(self.groq_api_key and self.groq_api_key.startswith("gsk_"))

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return cached settings singleton. Re-reads on first call per process."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
