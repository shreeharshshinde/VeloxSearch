# =============================================================================
# API Service Dockerfile — FastAPI + sentence-transformers (for query embedding)
# =============================================================================
FROM python:3.11-slim

LABEL service="api"
LABEL version="1.0"

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download embedding model (used for query vectors)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY . .

ENV PYTHONPATH="/app:/app/shared"
ENV PYTHONUNBUFFERED=1
ENV SENTENCE_TRANSFORMERS_HOME="/app/models"

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${API_PORT:-8000}/api/v1/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-config", "/dev/null"]