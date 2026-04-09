"""
structlog configuration — JSON structured logging with automatic
context injection (request_id, trace_id) across async boundaries.
"""
from __future__ import annotations

import logging
import sys

import structlog

from src.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.is_production:
        # Machine-readable JSON for log aggregators (Loki, CloudWatch, etc.)
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # Pretty console output for development
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Wire stdlib logging into structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    for noisy_logger in ["uvicorn.access", "sqlalchemy.engine"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.BoundLogger:  # type: ignore[type-arg]
    return structlog.get_logger(name)
