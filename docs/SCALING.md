# Scaling Guide
## PRN: 22510103 — Real-Time Web Indexing System
## Status: FINAL — Production upgrade path from MVP

This document explains how to scale every component from the single-node Docker Compose MVP to a production system handling millions of pages per day.

---

## MVP vs Production at a glance

| Component | MVP (Docker Compose) | Production (Kubernetes) |
|---|---|---|
| Crawler workers | 10 goroutines, 1 container | 10–10,000 pods, HPA on queue depth |
| Processor workers | 2 containers, CPU only | 4–100 pods, GPU for embeddings |
| Indexer workers | 1 container | 2–10 pods |
| Meilisearch | 1 node, ~10M docs | Elasticsearch cluster, unlimited |
| Qdrant | 1 node, ~10M vectors | Qdrant cluster, unlimited |
| PostgreSQL | 1 node | RDS PostgreSQL with read replicas |
| Redis | 1 node | Redis Cluster (6 nodes, 3 primary + 3 replica) |
| MinIO | 1 node | AWS S3 or GCS |
| API | 1 uvicorn process | 4–20 pods behind load balancer |

---

## Phase 1 — Discovery: Scaling the watchers

### Current capacity
- Sitemap poller: ~100 domains, 30-second interval
- CT log scanner: ~50 new domains/second
- Link extractor: ~100 outbound links/second per crawled page

### Scaling sitemap polling
```python
# Increase domain registry: store in PostgreSQL, not hardcoded list
# Partition domains across multiple discovery instances by hash:
#   instance 0 → domains where hash(domain) % 4 == 0
#   instance 1 → domains where hash(domain) % 4 == 1
#   ...

# In services/discovery/watchers/sitemap.py:
async def _load_domains(self) -> None:
    # Production: query from PostgreSQL
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT domain FROM domains WHERE is_active=TRUE AND id % $1 = $2",
            NUM_INSTANCES, INSTANCE_ID
        )
    self._domains = {row["domain"] for row in rows}
```

### Bloom filter at scale
At 1 billion URLs, the single Bloom filter (100M capacity) is full. Options:
1. **Increase capacity**: `BF.RESERVE key 0.01 1000000000` — uses ~1.2GB RAM
2. **Redis Cluster sharding**: shard the BF key across nodes by URL hash
3. **Rotating filters**: use two filters — active and archive. Swap daily.

---

## Phase 2 — Crawler: Scaling the worker pool

### Horizontal scaling
The Go crawler is stateless — add more replicas:
```yaml
# docker-compose.yml
crawler:
  deploy:
    replicas: 10        # 10 replicas × 10 workers = 100 concurrent fetches
```

```yaml
# Kubernetes HPA: scale on queue depth (custom metric)
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: crawler-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: crawler
  minReplicas: 3
  maxReplicas: 100
  metrics:
    - type: External
      external:
        metric:
          name: redis_queue_depth
        target:
          type: AverageValue
          averageValue: "500"   # 1 crawler pod per 500 queued URLs
```

### Rate limiting at scale
The token bucket rate limiter is Redis-backed — it works correctly across any number of replicas automatically. No configuration change needed.

### Playwright at scale
Playwright (headless Chromium) is memory-heavy (~300MB per instance). For production:
```bash
# Separate Playwright workers from Go fetcher
# Run Go fetcher: 50 replicas × 10 goroutines = 500 concurrent static fetches
# Run Playwright pool: 10 replicas, queue JS-only URLs to separate Redis list

# In page_classifier.py: route JS pages to QUEUE:urls:js
# Run dedicated Playwright workers consuming QUEUE:urls:js
```

---

## Phase 3 — Processor: Scaling the NLP pipeline

### The bottleneck
The processor is the most CPU-intensive stage. The embedding model (`all-MiniLM-L6-v2`) runs in 50ms on CPU. At 2 workers processing 50 docs/sec each, throughput is 100 docs/sec.

### Option 1: More CPU workers (easy, free)
```bash
# Scale processor replicas
docker compose up --scale processor=8
# 8 replicas × 50 docs/sec = 400 docs/sec
```

### Option 2: GPU embeddings (10× speedup)
```python
# In services/processor/pipeline/embedder.py:
import torch

def _get_model():
    model = SentenceTransformer("all-MiniLM-L6-v2")
    if torch.cuda.is_available():
        model = model.to("cuda")
        log.info("GPU embeddings enabled", device=torch.cuda.get_device_name(0))
    return model

# Result: ~50ms (CPU) → ~5ms (GPU T4) per document
# Cost: ~$0.30/hr for Google Cloud T4 GPU
```

### Option 3: ONNX runtime (2× speedup, no GPU required)
```bash
pip install optimum onnxruntime
optimum-cli export onnx --model sentence-transformers/all-MiniLM-L6-v2 ./model_onnx
```
```python
# Load ONNX model instead of PyTorch
from optimum.onnxruntime import ORTModelForFeatureExtraction
model = ORTModelForFeatureExtraction.from_pretrained("./model_onnx")
# Result: ~25ms per document on CPU (2× faster than PyTorch)
```

### Option 4: Batched embedding (3× throughput improvement)
```python
# In embedder.py: accumulate documents, embed in batches
def embed_batch(docs: list[tuple[str, str]]) -> list[list[float]]:
    inputs = [f"{title}. {text[:2000]}" for title, text in docs]
    embeddings = model.encode(
        inputs,
        batch_size=32,              # GPU: 64; CPU: 16
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [e.tolist() for e in embeddings]
# Batching amortises model overhead: 32 docs in ~100ms vs 32 × 50ms = 1600ms
```

---

## Phase 4 — Indexer: Scaling the index writers

### Meilisearch → Elasticsearch migration
When you exceed ~50M documents or need advanced query DSL:

