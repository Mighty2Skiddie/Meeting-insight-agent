"""Request timing and global error handler."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.observability.logging import get_logger
from src.schemas import ProblemDetail

log = get_logger(__name__)


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Logs every request with duration and injects X-Process-Time header."""

    async def dispatch(self, request: Request, call_next: object) -> Response:
        start = time.monotonic()
        response: Response = await call_next(request)  # type: ignore[arg-type, operator]
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=elapsed_ms,
        )
        return response


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all exception handler — converts any unhandled error to RFC 7807 format.
    Ensures clients never receive raw Python tracebacks.
    """
    request_id = request.headers.get("X-Request-ID", "unknown")
    log.exception("unhandled_exception", path=request.url.path, request_id=request_id)

    problem = ProblemDetail(
        type="https://github.com/Mighty2Skiddie/Meeting-insight-agent/blob/main/docs/errors#internal-error",
        title="Internal Server Error",
        status=500,
        detail="An unexpected error occurred. Please try again later.",
        instance=request.url.path,
        request_id=request_id,
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=500, content=problem.model_dump(mode="json"))
