# Mneme — Production Dockerfile
#
# Multi-stage build:
#   1. Builder: install dependencies with uv (faster than pip)
#   2. Runtime: copy artifacts, run uvicorn

# ── Stage 1: Builder ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install build deps for any wheels that need compiling
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies into a virtual env at /app/.venv
RUN uv sync --frozen --no-dev

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Don't run as root
RUN groupadd -r mneme && useradd -r -g mneme mneme

WORKDIR /app

# Copy the venv from the builder stage
COPY --from=builder --chown=mneme:mneme /app/.venv /app/.venv
COPY --from=builder --chown=mneme:mneme /app/pyproject.toml /app/uv.lock ./
COPY --chown=mneme:mneme src /app/src

# Add the venv to PATH so `uvicorn` and `python` resolve correctly
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER mneme

EXPOSE 8080

# Health check — same as in docker-compose.yml
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=5)"

# Run uvicorn — production server
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
