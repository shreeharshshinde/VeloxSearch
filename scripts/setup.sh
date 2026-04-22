#!/usr/bin/env bash
# =============================================================================
# setup.sh — One-time environment setup for the Real-Time Indexing System
# PRN: 22510103
#
# Run once after cloning the repo:
#   chmod +x scripts/setup.sh
#   ./scripts/setup.sh
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Colour

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo ""
echo "======================================================"
echo "  Real-Time Web Indexing System — Environment Setup"
echo "  PRN: 22510103"
echo "======================================================"
echo ""

# ── Prerequisites check ───────────────────────────────────────────────────────
info "Checking prerequisites..."

command -v docker  >/dev/null 2>&1 || error "Docker not found. Install from https://docs.docker.com/get-docker/"
command -v python3 >/dev/null 2>&1 || error "Python 3 not found. Install Python 3.11+"
command -v go      >/dev/null 2>&1 || warn  "Go not found. Needed for crawler service. Install from https://go.dev/dl/"
command -v pip3    >/dev/null 2>&1 || error "pip3 not found."

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
info "Python version: $PYTHON_VERSION"

DOCKER_VERSION=$(docker --version | cut -d' ' -f3 | tr -d ',')
info "Docker version: $DOCKER_VERSION"

success "Prerequisites OK"
echo ""

# ── Environment file ──────────────────────────────────────────────────────────
info "Setting up environment file..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    success ".env created from .env.example"
    warn "Review .env and update any values before running in production"
else
    warn ".env already exists — skipping (delete and re-run to reset)"
fi
echo ""

# ── Python virtual environments ───────────────────────────────────────────────
info "Setting up Python virtual environments..."

setup_venv() {
    local service=$1
    local dir="services/$service"
    if [ ! -d "$dir" ]; then
        warn "Service directory not found: $dir"
        return
    fi
    info "  Installing $service dependencies..."
    python3 -m pip install --quiet -r "$dir/requirements.txt" 2>&1 | tail -5
    success "  $service deps installed"
}

# Install shared deps globally (or in a project venv if you prefer)
if [ -f "shared/requirements.txt" ]; then
    info "  Installing shared dependencies..."
    python3 -m pip install --quiet -r shared/requirements.txt
fi

setup_venv "discovery"
setup_venv "processor"
setup_venv "indexer"
setup_venv "api"

echo ""

# ── NLP Model downloads ───────────────────────────────────────────────────────
info "Downloading NLP models (this may take a few minutes)..."

# spaCy English model
info "  Downloading spaCy en_core_web_sm..."
python3 -m spacy download en_core_web_sm --quiet && success "  spaCy model ready" \
    || warn "  spaCy model download failed — processor will download on first run"

# sentence-transformers model (cached to ~/.cache/huggingface)
info "  Pre-downloading sentence-transformers model (all-MiniLM-L6-v2)..."
python3 -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('all-MiniLM-L6-v2')
print('  Model downloaded and cached')
" && success "  Embedding model ready" \
    || warn "  Model download failed — processor will download on first run"

echo ""

# ── Go modules ───────────────────────────────────────────────────────────────
if command -v go >/dev/null 2>&1; then
    info "Downloading Go modules..."
    if [ -f "services/crawler/go.mod" ]; then
        (cd services/crawler && go mod download)
        success "Go modules downloaded"
    else
        warn "services/crawler/go.mod not found — skipping"
    fi
else
    warn "Go not installed — skipping Go module setup"
fi
echo ""

# ── Docker images ─────────────────────────────────────────────────────────────
info "Pulling Docker images for infrastructure services..."
docker compose pull redis minio postgres meilisearch qdrant prometheus grafana 2>&1 \
    | grep -E "(Pulling|Pull complete|already)" || true
success "Infrastructure images ready"
echo ""

# ── Infrastructure startup check ──────────────────────────────────────────────
info "Starting infrastructure services..."
docker compose up -d redis minio postgres meilisearch qdrant

