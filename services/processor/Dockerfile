# =============================================================================
# Processor Service Dockerfile
# NLP pipeline: Trafilatura + spaCy + sentence-transformers + extruct
#
# NOTE ON IMAGE SIZE:
#   PyTorch (CPU) alone is ~400MB. This image will be ~1.5GB.
#   In production, use a multi-stage build to separate model caching.
#   For MVP, the large size is acceptable — it only downloads once.
# =============================================================================
FROM python:3.11-slim

LABEL service="processor"
LABEL version="1.0"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer — changes rarely)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download spaCy English model
RUN python -m spacy download en_core_web_sm

# Pre-download sentence-transformers model (cached in Docker layer)
# This avoids downloading at runtime — critical for latency
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Download fastText language ID model
RUN mkdir -p /app/models && \
    curl -sL https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz \
         -o /app/models/lid.176.ftz && \
    echo "fastText model downloaded: $(du -sh /app/models/lid.176.ftz | cut -f1)"

# Copy service code (changes frequently — separate layer from deps)
COPY . .

# Copy shared module
# Mounted as volume in dev; uncomment for standalone production build:
# COPY ../../shared /app/shared

ENV PYTHONPATH="/app:/app/shared"
ENV PYTHONUNBUFFERED=1
# Tell sentence-transformers where to cache models
ENV SENTENCE_TRANSFORMERS_HOME="/app/models"

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD python -c "import redis, asyncio; asyncio.run(redis.from_url('${REDIS_URL:-redis://redis:6379/0}').ping())" \
    || exit 1

CMD ["python", "main.py"]