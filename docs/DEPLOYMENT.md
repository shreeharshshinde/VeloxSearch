# Deployment Guide
## PRN: 22510103 — Real-Time Web Indexing System
## Status: FINAL

Three deployment modes: local development, Docker Compose (MVP demo), and Kubernetes (production).

---

## Prerequisites

| Tool | Minimum version | Check |
|---|---|---|
| Docker Desktop | 4.28+ (or Engine + Compose Plugin) | `docker --version` |
| Python | 3.11+ | `python3 --version` |
| Go | 1.22+ | `go version` |
| Make | any | `make --version` |
| RAM | 12 GB free | `free -h` |
| Disk | 10 GB free (Docker images + data) | `df -h` |

---

## Mode 1 — Local Python (development only)

Use this when iterating on a single service without Docker overhead.

```bash
# 1. Clone and set up
git clone https://github.com/your-username/realtime-indexer
cd realtime-indexer
cp .env.example .env

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install all dependencies
pip install -r services/discovery/requirements.txt
pip install -r services/processor/requirements.txt
pip install -r services/indexer/requirements.txt
pip install -r services/api/requirements.txt

# 4. Download NLP models
python -m spacy download en_core_web_sm
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# 5. Install Playwright browsers
python -m playwright install chromium --with-deps

# 6. Start infrastructure (Redis, MinIO, etc.) via Docker
docker compose up -d redis minio postgres meilisearch qdrant

# 7. Run a single service locally (hot reload)
cd services/discovery
LOG_FORMAT=text python main.py

# Or run the API with auto-reload
cd services/api
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## Mode 2 — Docker Compose (MVP / Demo)

This is the standard way to run the full system for the HoD demo.

### First-time setup

```bash
# 1. Copy environment config
cp .env.example .env
# Edit .env if needed (default values work for local demo)

# 2. Run one-time setup (pulls images, downloads NLP models into Docker layers)
make setup
# or: chmod +x scripts/setup.sh && ./scripts/setup.sh

# 3. Build all Docker images
make build
# This takes 5–10 minutes on first run (PyTorch download is large)
```

### Starting the system

```bash
# Option A: Start everything at once
make start
# or: docker compose up -d

# Option B: Start infrastructure first, then apps (better for debugging)
make infra                              # Redis, MinIO, PG, Meili, Qdrant, Grafana
docker compose up -d discovery crawler processor indexer api

# Check all services are healthy
docker compose ps
```

Expected healthy output:
```
NAME                  STATUS
indexer_redis         running (healthy)
indexer_minio         running (healthy)
indexer_postgres      running (healthy)
indexer_meilisearch   running (healthy)
indexer_qdrant        running (healthy)
indexer_discovery     running
indexer_crawler       running
indexer_processor     running
indexer_writer        running
indexer_api           running (healthy)
indexer_prometheus    running
indexer_grafana       running
```

### Accessing services

| Service | URL | Credentials |
|---|---|---|
| Search API | http://localhost:8000 | none |
| Swagger docs | http://localhost:8000/docs | none |
| Live demo dashboard | http://localhost:8000/demo | none |
| Grafana | http://localhost:3000 | admin / admin |
| MinIO console | http://localhost:9001 | minioadmin / minioadmin |
| RedisInsight | http://localhost:8001 | none |
| Prometheus | http://localhost:9090 | none |
| Meilisearch | http://localhost:7700 | masterKey |
| Qdrant | http://localhost:6333 | none |

### Running the demo

```bash
# Seed test URLs into the queue
make seed                     # 50 URLs (fast, good for demo)
make seed-large               # 500 URLs (more realistic)

# Run the live demo script (for HoD presentation)
make demo
# or: python scripts/demo.py

# Check SLO status
make check-slo

# Check index sizes
make check-index
```

### Stopping

```bash
make stop          # Stop containers (data preserved)
make clean         # Stop + remove containers (data preserved in volumes)
make clean-all     # Stop + remove containers AND volumes (DESTROYS ALL DATA)
```

---

## Mode 3 — Kubernetes (production)

### Prerequisites
- Kubernetes cluster (GKE, EKS, AKS, or k3s for local)
- `kubectl` configured
- Helm 3

### Build and push images

```bash
# Set your registry
REGISTRY=gcr.io/your-project

# Build and push all service images
for service in discovery crawler processor indexer api; do
  docker build -t $REGISTRY/indexer-$service:latest services/$service/
  docker push $REGISTRY/indexer-$service:latest
done
```

### Deploy infrastructure with Helm

```bash
# Redis
helm install redis bitnami/redis \
  --set auth.enabled=false \
  --set cluster.enabled=true \
  --set cluster.slaveCount=2

