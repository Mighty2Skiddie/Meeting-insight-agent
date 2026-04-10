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

# Copy dependency spec and install all deps (not editable — just packages)
COPY pyproject.toml .
RUN uv pip install --system --no-cache \
    fastapi uvicorn pydantic pydantic-settings \
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

# FFmpeg for audio processing, libmagic1 for python-magic MIME detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code + package spec
COPY pyproject.toml .
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Install the package in editable mode inside the runtime image.
# This creates a .pth file in site-packages pointing to /app,
# which Python processes at startup for EVERY interpreter — including
# multiprocessing spawned workers. This is the only reliable fix.
RUN pip install --no-cache-dir -e . --no-deps

# Create data dirs + non-root user
RUN mkdir -p /app/data/uploads /app/data/db /tmp/uploads && \
    useradd --system --no-create-home --shell /bin/false appuser && \
    chown -R appuser:appuser /app && \
    chmod -R 777 /tmp

USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

EXPOSE 8000

# Single worker — no multiprocessing spawn issues, fits in 512MB RAM
CMD ["python", "-m", "uvicorn", "src.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8000", \
    "--timeout-keep-alive", "120"]
