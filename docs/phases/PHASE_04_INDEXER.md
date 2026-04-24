# Phase 4 — Indexer Layer
## PRN: 22510103 | Real-Time Web Indexing System

> **Goal of Phase 4:** Write each enriched `IndexedDocument` from Phase 3
> to all four search backends **simultaneously** — completing the write
> within the **45–58 second** window from original URL discovery.

---

## Table of Contents

1. [What Phase 4 Does](#1-what-phase-4-does)
2. [Architecture of the Indexer Layer](#2-architecture-of-the-indexer-layer)
3. [Backend Deep-Dive](#3-backend-deep-dive)
   - 3.1 [Meilisearch — Full-Text BM25 Index](#31-meilisearch--full-text-bm25-index)
   - 3.2 [Qdrant — ANN Vector Index](#32-qdrant--ann-vector-index)
   - 3.3 [PostgreSQL — Metadata + Audit Log](#33-postgresql--metadata--audit-log)
   - 3.4 [Redis Signals — Ranking Cache + Cache Invalidation](#34-redis-signals--ranking-cache--cache-invalidation)
4. [Parallel Write Strategy](#4-parallel-write-strategy)
5. [Fault Tolerance: Partial Success + DLQ](#5-fault-tolerance-partial-success--dlq)
6. [Document ID Strategy](#6-document-id-strategy)
7. [Configuration Reference](#7-configuration-reference)
8. [File Reference](#8-file-reference)
9. [How to Run Phase 4](#9-how-to-run-phase-4)
10. [How to Test Phase 4](#10-how-to-test-phase-4)
11. [Metrics & Observability](#11-metrics--observability)
12. [Common Issues & Fixes](#12-common-issues--fixes)
13. [What Phase 5 Can Now Do](#13-what-phase-5-can-now-do)

---

## 1. What Phase 4 Does

Phase 4 is the final write step. It takes a fully enriched `IndexedDocument`
from the Processor (Phase 3) and makes it searchable in under 3 seconds
by writing to four backends in parallel.

**Input:**  Redis Stream `STREAM:docs_ready` — IndexedDocument JSON from Phase 3

**Output:** Document written to:
  - Meilisearch (BM25 keyword search — searchable within ~200ms of write)
  - Qdrant (ANN vector search — searchable within ~5ms of write)
  - PostgreSQL (metadata + audit log — persistent history)
  - Redis (ranking signals + cache invalidation events)

**Time budget:** 45 to 58 seconds from discovery (12ms write time)

### Why four backends?

Each backend serves a different purpose and excels at a different query type:

| Backend | Query type | Strength | Used for |
|---|---|---|---|
| Meilisearch | Keyword / BM25 | Exact word matching, typo tolerance | "machine learning tutorial" |
| Qdrant | Vector / ANN | Semantic meaning | "how do neural nets learn?" |
| PostgreSQL | SQL | Complex filters, aggregations, history | Admin dashboard, SLO reports |
| Redis | Key-value | Sub-millisecond lookups | Reranking signals, cache invalidation |

Phase 5 (API) uses Meilisearch + Qdrant together (hybrid search), then uses
Redis signals to rerank, and PostgreSQL only for admin/stats queries.

---

## 2. Architecture of the Indexer Layer

```
Redis STREAM:docs_ready
         │
   XREADGROUP (consumer group: indexer_group)
         │ BATCH_SIZE = 5 docs per read
         │
    ┌────┴──────────────────────────────────────────────┐
    │               INDEXER SERVICE                      │
    │           (asyncio event loop)                     │
    │                                                    │
    │  for each doc in batch:                            │
    │    asyncio.gather(                                 │
    │      ┌─────────────────┐                           │
    │      │  Meilisearch    │  ~10ms                    │
    │      │  add_documents()│                           │
    │      └─────────────────┘                           │
    │      ┌─────────────────┐                           │
    │      │  Qdrant         │  ~5ms   ← all run         │
    │      │  upsert()       │         in PARALLEL        │
    │      └─────────────────┘         via gather()      │
    │      ┌─────────────────┐                           │
    │      │  PostgreSQL     │  ~10ms                    │
    │      │  UPSERT+INSERT  │                           │
    │      └─────────────────┘                           │
    │      ┌─────────────────┐                           │
    │      │  Redis signals  │  ~2ms                     │
    │      │  HSET+PUBLISH   │                           │
    │      └─────────────────┘                           │
    │    )                                               │
    │    → total: ~12ms  (max, not sum)                  │
    │                                                    │
    │  XACK STREAM:docs_ready msg_id                     │
    └────────────────────────────────────────────────────┘
```

### Why asyncio.gather() for parallel writes?

Sequential writes would take 10 + 5 + 10 + 2 = **27ms**.
Parallel writes take max(10, 5, 10, 2) = **~12ms**.

Both are well within Phase 4's 13-second budget (45s → 58s),
but parallel writes also free up the event loop sooner for the next batch.

---

## 3. Backend Deep-Dive

### 3.1 Meilisearch — Full-Text BM25 Index

**File:** `services/indexer/writers/meilisearch_writer.py`
**Schema:** `services/indexer/schemas/meilisearch_schema.json`

#### What is BM25?

BM25 (Best Match 25) is the gold-standard ranking algorithm for keyword search.
It improves on simple TF-IDF in two ways:

```
BM25(doc, query) = Σ IDF(term) × TF(term, doc) × (k₁ + 1)
                              ──────────────────────────────────────
                              TF(term, doc) + k₁ × (1 - b + b × |doc|/avgdl)

Where:
  IDF  = log((N - df + 0.5) / (df + 0.5))  [rarer term = higher weight]
  TF   = term frequency in document
  |doc| = document length in words
  avgdl = average document length
  k₁   = 1.2  (term saturation — prevents one repeated term from dominating)
  b    = 0.75 (length normalisation — long docs don't unfairly dominate)
```

This means:
- A search for "machine learning" finds exact matches
- A rare term like "HNSW" has higher IDF weight than a common term like "search"
- A 50-word stub and a 2000-word article are compared fairly (length-normalised)

#### Document schema in Meilisearch

```
id:               SHA-256(url)[:32]  — primary key, alphanumeric
url:              canonical URL
domain:           hostname
title:            highest search weight (in searchableAttributes[0])
og_title:         secondary title
keywords:         extracted phrases — boosts relevant queries
clean_text:       first 10,000 chars — body text (truncated to keep index small)
summary:          300-char snippet — returned in search results
language:         for filter expressions  (filter=language="en")
freshness_score:  for sort  (sort=freshness_score:desc)
domain_authority: for sort and display
indexed_at:       Unix timestamp — when this version was written
entity_names:     list of entity text values — "Google", "OpenAI"
entity_types:     set of entity label types  — "ORG", "PERSON"
```

#### NRT (Near-Real-Time) behaviour

```
add_documents() called → document queued internally in Meilisearch
                       → background task processes queue (~200ms)
                       → document appears in search results

Meilisearch guarantees: new docs searchable within 1 second of add_documents()
For our SLO: even 1s delay is fine — we have 13 seconds in Phase 4's budget.
```

#### Why the 10,000 char truncation on clean_text?

Meilisearch builds an in-memory inverted index. Storing 50,000 characters
of full article text per document at 10 million documents = 500GB of index —
too large for RAM. We store the first 10,000 chars (covers most of the relevant
content) and rely on the `summary`, `keywords`, and `title` fields for the rest.

---

### 3.2 Qdrant — ANN Vector Index

**File:** `services/indexer/writers/qdrant_writer.py`
**Schema:** `services/indexer/schemas/qdrant_collection.json`

#### What is HNSW?

HNSW (Hierarchical Navigable Small World) is the state-of-the-art algorithm
for approximate nearest-neighbour search in high-dimensional spaces.

```
Layer 2 (coarse):  ● ────────────── ●           ← few nodes, long-range links
                    \               /
Layer 1 (medium):   ● ──── ● ──── ●             ← more nodes
                          |
Layer 0 (fine):  ● ─ ● ─ ● ─ ● ─ ● ─ ●        ← all nodes, local links
```

Search starts at Layer 2 (quick coarse navigation) and descends to Layer 0
(precise local search), pruning branches that are geometrically far from
the query vector at each layer.

Result: finds the nearest neighbour in O(log N) time, not O(N).
At 1M documents: HNSW = ~1ms, exact search = ~1000ms.

"Approximate" means it might miss the true nearest neighbour 1–5% of the time.
For search relevance, this is an acceptable trade-off for 1000× speed gain.

#### HNSW parameters we use

```
m = 16
  Number of bidirectional links per node at each layer.
  Higher = better quality, more memory, slower build.
  8 = fast/light, 16 = balanced (our choice), 32 = high quality

ef_construct = 100
  Candidates explored when building each node's neighbourhood.
  Higher = better graph quality, slower initial indexing.
  100 is a well-tested default for web-scale search.

full_scan_threshold = 10,000
  When the collection has fewer than 10k vectors, use exact search.
  Exact search is faster than HNSW for very small collections.
```

#### Point structure

```python
PointStruct(
    id     = uint64  # SHA-256(url)[:8] as big-endian int64
    vector = float32[384]  # L2-normalised embedding from Phase 3
    payload = {
        "url":              "https://techcrunch.com/...",
        "domain":           "techcrunch.com",
        "title":            "How AI is Reshaping Web Search",
        "summary":          "The landscape of web search...",
        "language":         "en",
        "freshness_score":  0.98,
        "domain_authority": 0.92,
        "crawled_at":       1704067215.3,
    }
)
```

#### Payload filtering (critical for performance)

Without filtering, ANN search returns the 50 most semantically similar documents
regardless of language, domain, or recency. With payload filtering:

```python
# Example: find English articles about "machine learning" from high-authority domains
await qdrant.search(
    collection_name="pages",
    query_vector=embed_query("machine learning"),
    query_filter=Filter(must=[
        FieldCondition(key="language", match=MatchValue(value="en")),
        FieldCondition(key="domain_authority", range=Range(gte=0.3)),
    ]),
    limit=50,
)
```

The payload index on `language` and `domain_authority` means Qdrant applies
the filter BEFORE searching the HNSW graph — much faster than post-filtering.

---

### 3.3 PostgreSQL — Metadata + Audit Log

**File:** `services/indexer/writers/postgres_writer.py`

#### Two-table write pattern

Every successful index triggers two SQL statements in a single transaction:

**1. UPSERT into `pages` (current state)**
```sql
INSERT INTO pages (url, domain, title, language, ...)
VALUES ($1, $2, $3, $4, ...)
ON CONFLICT (url) DO UPDATE SET
    title       = EXCLUDED.title,
    indexed_at  = EXCLUDED.indexed_at,
    crawl_count = pages.crawl_count + 1;
```

**2. INSERT into `crawl_log` (append-only audit)**
```sql
INSERT INTO crawl_log (
    url, domain,
    queued_at, crawl_started_at, crawl_ended_at,
    process_ended_at, indexed_at,
    fetch_ms, process_ms, index_ms, total_ms,  -- SLO tracking
    success
) VALUES (...);
```

Both succeed or both fail (transaction). The `total_ms` column is the
most critical — it's what the `slo_status()` SQL function queries for
P50/P95 latency reports.

#### SLO query

```sql
-- Call this from the API's /stats endpoint
SELECT * FROM slo_status(60);   -- last 60 minutes

-- Returns:
-- total   | passed | failed | slo_pct | p50_ms | p95_ms
-- 1423    | 1382   | 41     | 97.13   | 28400  | 54200
```

The `slo_status()` function is defined in `infra/postgres/init.sql`.
It counts how many crawl_log entries have `total_ms <= 60000` (our SLO target).

---

### 3.4 Redis Signals — Ranking Cache + Cache Invalidation

**File:** `services/indexer/writers/redis_signals.py`

#### Two operations per document

**1. HSET signals hash (ranking cache)**
```
Key:   SIGNALS:{sha256(url)[:16]}
Value: {
    freshness_score:    "0.9832"
    domain_authority:   "0.9200"
    word_count:         "487"
    language:           "en"
    indexed_at:         "1704067245.3"
    content_hash:       "a3f9c2d8e1b74f56"
}
TTL: 7 days (refreshed on every re-crawl)
```

The Phase 5 API reranker reads these in <1ms per document,
vs 5–15ms from PostgreSQL. Critical for keeping query latency < 50ms.

**2. PUBLISH cache invalidation event**
```
Channel: CHANNEL:index_commits
Message: "https://techcrunch.com/...|techcrunch.com|en"

Subscribers: Phase 5 API result cache
Action:      Invalidate any cached search results that may include this URL
```

This keeps the 15-second result cache fresh — after a document is indexed,
any cached queries that could return it are invalidated immediately,
ensuring users see the newly indexed content on the next search.

---

## 4. Parallel Write Strategy

```python
# All four writes launch simultaneously
results = await asyncio.gather(
    self._meili.write(doc),    # ~10ms
    self._qdrant.write(doc),   # ~5ms
    self._pg.write(doc),       # ~10ms
    self._rsig.write(doc),     # ~2ms
    return_exceptions=True,    # don't let one failure cancel others
)

# Each result is either True (success), False (failure), or Exception
meili_ok, qdrant_ok, pg_ok, redis_ok = [
    r if isinstance(r, bool) else False
    for r in results
]

# Total wall-clock time ≈ max(10, 5, 10, 2) = ~12ms
# vs sequential: 10 + 5 + 10 + 2 = 27ms
```

The `return_exceptions=True` flag is critical — without it, a single backend
failure would cancel all remaining writes, causing the document to be
missing from all indexes. With it, each backend write is independent.

---

## 5. Fault Tolerance: Partial Success + DLQ

### Partial success is acceptable

If Meilisearch write fails but Qdrant, PostgreSQL, and Redis succeed:
- Semantic search (Qdrant) finds the document ✓
- Metadata is stored (PostgreSQL) ✓
- Ranking signals are cached (Redis) ✓
- Keyword search (Meilisearch) will find it after retry ✗ (temporary)

We ACK the message after any write attempt. The document is not permanently
lost — re-crawling the URL will re-index it to all backends.

### Dead Letter Queue (DLQ)

If ALL backends fail (network partition, full outage), the message is retried:
```
Attempt 1: fail → wait 1s
Attempt 2: fail → wait 2s
Attempt 3: fail → move to STREAM:dlq, ACK original
```

The DLQ preserves the full IndexedDocument JSON. Operators can replay it:
```bash
# Inspect DLQ
docker exec indexer_redis redis-cli XLEN STREAM:dlq

# Read DLQ entries
docker exec indexer_redis redis-cli XRANGE STREAM:dlq - +

# Replay: move entries back to docs_ready stream
# (manual operation — no automated replay in MVP)
```

---

## 6. Document ID Strategy

Two different ID types are used:

### Meilisearch: SHA-256 hex string (32 chars)
```python
id = hashlib.sha256(url.encode()).hexdigest()[:32]
# "https://example.com/page" → "a3f9c2d8e1b74f5698b2c3d4e5f67890"

# Why: Meilisearch requires alphanumeric IDs.
# Why 32 chars: 128 bits of SHA-256 → negligible collision probability.
# Why stable: same URL → same ID → UPSERT works correctly.
```

### Qdrant: uint64 (8-byte integer)
```python
h = hashlib.sha256(url.encode()).digest()
point_id = int.from_bytes(h[:8], byteorder="big")
# Range: 0 to 2^64 - 1

# Why: Qdrant's native ID type is uint64 (or UUID).
# Why first 8 bytes of SHA-256: uniform distribution, no collisions at scale.
# Why stable: same URL → same point_id → UPSERT works correctly.
```

Both IDs are derived from the same URL using the same hash function — they
are consistent but different types required by their respective APIs.

---

## 7. Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `MEILISEARCH_URL` | `http://localhost:7700` | Meilisearch server |
| `MEILISEARCH_API_KEY` | `masterKey` | Meilisearch master key |
| `MEILISEARCH_INDEX` | `pages` | Index name |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server |
| `QDRANT_COLLECTION` | `pages` | Collection name |
| `DATABASE_URL` | `postgresql://indexer:password@localhost:5432/indexer` | PostgreSQL DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |

### Performance tuning

| Tuning | Setting | Effect |
|---|---|---|
| Faster Meilisearch | Lower `batchSize` in Meilisearch settings | More frequent NRT commits |
| Faster Qdrant build | Lower `indexing_threshold` | HNSW built earlier (more I/O) |
| More indexer replicas | Scale `indexer` service in docker-compose | Parallel consumer workers |
| Reduce write latency | Use gRPC for Qdrant (`qdrant_client` uses it by default) | Already enabled |

---

## 8. File Reference

```
services/indexer/
├── __init__.py
├── Dockerfile                          Lightweight — no ML models
├── requirements.txt                    meilisearch + qdrant-client + asyncpg + redis
├── main.py                             ★ Orchestrator — Redis stream consumer,
│                                         parallel writes, ACK, DLQ, metrics
│
├── writers/
│   ├── __init__.py
│   ├── meilisearch_writer.py           ★ BM25 index writer, schema setup, UPSERT
│   ├── qdrant_writer.py                ★ HNSW vector upsert, collection setup, payload indexes
│   ├── postgres_writer.py              ★ pages UPSERT + crawl_log INSERT (transaction)
│   └── redis_signals.py                ★ Ranking signal cache + cache invalidation pub/sub
│
├── schemas/
│   ├── meilisearch_schema.json         Index schema reference (documentation)
│   └── qdrant_collection.json          Collection config reference (documentation)
│
└── tests/
    ├── __init__.py
    └── test_writers.py                 ★ 40+ tests — no external services needed

★ = files you must understand to work on Phase 4
```

---

## 9. How to Run Phase 4

### Prerequisites

Phases 1, 2, 3 must be running (or `STREAM:docs_ready` pre-populated).

```bash
# Start all required infrastructure
docker compose up -d redis meilisearch qdrant postgres

# Verify all healthy
docker compose ps
```

### Start the indexer

```bash
# Docker (recommended)
docker compose up indexer

# Expected startup log:
# INFO indexer | Indexer service starting pid=1
# INFO indexer.meilisearch | Meilisearch writer ready index=pages
# INFO indexer.qdrant | Qdrant writer ready collection=pages vector_size=384
# INFO indexer.postgres | PostgreSQL writer ready
# INFO indexer.redis_signals | Redis signals writer ready
# INFO indexer | All backends connected: Meilisearch, Qdrant, PostgreSQL, Redis
# INFO indexer | Indexer service running stream=STREAM:docs_ready
```

### Verify documents are indexed

```bash
# Meilisearch: check document count
curl http://localhost:7700/indexes/pages/stats \
  -H "Authorization: Bearer masterKey" | python3 -m json.tool

# Qdrant: check collection info
curl http://localhost:6333/collections/pages | python3 -m json.tool

# PostgreSQL: check page count
docker exec indexer_postgres psql -U indexer -c "SELECT COUNT(*) FROM pages;"

# PostgreSQL: check recent SLO
docker exec indexer_postgres psql -U indexer -c "SELECT * FROM slo_status(60);"

# Redis: check signal count
docker exec indexer_redis redis-cli DBSIZE

# Meilisearch: try a search
curl "http://localhost:7700/indexes/pages/search" \
  -H "Authorization: Bearer masterKey" \
  -d '{"q":"machine learning","limit":3}' | python3 -m json.tool
```

---

## 10. How to Test Phase 4

### Run all tests (no external services needed)

```bash
source .venv/bin/activate

# All Phase 4 tests
pytest services/indexer/tests/test_writers.py -v

# Run specific class
pytest services/indexer/tests/test_writers.py::TestMeilisearchWriter -v
pytest services/indexer/tests/test_writers.py::TestQdrantWriter -v
pytest services/indexer/tests/test_writers.py::TestParallelWriteAggregation -v
```

### Expected output

```
PASSED TestMeilisearchWriter::test_url_to_id_is_stable
PASSED TestMeilisearchWriter::test_url_to_id_is_32_chars
PASSED TestMeilisearchWriter::test_url_to_id_alphanumeric
PASSED TestMeilisearchWriter::test_different_urls_give_different_ids
PASSED TestMeilisearchWriter::test_to_meilisearch_doc_has_required_fields
PASSED TestMeilisearchWriter::test_clean_text_truncated_to_10k
PASSED TestMeilisearchWriter::test_keywords_stored_as_list
PASSED TestMeilisearchWriter::test_entity_names_extracted
PASSED TestMeilisearchWriter::test_entity_types_deduplicated
PASSED TestMeilisearchWriter::test_no_raw_embedding_in_doc
PASSED TestQdrantWriter::test_url_to_point_id_is_uint64
PASSED TestQdrantWriter::test_to_qdrant_point_has_correct_id
PASSED TestQdrantWriter::test_to_qdrant_point_payload_has_required_fields
PASSED TestQdrantWriter::test_payload_does_not_contain_full_text
PASSED TestRedisSignalsWriter::test_signals_key_has_prefix
PASSED TestIndexedDocumentProperties::test_json_roundtrip_preserves_all_fields
PASSED TestParallelWriteAggregation::test_all_true_is_full_success
PASSED TestParallelWriteAggregation::test_exception_treated_as_false

40 passed in 0.41s
```

---

## 11. Metrics & Observability

Prometheus metrics on **port 9104**:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `indexer_docs_indexed_total` | Counter | — | Documents written to ALL backends |
| `indexer_index_duration_seconds` | Histogram | — | Total parallel write time |
| `indexer_index_errors_total` | Counter | `backend` | Failures per backend |
| `indexer_e2e_latency_seconds` | Histogram | — | Discovery → fully indexed |
| `indexer_slo_breached_total` | Counter | — | Docs over 60s SLO |

### Key Grafana queries

```promql
# Indexing throughput (docs/min)
rate(indexer_docs_indexed_total[1m]) * 60

# Index write latency P95
histogram_quantile(0.95, rate(indexer_index_duration_seconds_bucket[5m]))

# Error rate per backend
rate(indexer_index_errors_total{backend="meilisearch"}[5m])
rate(indexer_index_errors_total{backend="qdrant"}[5m])
rate(indexer_index_errors_total{backend="postgres"}[5m])

# SLO pass rate (CRITICAL — this is the main KPI)
1 - (rate(indexer_slo_breached_total[5m]) / rate(indexer_docs_indexed_total[5m]))
# Target: > 0.95 (95% of docs indexed within 60 seconds)

# End-to-end latency distribution
histogram_quantile(0.50, rate(indexer_e2e_latency_seconds_bucket[5m]))   # P50
histogram_quantile(0.95, rate(indexer_e2e_latency_seconds_bucket[5m]))   # P95
```

---

## 12. Common Issues & Fixes

### "Meilisearch index_already_exists error on startup"

This is normal — caught and silently ignored. The index already exists
from a previous run. Settings are re-applied idempotently.

### "Qdrant collection not found"

```bash
# Check Qdrant is running
curl http://localhost:6333/healthz
# Should return: {"title":"qdrant - vector search engine","version":"..."}

# If 404: restart Qdrant
docker compose restart qdrant
```

### "asyncpg.InvalidPasswordError"

```bash
# Check PostgreSQL is running and credentials match .env
docker exec indexer_postgres psql -U indexer -c "SELECT 1;"
# If fails: check POSTGRES_USER and POSTGRES_PASSWORD in .env
```

### "No messages consumed — STREAM:docs_ready is empty"

Phase 3 (Processor) is not running or has no documents to process.
```bash
# Check processor
docker compose ps processor
docker compose logs processor | tail -20

# Check stream
docker exec indexer_redis redis-cli XLEN STREAM:docs_ready

# End-to-end test: seed URLs and start all phases
python scripts/seed_urls.py --count 5
docker compose up -d crawler processor indexer
```

### "DLQ growing — persistent indexer failures"

```bash
# Check DLQ size
docker exec indexer_redis redis-cli XLEN STREAM:dlq

# Read error messages
docker exec indexer_redis redis-cli XRANGE STREAM:dlq - + COUNT 3

# Common cause: backend is down
docker compose ps meilisearch qdrant postgres
```

---

## 13. What Phase 5 Can Now Do

After Phase 4 writes a document, Phase 5 (API) can:

**Full-text keyword search (Meilisearch):**
```
GET /api/v1/search?q=machine+learning&lang=en&limit=10
→ BM25 ranked results with keyword highlighting
→ <mark>machine</mark> <mark>learning</mark> in title/body
```

**Semantic vector search (Qdrant):**
```
Embed "how do neural networks learn?" → 384d vector
ANN search against Qdrant collection
→ Finds "deep learning tutorial", "backpropagation explained"
  even without exact keyword match
```

**Hybrid search (BM25 + ANN fused with RRF — Phase 5's default):**
```
Run both queries in parallel → merge with Reciprocal Rank Fusion
→ Best of both: keyword precision + semantic recall
```

**Reranking with Redis signals:**
```
For each search result, fetch SIGNALS:{url_hash} from Redis (<1ms)
Apply composite score: 0.4×BM25 + 0.4×similarity + 0.1×freshness + 0.1×authority
→ Final ranked result list
```

**SLO reporting (PostgreSQL):**
```
SELECT * FROM slo_status(60);
→ % of documents indexed within 60 seconds — our core KPI
```

**The pipeline is now complete through Phase 4:**
```
URL discovered (Phase 1)
  ↓ ~8s
Raw HTML fetched (Phase 2)
  ↓ ~15s
IndexedDocument produced (Phase 3)
  ↓ ~12ms
All indexes written (Phase 4)
  ────────────────────────
  Total: ~23 seconds average
  P95:   ~50 seconds
  SLO:   < 60 seconds ✓
```

---

*Phase 4 documentation | PRN: 22510103 | Version 1.0*