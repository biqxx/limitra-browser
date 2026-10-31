# Multi-stage production Dockerfile for supplier-browser-service

# --- Stage 1: Build & Dependencies ---
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# --- Stage 2: Runtime Environment ---
FROM python:3.12-slim-bookworm AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    TMPDIR=/tmp

WORKDIR /app

# Install system dependencies required for Chromium in headless mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Install only the Chromium browser binary used by the application,
# omitting the redundant chromium-headless-shell download to reduce image size.
RUN playwright install --no-shell chromium && \
    rm -rf /root/.cache /tmp/*

# Create non-root system user and runtime directories
RUN groupadd -r -g 1001 appgroup && \
    useradd -r -u 1001 -g appgroup -d /app -s /sbin/nologin appuser && \
    mkdir -p /app/diagnostics /opt/ms-playwright /tmp && \
    chown -R appuser:appgroup /app /opt/ms-playwright /tmp

# Copy application source
COPY --chown=appuser:appgroup app /app/app
COPY --chown=appuser:appgroup migrations /app/migrations
COPY --chown=appuser:appgroup alembic.ini /app/alembic.ini
COPY --chown=appuser:appgroup README.md /app/README.md

USER appuser

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/live || exit 1

# Default CMD runs the FastAPI service
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

