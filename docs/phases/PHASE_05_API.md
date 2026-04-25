# Phase 5 — Query API
## PRN: 22510103 | Real-Time Web Indexing System

> **Goal of Phase 5:** Expose a unified search API that makes every indexed
> page instantly searchable using hybrid keyword + semantic retrieval,
> with P99 query latency under **50ms**.

---

## Table of Contents

1. [What Phase 5 Does](#1-what-phase-5-does)
2. [Architecture](#2-architecture)
3. [Retrieval Deep-Dive](#3-retrieval-deep-dive)
   - 3.1 [BM25 Retrieval — Meilisearch](#31-bm25-retrieval--meilisearch)
   - 3.2 [ANN Retrieval — Qdrant](#32-ann-retrieval--qdrant)
   - 3.3 [Hybrid Fusion — RRF](#33-hybrid-fusion--rrf)
   - 3.4 [Composite Reranking](#34-composite-reranking)
4. [Result Cache](#4-result-cache)
5. [WebSocket Live Feed](#5-websocket-live-feed)
6. [API Endpoint Reference](#6-api-endpoint-reference)
7. [File Reference](#7-file-reference)
8. [How to Run Phase 5](#8-how-to-run-phase-5)
9. [How to Test Phase 5](#9-how-to-test-phase-5)
10. [Metrics & Observability](#10-metrics--observability)
11. [Demo Script — End-to-End](#11-demo-script--end-to-end)

---

## 1. What Phase 5 Does

Phase 5 is the user-facing interface to everything built in Phases 1–4.
It exposes a REST + WebSocket API that:

- Accepts search queries
- Runs BM25 + ANN retrieval **in parallel** across Meilisearch and Qdrant
- Fuses results with Reciprocal Rank Fusion
- Reranks using freshness + authority signals from Redis
- Caches results for 15 seconds (invalidated on new index commits)
- Streams real-time pipeline events via WebSocket for the live demo

**Input:** HTTP GET/POST search queries from users or the demo dashboard

**Output:**
- Ranked JSON search results with highlights, scores, and metadata
- Live indexing event stream via WebSocket
- Pipeline health and SLO statistics

**Target latency:** < 50ms P99 (cache hit < 2ms, cache miss ~20ms)

---

## 2. Architecture

```
Client (browser / curl)
         │
    HTTP GET /api/v1/search?q=machine+learning
         │
    ┌────▼──────────────────────────────────────────────────┐
    │                   FastAPI                               │
    │                                                         │
    │  ① Check Redis result cache  ──── HIT ──→ return ~1ms  │
    │                   │ MISS                                │
    │  ② Run in parallel:                                     │
    │    ┌──────────────────┐  ┌──────────────────────────┐  │
    │    │  BM25 Retrieval  │  │    ANN Retrieval          │  │
    │    │  (Meilisearch)   │  │    (Qdrant HNSW)          │  │
    │    │  ~10ms           │  │    embed_query ~50ms       │  │
    │    │  50 candidates   │  │    + search ~5ms           │  │
    │    └────────┬─────────┘  └──────────┬───────────────┘  │
    │             └─────────────┬──────────┘                   │
    │                           ▼                              │
    │  ③ RRF Fusion  →  merge by rank position, k=60          │
    │                           │                              │
    │  ④ Composite reranking:                                  │
    │       score = 0.80 × rrf                                 │
    │             + 0.10 × freshness_score (Redis <1ms)        │
    │             + 0.10 × domain_authority                    │
    │                           │                              │
    │  ⑤ Set cache (15s TTL)                                   │
    │  ⑥ Return top-10 results with highlights                 │
    └───────────────────────────────────────────────────────────┘
         │
    HTTP 200 JSON  +  X-Query-Time-Ms: 23  +  X-Index-Freshness: ...
```

---

## 3. Retrieval Deep-Dive

### 3.1 BM25 Retrieval — Meilisearch

**File:** `services/api/retrieval/bm25.py`

BM25 is the gold-standard algorithm for keyword search. It finds documents
containing the query terms, ranked by:
- **IDF** (inverse document frequency) — rarer terms score higher
- **TF** (term frequency) — more occurrences = higher score, but saturated at k₁=1.2
- **Length normalisation** — long docs don't unfairly dominate

We always fetch **50 candidates** (not just 10) because we need extras for RRF fusion.
After fusion, only the top-10 are returned to the user.

**Searchable fields (priority order):**
```
title       ← highest weight (match here = very relevant)
og_title    ← OpenGraph title
keywords    ← extracted keyword phrases
clean_text  ← full article body (lower weight)
og_description, author, domain
```

**Filter syntax:**
```
language = "en"
domain = "techcrunch.com" AND freshness_score > 0.5
crawled_at > 1704067200
```

**Highlights:** Meilisearch wraps matching terms in `<mark>` tags:
```json
"highlights": {
    "title":   "How <mark>Machine Learning</mark> is Changing Search",
    "summary": "...uses <mark>machine learning</mark> algorithms to..."
}
```

---

### 3.2 ANN Retrieval — Qdrant

**File:** `services/api/retrieval/ann.py`

ANN (Approximate Nearest Neighbour) search finds documents that are
**semantically similar** to the query — even with no word overlap.

**Query → vector → HNSW search:**
```python
# 1. Embed the query (50ms on CPU, cached in Redis for 60s)
query_vector = embed_query("how do neural networks learn?")  # 384 floats

# 2. Search HNSW index (5ms)
results = qdrant.search(
    collection="pages",
    query_vector=query_vector,
    query_filter=Filter(must=[language == "en"]),
    limit=50,
    score_threshold=0.3,   # ignore results with < 30% similarity
)
```

**Query vector caching:**
The embedding model takes ~50ms to run. For popular queries, this would
add 50ms to every request. We cache query vectors in Redis with 60s TTL:

```
Key:   QVEC:{sha256(query)[:16]}
Value: JSON array of 384 floats
TTL:   60 seconds
```

After caching, repeated queries (e.g., autocomplete suggestions) serve
the vector in <1ms instead of 50ms.

**Score threshold = 0.3:**
Cosine similarity of 0.3 means "at least 30% semantically similar".
Below this, documents are likely unrelated noise. We filter them out
to keep the candidate list clean for RRF fusion.

---

### 3.3 Hybrid Fusion — RRF

**File:** `services/api/retrieval/fusion.py`

Reciprocal Rank Fusion merges the BM25 and ANN result lists by RANK
position (not raw score). This works because ranks are comparable across
methods; raw scores are not.

**Formula:**
```
RRF(document) = Σ  1 / (k + rank_in_list_i)
               i=1..L

k = 60  (standard constant — smooths rank differences)
L = 2   (two lists: BM25 + ANN)
```

**Why k=60?**
- k=0: rank 1 gets 1.0, rank 2 gets 0.5 — very sensitive to rank order
- k=60: rank 1 gets 1/61≈0.0164, rank 2 gets 1/62≈0.0161 — very smooth
- k=60 is well-established in information retrieval literature

**Example:**
```
Query: "machine learning transformers"

BM25 (by keyword):    ANN (by meaning):
  1. wikipedia.org      1. arxiv.org/transformers
  2. techcrunch.com     2. wikipedia.org
  3. medium.com         3. medium.com

RRF scores:
  wikipedia.org:       1/(60+1) + 1/(60+2) = 0.01639 + 0.01613 = 0.03252
  arxiv.org:           0        + 1/(60+1) = 0       + 0.01639 = 0.01639
  techcrunch.com:      1/(60+2) + 0        = 0.01613 + 0       = 0.01613
  medium.com:          1/(60+3) + 1/(60+3) = 0.01587 + 0.01587 = 0.03174

Final order: wikipedia > medium > arxiv > techcrunch
```

Wikipedia appears #1 because it's highly relevant in BOTH lists.
Medium appears #2 because it's #3 in both lists (also in both).

---

### 3.4 Composite Reranking

After RRF, we apply a lightweight reranker using signals stored in Redis:

```python
final_score = (
    0.80 × rrf_normalised      # semantic + keyword relevance
  + 0.10 × freshness_score     # how recently was this published?
  + 0.10 × domain_authority    # how trustworthy is the source?
)
```

**RRF normalisation:**
```python
# Max possible RRF = 1/(60+1) + 1/(60+1) ≈ 0.0328 (doc is #1 in both lists)
rrf_normalised = min(1.0, rrf_raw / 0.033)
```

**Why these weights?**
- 0.80 for RRF: relevance is the primary signal — we don't want freshness
  to override highly relevant old content
- 0.10 for freshness: news queries need recent results; conceptual queries don't
- 0.10 for authority: Wikipedia should edge out a random blog when equally relevant

In production, a LightGBM learning-to-rank model learns optimal weights
from click-through data.

---

## 4. Result Cache

**File:** `services/api/cache/result_cache.py`

### How it works

```
Cache key = SHA-256(query + mode + lang + domain + limit + offset)[:24]
Cache TTL = 15 seconds

Request → check Redis → HIT → return immediately (~1ms)
       →             → MISS → full retrieval → cache result → return
```

### Cache invalidation via pub/sub

When Phase 4 (Indexer) commits a new document, it publishes:
```
PUBLISH CHANNEL:index_commits "https://techcrunch.com/...|techcrunch.com|en"
```

The API subscribes to this channel in a background asyncio task.
On each commit event, it updates a `LAST_INDEX_COMMIT` timestamp in Redis.

On every cache retrieval, we compare the cache entry's `_cached_at`
against `LAST_INDEX_COMMIT`. If `_cached_at < LAST_INDEX_COMMIT`,
the cache is stale — a new document was indexed after this result was cached.
We delete the stale entry and force a fresh retrieval.

This ensures users never get results that are more than one query cycle stale.

### Cache hit rates (typical)

| Query type | Hit rate | Why |
|---|---|---|
| Popular queries ("machine learning") | ~99% | Many users, same query |
| Autocomplete (per-keystroke) | ~80% | Small query variations |
| Unique queries | 0% | Never seen before |
| After new index commit | 0% | Invalidated |

---

## 5. WebSocket Live Feed

**File:** `services/api/routers/websocket.py`

The WebSocket at `ws://localhost:8000/ws/feed` streams real-time pipeline
events to connected clients (the demo dashboard).

### Event types

```json
{"type": "crawl_complete",  "url": "...", "domain": "...", "fetch_ms": 342}
{"type": "indexed",         "url": "...", "latency_ms": 28400, "slo_passed": true}
{"type": "robots_blocked",  "url": "...", "domain": "..."}
{"type": "stats",           "queue_depth": 14, "clients": 2}
{"type": "ping",            "timestamp": "2024-01-15T10:23:45Z"}
```

### Architecture

A background asyncio task (`stream_pipeline_events`) runs from API startup.
It uses `XREAD` on `STREAM:crawl_complete` and `STREAM:docs_ready` with
a 1-second block timeout. When new records arrive, it broadcasts to all
connected WebSocket clients via the `broadcast()` function.

Multiple clients can connect simultaneously — all receive the same events.
Disconnected clients are detected and removed from the broadcast set.

---

## 6. API Endpoint Reference

### `GET /api/v1/search`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `q` | string | required | Search query (1–500 chars) |
| `mode` | enum | `hybrid` | `bm25` \| `semantic` \| `hybrid` |
| `lang` | string | null | ISO 639-1 language filter |
| `domain` | string | null | Domain filter |
| `schema_type` | string | null | Schema.org type filter |
| `since` | float | null | Unix timestamp lower bound |
| `limit` | int | 10 | Results per page (1–50) |
| `offset` | int | 0 | Pagination offset |

**Response:**
```json
{
  "query": "machine learning",
  "mode": "hybrid",
  "total": 1423,
  "took_ms": 23,
  "index_freshness": "2024-01-15T10:23:45Z",
  "from_cache": false,
  "results": [
    {
      "url":              "https://techcrunch.com/2024/01/15/ai-search",
      "domain":           "techcrunch.com",
      "title":            "How AI is Reshaping Web Search",
      "summary":          "The landscape of web search has changed...",
      "og_image":         "https://techcrunch.com/img/ai.jpg",
      "author":           "Jane Smith",
      "published_at":     "2024-01-15T10:00:00Z",
      "language":         "en",
      "schema_type":      "Article",
      "score":            0.9241,
      "bm25_score":       0.8100,
      "semantic_score":   0.8734,
      "freshness_score":  0.9800,
      "domain_authority": 0.9200,
      "indexed_at":       1705316645.3,
      "highlights": {
        "title":   "How AI is Reshaping Web <mark>Search</mark>",
        "summary": "uses <mark>machine learning</mark> to..."
      }
    }
  ]
}
```

**Response headers:**
```
X-Query-Time-Ms:      23
X-Results-From-Cache: false
X-Index-Freshness:    2024-01-15T10:23:45Z
```

---

### `POST /api/v1/urls/submit`

```json
// Request
{"url": "https://example.com/new-article", "priority": "high"}

// Response
{
  "task_id": "abc123de",
  "url": "https://example.com/new-article",
  "queued_at": "2024-01-15T10:23:45Z",
  "estimated_index_time": "45–60 seconds",
  "priority": "high"
}
```

---

### `GET /api/v1/health`

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "backends": {
    "redis": "healthy",
    "meilisearch": "healthy",
    "qdrant": "healthy",
    "postgres": "healthy"
  }
}
```

---

### `GET /api/v1/stats`

```json
{
  "urls_indexed_last_60s": 15,
  "urls_indexed_last_hour": 842,
  "total_pages_indexed": 28472,
  "slo_pass_rate_pct": 97.3,
  "p50_latency_ms": 28400.0,
  "p95_latency_ms": 54200.0,
  "queue_depth": 14,
  "meilisearch_docs": 28472,
  "qdrant_vectors": 28100,
  "uptime_seconds": 86423,
  "index_freshness": "2024-01-15T10:23:45Z"
}
```

---

### `WebSocket ws://localhost:8000/ws/feed`

Connect with any WebSocket client. Events are JSON objects with `type` and `timestamp`.

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/feed");
ws.onmessage = (e) => {
    const event = JSON.parse(e.data);
    if (event.type === "indexed") {
        console.log(`Indexed: ${event.url} in ${event.latency_ms}ms`);
    }
};
```

---

## 7. File Reference

```
services/api/
├── __init__.py
├── Dockerfile
├── requirements.txt
├── main.py                      ★ FastAPI app, lifespan, middleware, startup
├── demo_dashboard.html            Live demo dashboard (dark theme, WebSocket)
│
├── routers/
│   ├── __init__.py
│   ├── search.py                ★ GET+POST /search, POST /urls/submit
│   ├── status.py                  GET /health, GET /stats
│   └── websocket.py             ★ WS /ws/feed, broadcast(), event streamer
│
├── retrieval/
│   ├── __init__.py
│   ├── bm25.py                  ★ Meilisearch BM25 search (50 candidates)
│   ├── ann.py                   ★ Qdrant HNSW ANN search + query vector cache
│   └── fusion.py                ★ RRF merger + composite reranker
│
├── cache/
│   ├── __init__.py
│   └── result_cache.py          ★ 15s Redis cache + pub/sub invalidation
│
├── models/
│   ├── __init__.py
│   ├── request.py                 SearchRequest, URLSubmitRequest (Pydantic)
│   └── response.py                SearchResponse, SearchResult, PipelineStats
│
└── tests/
    ├── __init__.py
    └── test_search.py           ★ 45+ tests: RRF, cache keys, models, formatting

★ = files you must understand to work on Phase 5
```

---

## 8. How to Run Phase 5

```bash
# Start all infrastructure + phases 1-4
docker compose up -d

# Start the API (last to start)
docker compose up api

# Expected startup:
# INFO api | API service starting version=1.0.0
# INFO api | Redis connected
# INFO api | Search components initialised
# INFO api | Metrics server started port=9105
# INFO api | API service ready docs=http://0.0.0.0:8000/docs

# Open interactive docs
open http://localhost:8000/docs

# Open live demo dashboard
open http://localhost:8000/demo   # (served as static file)

# Try a search
curl "http://localhost:8000/api/v1/search?q=machine+learning&limit=3" | python3 -m json.tool

# Check health
curl http://localhost:8000/api/v1/health | python3 -m json.tool

# Submit a URL for indexing
curl -X POST http://localhost:8000/api/v1/urls/submit \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Web_indexing","priority":"high"}'

# Stream live events
wscat -c ws://localhost:8000/ws/feed
```

---

## 9. How to Test Phase 5

```bash
source .venv/bin/activate

# All Phase 5 tests (no external services)
pytest services/api/tests/test_search.py -v

# Specific test classes
pytest services/api/tests/test_search.py::TestRRFFusion -v
pytest services/api/tests/test_search.py::TestCacheKey -v
pytest services/api/tests/test_search.py::TestRequestModels -v
```

### Expected output

```
PASSED TestRRFFusion::test_doc_in_both_lists_scores_higher
PASSED TestRRFFusion::test_empty_bm25_returns_ann_results
PASSED TestRRFFusion::test_limit_respected
PASSED TestRRFFusion::test_offset_pagination
PASSED TestRRFFusion::test_composite_score_uses_freshness
PASSED TestRRFFusion::test_duplicate_urls_deduplicated
PASSED TestRRFFusion::test_composite_score_between_0_and_1
PASSED TestRRFFusion::test_bm25_only_mode
PASSED TestRRFFusion::test_ann_only_mode
PASSED TestCacheKey::test_same_params_same_key
PASSED TestCacheKey::test_different_query_different_key
PASSED TestCacheKey::test_key_has_correct_prefix
PASSED TestCacheKey::test_key_is_fixed_length
PASSED TestRequestModels::test_search_request_defaults
PASSED TestRequestModels::test_search_request_strips_whitespace
PASSED TestRequestModels::test_invalid_mode_rejected
PASSED TestResultFormatting::test_og_title_preferred_over_title
PASSED TestResultFormatting::test_highlights_extracted

45 passed in 0.38s
```

---

## 10. Metrics & Observability

Prometheus metrics on **port 9105**:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `indexer_search_requests_total` | Counter | `mode` | Total search requests |
| `indexer_search_latency_seconds` | Histogram | — | Query end-to-end latency |
| `indexer_search_cache_hits_total` | Counter | — | Cache hit count |
| `indexer_search_cache_misses_total` | Counter | — | Cache miss count |

```promql
# Query throughput (requests/min)
rate(indexer_search_requests_total[1m]) * 60

# Cache hit rate
rate(indexer_search_cache_hits_total[5m]) /
  (rate(indexer_search_cache_hits_total[5m]) + rate(indexer_search_cache_misses_total[5m]))

# P99 query latency
histogram_quantile(0.99, rate(indexer_search_latency_seconds_bucket[5m]))

# Requests by mode
rate(indexer_search_requests_total{mode="hybrid"}[1m]) * 60
rate(indexer_search_requests_total{mode="bm25"}[1m]) * 60
```

---

## 11. Demo Script — End-to-End

```bash
# Full end-to-end demo (run this for the HoD)
python scripts/demo.py

# What it does:
#  1. Checks API health (all backends green)
#  2. Confirms URL not yet in index (0 results)
#  3. Submits URL with high priority
#  4. Shows real-time pipeline progress (queued → crawling → processing → indexed)
#  5. Waits for "indexed" event
#  6. Searches for the URL → appears in results
#  7. Reports total end-to-end latency + SLO verdict (PASSED / EXCEEDED)
#  8. Shows per-stage timing breakdown

# Expected output:
# ── System health check ──────────────────────────────────
#  ✓ API healthy at http://localhost:8000/api/v1
# ── Step 1: Verify URL is NOT yet indexed ───────────────
#  ✓ Confirmed: 0 results
# ── Step 2: Submit URL for real-time indexing ────────────
#  ✓ URL submitted — task_id: abc123de
#  ✓ Estimated index time: ~45–60s
# ── Step 3: Live pipeline progress ───────────────────────
#  ● queued        URL added to priority queue           0.0s
#  ● crawling      Fetching page content...              2.1s
#  ● crawled       Raw HTML stored in MinIO              4.8s
#  ● processing    NLP pipeline running...               7.3s
#  ● embedding     Generating semantic embedding         14.2s
#  ● indexing      Writing to search indexes             15.8s
#  ● done          Page is searchable!                   16.2s
# ── Step 4: SLO verdict ─────────────────────────────────
#  Total latency: 16.2s  —  60s SLO: PASSED ✓
# ── Step 5: Search for the newly indexed page ────────────
#  ✓ Found 1 result(s) in 18ms
#  Result 1:
#    Title:   Web indexing — Wikipedia
#    URL:     https://en.wikipedia.org/wiki/Web_indexing
#    Score:   0.924
#    Indexed: 2024-01-15T10:23:45Z
```

---

*Phase 5 documentation | PRN: 22510103 | Version 1.0*