# PostgreSQL
helm install postgres bitnami/postgresql \
  --set postgresqlDatabase=indexer \
  --set postgresqlUsername=indexer \
  --set postgresqlPassword=your-secure-password

# Meilisearch
helm install meilisearch meilisearch/meilisearch \
  --set environment.MEILI_MASTER_KEY=your-master-key

# Qdrant
helm install qdrant qdrant/qdrant
```

### Deploy application services

```bash
# Create namespace
kubectl create namespace indexer

# Apply secrets
kubectl create secret generic indexer-secrets \
  --from-env-file=.env \
  -n indexer

# Deploy each service
for service in discovery crawler processor indexer api; do
  kubectl apply -f k8s/$service-deployment.yaml -n indexer
done

# Check rollout
kubectl rollout status deployment/indexer-api -n indexer
```

### Environment variables in Kubernetes

```yaml
# k8s/api-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: indexer-api
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: api
          image: gcr.io/your-project/indexer-api:latest
          envFrom:
            - secretRef:
                name: indexer-secrets
          env:
            - name: REDIS_URL
              value: "redis://redis-master:6379/0"
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: indexer-secrets
                  key: DATABASE_URL
```

---

## Environment variables reference

All variables live in `.env` (copy from `.env.example`).

### Critical variables (must set for production)

| Variable | Dev default | Production value |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | `redis://redis-cluster:6379/0` |
| `DATABASE_URL` | `postgresql://indexer:password@localhost:5432/indexer` | Use a managed DB + SSL |
| `MINIO_ENDPOINT` | `localhost:9000` | `s3.amazonaws.com` (or GCS) |
| `MINIO_SECURE` | `false` | `true` |
| `MEILISEARCH_API_KEY` | `masterKey` | Long random string |
| `CRAWLER_USER_AGENT` | `RealtimeIndexer/1.0` | Include your domain for contact |
| `WEBSUB_CALLBACK_URL` | `http://localhost:8000/websub/callback` | Your public domain |

### Tuning variables for production

| Variable | Dev | Production (recommended) |
|---|---|---|
| `CRAWLER_WORKERS` | `10` | `50` (per pod) |
| `PROCESSOR_WORKERS` | `4` | `8` or GPU pod |
| `REDIS_BLOOM_CAPACITY` | `100000000` | `1000000000` (1B) |
| `LOG_LEVEL` | `INFO` | `WARNING` (less noise) |
| `API_RESULT_CACHE_TTL` | `15` | `30` (less invalidation pressure) |

---

## Health checks and readiness

All services expose health checks:

```bash
# API health (used by K8s readiness probe)
curl http://localhost:8000/api/v1/health

# Redis ping
docker exec indexer_redis redis-cli PING

# Meilisearch health
curl http://localhost:7700/health -H "Authorization: Bearer masterKey"

# Qdrant health
curl http://localhost:6333/healthz

# PostgreSQL
docker exec indexer_postgres pg_isready -U indexer
```

### Kubernetes readiness probes

```yaml
# Already configured in Dockerfiles via HEALTHCHECK directive
# Override in K8s deployment if needed:
readinessProbe:
  httpGet:
    path: /api/v1/health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10
  failureThreshold: 3
livenessProbe:
  httpGet:
    path: /api/v1/health
    port: 8000
  initialDelaySeconds: 60
  periodSeconds: 30
```

---

## Backup and restore

### Redis (queue + Bloom filter)
```bash
# RDB snapshot (already configured in redis.conf)
docker exec indexer_redis redis-cli BGSAVE

# Copy RDB to safe location
docker cp indexer_redis:/data/dump.rdb ./backups/redis-$(date +%Y%m%d).rdb

# Restore
docker cp ./backups/redis-backup.rdb indexer_redis:/data/dump.rdb
docker compose restart redis
```

### PostgreSQL (metadata + crawl history)
```bash
# Backup
docker exec indexer_postgres pg_dump -U indexer indexer \
  | gzip > ./backups/postgres-$(date +%Y%m%d).sql.gz

# Restore
gunzip -c ./backups/postgres-backup.sql.gz \
  | docker exec -i indexer_postgres psql -U indexer indexer
```

### Meilisearch and Qdrant
Both persist to Docker volumes. Back up the volumes directly:
```bash
docker run --rm \
  -v realtime-indexer_meilisearch_data:/source \
  -v $(pwd)/backups:/backup \
  alpine tar czf /backup/meilisearch-$(date +%Y%m%d).tar.gz -C /source .
```

---

*Deployment guide · PRN: 22510103 · Status: FINAL*