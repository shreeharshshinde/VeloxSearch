# =============================================================================
# Indexer Service Dockerfile
# Lightweight — no ML models, just API clients for 4 backends
# =============================================================================
FROM python:3.11-slim

LABEL service="indexer"
LABEL version="1.0"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH="/app:/app/shared"
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python -c "import redis, asyncio; asyncio.run(redis.from_url('${REDIS_URL:-redis://redis:6379/0}').ping())" \
    || exit 1

CMD ["python", "main.py"]