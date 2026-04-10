# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system deps: FFmpeg + build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv==0.5.14

# Copy dependency spec and install
COPY pyproject.toml .
RUN uv pip install --system --no-cache -e ".[dev]" 2>/dev/null || true
RUN uv pip install --system --no-cache \
    fastapi uvicorn gunicorn pydantic pydantic-settings \
    openai groq google-generativeai \
    sqlalchemy aiosqlite alembic \
    structlog prometheus-fastapi-instrumentator \
    opentelemetry-api opentelemetry-sdk opentelemetry-instrumentation-fastapi \
    opentelemetry-exporter-otlp-proto-grpc \
    tenacity slowapi asgi-correlation-id \
    aiofiles python-multipart httpx


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy FFmpeg from builder (already installed via apt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Ensure Python can resolve 'src.*' imports from /app
ENV PYTHONPATH=/app

# Create data directories with correct permissions
RUN mkdir -p /app/data/uploads /app/data/db /tmp/uploads && \
    useradd --system --no-create-home --shell /bin/false appuser && \
    chown -R appuser:appuser /app && \
    chmod -R 777 /tmp

USER appuser

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

EXPOSE 8000

# Use Gunicorn with Uvicorn workers for production
CMD ["gunicorn", "src.main:app", \
    "--workers", "2", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--bind", "0.0.0.0:8000", \
    "--timeout", "120", \
    "--graceful-timeout", "30", \
    "--access-logfile", "-", \
    "--error-logfile", "-"]