info "Waiting for services to be healthy (up to 60s)..."
TIMEOUT=60
ELAPSED=0

wait_healthy() {
    local service=$1
    while [ $ELAPSED -lt $TIMEOUT ]; do
        STATUS=$(docker compose ps --format json "$service" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health','unknown'))" 2>/dev/null || echo "unknown")
        if [ "$STATUS" = "healthy" ]; then
            success "  $service is healthy"
            return 0
        fi
        sleep 2
        ELAPSED=$((ELAPSED+2))
    done
    warn "  $service health check timed out — may still be starting"
}

wait_healthy "indexer_redis"
wait_healthy "indexer_postgres"
wait_healthy "indexer_meilisearch"
wait_healthy "indexer_qdrant"

echo ""

# ── MinIO bucket setup ────────────────────────────────────────────────────────
info "Creating MinIO buckets..."
docker compose run --rm minio_init 2>/dev/null && success "MinIO buckets created" \
    || warn "MinIO init may have already run"
echo ""

# ── Meilisearch index setup ───────────────────────────────────────────────────
info "Configuring Meilisearch index..."
python3 - <<'PYEOF'
import httpx, json, time, sys

base = "http://localhost:7700"
key  = "masterKey"
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

# Wait for Meilisearch to be ready
for i in range(10):
    try:
        r = httpx.get(f"{base}/health")
        if r.status_code == 200:
            break
    except Exception:
        pass
    time.sleep(2)
else:
    print("  Meilisearch not reachable — skipping index setup")
    sys.exit(0)

# Create/update index settings
settings = {
    "searchableAttributes": ["title", "clean_text", "keywords", "domain"],
    "filterableAttributes": ["domain", "language", "crawled_at", "discovery_source"],
    "sortableAttributes":   ["crawled_at", "freshness_score", "word_count"],
    "rankingRules": ["words", "typo", "proximity", "attribute", "sort", "exactness"],
}

r = httpx.patch(f"{base}/indexes/pages/settings", headers=headers, json=settings)
if r.status_code in (200, 202):
    print("  Meilisearch 'pages' index configured")
else:
    print(f"  Meilisearch settings: {r.status_code} {r.text[:100]}")
PYEOF
echo ""

# ── Qdrant collection setup ───────────────────────────────────────────────────
info "Creating Qdrant collection..."
python3 - <<'PYEOF'
import httpx, sys, time

base = "http://localhost:6333"

for i in range(10):
    try:
        r = httpx.get(f"{base}/healthz")
        if r.status_code == 200:
            break
    except Exception:
        pass
    time.sleep(2)
else:
    print("  Qdrant not reachable — skipping collection setup")
    sys.exit(0)

payload = {
    "vectors": {"size": 384, "distance": "Cosine"},
    "hnsw_config": {"m": 16, "ef_construct": 100},
    "optimizers_config": {"indexing_threshold": 1000},
}

r = httpx.put(f"{base}/collections/pages", json=payload)
if r.status_code in (200, 201):
    print("  Qdrant 'pages' collection created")
elif "already exists" in r.text.lower():
    print("  Qdrant 'pages' collection already exists")
else:
    print(f"  Qdrant: {r.status_code} {r.text[:100]}")
PYEOF
echo ""

# ── Done ──────────────────────────────────────────────────────────────────────
echo "======================================================"
echo -e "${GREEN}  Setup complete!${NC}"
echo "======================================================"
echo ""
echo "  Next steps:"
echo "    1. Review .env and adjust any settings"
echo "    2. Start all services:  docker compose up"
echo "    3. Seed test URLs:       python scripts/seed_urls.py"
echo "    4. Open API docs:        http://localhost:8000/docs"
echo "    5. Open Grafana:         http://localhost:3000  (admin/admin)"
echo "    6. Open RedisInsight:    http://localhost:8001"
echo "    7. Open MinIO Console:   http://localhost:9001  (minioadmin/minioadmin)"
echo ""