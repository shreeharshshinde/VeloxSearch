# =============================================================================
# Crawler Service Dockerfile
# Multi-stage build: Go HTTP fetcher + Python Playwright runner
# =============================================================================

# ── Stage 1: Build the Go HTTP fetcher binary ─────────────────────────────────
FROM golang:1.22-alpine AS go-builder

WORKDIR /build

# Download Go dependencies first (cached layer)
COPY go.mod go.sum ./
RUN go mod download

# Build the crawler binary
COPY cmd/ ./cmd/
COPY internal/ ./internal/
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /crawler ./cmd/

# ── Stage 2: Python runtime for Playwright ────────────────────────────────────
FROM python:3.11-slim

LABEL service="crawler"
LABEL version="1.0"

# System dependencies for Playwright / Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    ca-certificates \
    # Chromium dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the compiled Go binary from builder stage
COPY --from=go-builder /crawler /usr/local/bin/crawler

# Install Python dependencies
COPY playwright_crawler/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Download Playwright Chromium browser
RUN python -m playwright install chromium --with-deps

# Copy Python crawler code
COPY playwright_crawler/ ./playwright_crawler/

# Copy shared module
# (mounted as volume in dev, copied in prod)
# COPY ../../shared /app/shared

ENV PYTHONPATH="/app:/app/shared"
ENV PYTHONUNBUFFERED=1

# The crawler service runs the Go binary.
# Playwright tasks are dispatched by the Processor service (Celery).
CMD ["/usr/local/bin/crawler"]