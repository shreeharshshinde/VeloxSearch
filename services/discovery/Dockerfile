# =============================================================================
# Discovery Service Dockerfile
# =============================================================================
FROM python:3.11-slim

# Metadata
LABEL service="discovery"
LABEL version="1.0"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY . .

# Copy shared module (mounted as volume in dev, copied in prod)
# In docker-compose.yml, shared/ is volume-mounted at /app/shared
# For a standalone image build, uncomment:
# COPY ../../shared /app/shared

# Add shared to Python path
ENV PYTHONPATH="/app:/app/shared"
ENV PYTHONUNBUFFERED=1

# Health check — verify the service process is running
HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python -c "import redis, asyncio; asyncio.run(redis.from_url('${REDIS_URL:-redis://redis:6379/0}').ping())" \
    || exit 1

# Run the discovery service
CMD ["python", "main.py"]