```python
# services/indexer/writers/elasticsearch_writer.py  (swap-in replacement)
from elasticsearch import AsyncElasticsearch

class ElasticsearchWriter:
    def __init__(self):
        self._client = AsyncElasticsearch(hosts=["http://elasticsearch:9200"])

    async def write(self, doc: IndexedDocument) -> bool:
        await self._client.index(
            index="pages",
            id=self._url_to_id(doc.url),
            document=self._to_es_doc(doc),
        )
        return True
```

Change in `services/indexer/main.py`:
```python
# Before
self._meili = MeilisearchWriter()

# After
self._meili = ElasticsearchWriter()  # same interface, drop-in replacement
```

No changes to Phase 5 API needed — the writer interface is identical.

### Qdrant clustering
```yaml
# qdrant cluster config
cluster:
  enabled: true
  p2p:
    port: 6335
  consensus:
    tick_period_ms: 100

# Add shards: each shard handles 1/N of the vector space
# 1 shard = up to ~10M vectors (recommended)
# 3 shards = up to 30M vectors
```

```python
# Create collection with 3 shards, 2 replicas
await qdrant.create_collection(
    collection_name="pages",
    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    shard_number=3,
    replication_factor=2,
)
```

### PostgreSQL read replicas
```bash
# Add a read replica for analytics queries (SLO reports, admin dashboard)
# Write path: primary only
# Read path: replica (round-robin)

# In postgres_writer.py: use primary for writes
# In stats endpoint: use replica for SELECT queries
DATABASE_WRITE_URL=postgresql://indexer:pass@pg-primary:5432/indexer
DATABASE_READ_URL=postgresql://indexer:pass@pg-replica:5432/indexer
```

---

## Phase 5 — API: Scaling the search tier

### Multiple uvicorn workers
```bash
# Single process (MVP):
uvicorn main:app --workers 1

# Multi-process (production): 1 worker per CPU core
uvicorn main:app --workers 4

# Or with gunicorn:
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker
```

### Kubernetes deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: indexer-api
spec:
  replicas: 4
  selector:
    matchLabels:
      app: indexer-api
  template:
    spec:
      containers:
        - name: api
          image: realtime-indexer/api:latest
          resources:
            requests: { memory: "512Mi", cpu: "500m" }
            limits:   { memory: "2Gi",   cpu: "2" }
          env:
            - name: REDIS_URL
              value: redis://redis-cluster:6379/0
---
apiVersion: v1
kind: Service
metadata:
  name: indexer-api-svc
spec:
  selector:
    app: indexer-api
  ports:
    - port: 80
      targetPort: 8000
  type: LoadBalancer
```

### Redis result cache scaling
The 15-second TTL cache needs to be shared across all API replicas — Redis already handles this. No configuration change needed.

### Query embedding cache
At high query volume, the 60-second query vector cache in Redis can be extended:
```python
QUERY_VECTOR_CACHE_TTL = 300  # 5 minutes for popular queries
```

---

## Infrastructure Scaling

### Redis → Redis Cluster
```yaml
# redis-cluster.yml
services:
  redis-1: { image: redis:7, command: "redis-server --cluster-enabled yes --cluster-node-timeout 5000 --port 7001" }
  redis-2: { image: redis:7, command: "redis-server --cluster-enabled yes --cluster-node-timeout 5000 --port 7002" }
  redis-3: { image: redis:7, command: "redis-server --cluster-enabled yes --cluster-node-timeout 5000 --port 7003" }
  # + 3 replicas
```

```python
# In shared/config.py:
from redis.cluster import RedisCluster
redis_client = RedisCluster(
    startup_nodes=[
        {"host": "redis-1", "port": 7001},
        {"host": "redis-2", "port": 7002},
        {"host": "redis-3", "port": 7003},
    ],
    decode_responses=True,
)
```

### MinIO → AWS S3
```python
# services/crawler/internal/storage/minio_client.go
# Change endpoint to S3:
mc, _ := minio.New("s3.amazonaws.com", &minio.Options{
    Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
    Secure: true,
    Region: "us-east-1",
})
# No code changes elsewhere — same S3 API
```

---

## Throughput targets by scale tier

| Tier | URLs/day | Crawlers | Processors | Index size | Infrastructure |
|---|---|---|---|---|---|
| MVP | ~50K | 1 pod, 10 goroutines | 2 pods, CPU | ~1M docs | Docker Compose |
| Small prod | ~1M | 5 pods, 50 goroutines | 4 pods, CPU | ~10M docs | K8s, single nodes |
| Medium prod | ~10M | 20 pods, 200 goroutines | 8 pods, GPU | ~100M docs | K8s, clustered |
| Large prod | ~100M | 100+ pods | 20+ pods, GPU | ~1B docs | K8s, sharded, multi-region |

---

## Monitoring at scale

Add these Grafana alerts for production:

```yaml
# alerts.yml
groups:
  - name: indexer
    rules:
      - alert: SLOBreached
        expr: histogram_quantile(0.95, indexer_e2e_latency_seconds_bucket) > 60
        for: 5m
        labels: { severity: critical }
        annotations:
          summary: "P95 indexing latency exceeds 60s SLO"

      - alert: QueueBackpressure
        expr: indexer_queue_depth > 50000
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "URL queue backlog growing — scale crawlers"

      - alert: ProcessorLag
        expr: rate(indexer_docs_processed_total[5m]) == 0
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "Processor pipeline stalled — check STREAM:crawl_complete"

      - alert: IndexWriteErrors
        expr: rate(indexer_index_errors_total[5m]) > 0.01
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "Index write error rate above 1%"
```

---

*Scaling guide · PRN: 22510103 · Status: FINAL*