"""Application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.api.middleware import RequestTimingMiddleware, global_exception_handler
from src.api.v1.health import router as health_router
from src.api.v1.router import router as v1_router
from src.config import get_settings
from src.db.session import init_db
from src.observability import configure_logging, setup_metrics, setup_tracing

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    log = structlog.get_logger(__name__)
    log.info("startup_begin", environment=settings.environment, version=settings.app_version)

    await init_db()
    log.info("database_ready")

    yield

    log.info("shutdown_complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Meeting Insight Agent",
        version=settings.app_version,
        description=(
            "AI-powered meeting analysis — transcription, structured insights, "
            "action items, and productivity evaluation. "
            "Powered by OpenAI Whisper + GPT-4o-mini with intelligent fallback chains."
        ),
        contact={"name": "Meeting Insight Agent", "url": "https://github.com/Mighty2Skiddie/Meeting-insight-agent"},
        license_info={"name": "MIT"},
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    setup_tracing(app)
    instrumentator = setup_metrics(app)

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # Middleware registered outermost → innermost
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware, header_name="X-Request-ID")
    app.add_middleware(RequestTimingMiddleware)

    if settings.is_production:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    app.add_exception_handler(Exception, global_exception_handler)  # type: ignore[arg-type]

    app.include_router(health_router)   # /health, /readiness — no prefix
    app.include_router(v1_router)       # /api/v1/meetings/...

    instrumentator.expose(app, endpoint="/metrics", include_in_schema=False)

    return app


app = create_app()
