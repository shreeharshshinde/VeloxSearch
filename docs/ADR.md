# Architecture Decision Records (ADR)
## PRN: 22510103 — Real-Time Web Indexing System

Short notes on every non-obvious technology choice in the project.
Each entry explains: **What we chose → Why → What we rejected → Trade-offs.**

---

### ADR-001: Redis Sorted Set as priority queue

**Chosen:** Redis Sorted Set (ZADD / BZPOPMAX)

**Why:**
- O(log N) insert and pop — fast enough at 50k ops/sec
- Priority built-in via score — no custom logic needed
- BZPOPMAX blocks efficiently when empty (zero CPU idle)
- Redis already in the stack for other uses (Bloom filter, cache, signals)

**Rejected:** Kafka, RabbitMQ, Celery with Redis broker
- Kafka: excellent for production at massive scale but ops-heavy; overkill for MVP
- RabbitMQ: no built-in priority queue across multiple consumers at this scale
- Plain Redis List: FIFO only, no priority

**Trade-off:** At >100M items the Sorted Set becomes memory-heavy.
For production, migrate to Kafka with priority partitioning.

---

### ADR-002: RedisBloom for URL deduplication

**Chosen:** Redis Bloom filter (RedisBloom module, BF.ADD / BF.EXISTS)

**Why:**
- 100M URLs in ~120MB RAM vs ~6GB for a Redis Set
- O(1) lookup regardless of filter size
- 1% false positive rate is acceptable (occasional duplicate crawl is fine)
- Zero false negatives guaranteed — we never miss a truly new URL

**Rejected:** Redis Set, PostgreSQL unique constraint, in-memory Python set
- Redis Set: 50× more RAM for the same number of URLs
- PostgreSQL: network round-trip per URL, too slow at 50k URLs/sec
- Python set: lost on process restart, not shared between workers

**Trade-off:** 1% false positives = ~1 in 100 already-seen URLs gets re-crawled.
At 10M URLs/day this is 100k unnecessary crawls — acceptable.

---

### ADR-003: Python asyncio for Discovery service

**Chosen:** Python 3.11 with asyncio (single-threaded cooperative multitasking)

**Why:**
- All 4 watchers are I/O-bound — they spend time waiting for network
- asyncio runs them concurrently on one thread with zero locking overhead
- Simpler code than threading (no race conditions, no locks)
- Rich ecosystem: httpx, websockets, redis-py all have native async support

**Rejected:** Threading, multiprocessing, Go
- Threading: GIL limits CPU parallelism; adding locks for shared state is complex
- Multiprocessing: share-nothing means duplicating the Bloom filter per process
- Go: excellent for the HTTP fetcher (Phase 2), but Python ecosystem better for Phase 1

---

### ADR-004: Go for the HTTP fetcher (crawler)

**Chosen:** Go 1.22 for the static page fetcher

**Why:**
- Goroutines handle 10,000 concurrent HTTP connections with minimal RAM
- HTTP/2 support out of the box
- Compiled binary — no interpreter overhead per request
- Go's net/http is battle-tested for high-throughput crawling

**Rejected:** Python httpx/aiohttp, Node.js
- Python httpx: 3–5× slower than Go for pure HTTP throughput
- Node.js: good but Go is simpler for a focused crawler binary

**Trade-off:** Two languages in the stack. Mitigated by keeping Go only for the
fetcher binary — all business logic stays in Python.

---

### ADR-005: Meilisearch for full-text index (MVP)

**Chosen:** Meilisearch 1.7 for the inverted full-text index

**Why:**
- Zero-config NRT (near-real-time) refresh: documents searchable in < 1 second
- Simple REST API — no schema definition, no cluster management
- BM25 ranking, prefix search, typo tolerance out of the box
- Single Docker container — no sharding or coordination for MVP
- Fast enough: handles millions of documents on a laptop

**Rejected:** Elasticsearch, Solr, SQLite FTS5
- Elasticsearch: production choice — better for petabyte scale, more query options,
  but requires JVM, cluster setup, index mapping — adds ~1 day to setup
- Solr: same issues as Elasticsearch, older ecosystem
- SQLite FTS5: excellent for small datasets (<1M docs) but doesn't scale

**Migration path:** Meilisearch → Elasticsearch is a configuration swap in
`services/indexer/writers/meilisearch_writer.py`. The API layer doesn't change.

---

### ADR-006: Qdrant for vector search

**Chosen:** Qdrant 1.8 with HNSW index

**Why:**
- HNSW (Hierarchical Navigable Small World): < 5ms ANN query latency
- Payload filtering: filter by domain/language before vector search
- gRPC + REST APIs: flexible integration
- Active development, good Python SDK
- Single container for MVP, cluster mode for production

**Rejected:** Weaviate, Pinecone, pgvector, FAISS
- Weaviate: more complex, heavier resource requirements
- Pinecone: cloud-only, adds external dependency and cost
- pgvector: good for < 1M vectors but slower at scale than dedicated ANN
- FAISS: library, not a server — would need to build the API layer ourselves

---

### ADR-007: sentence-transformers all-MiniLM-L6-v2

**Chosen:** `all-MiniLM-L6-v2` from sentence-transformers

**Why:**
- 384-dimensional output (vs. 768 for BERT-base) — half the storage, twice the speed
- Runs in ~50ms on CPU per document — fast enough without a GPU
- Strong performance on semantic similarity benchmarks
- Pre-trained on 1B+ sentence pairs — no fine-tuning needed for MVP

**Rejected:** OpenAI embeddings API, BERT-base, all-mpnet-base-v2
- OpenAI API: external dependency, cost per call, latency, rate limits
- BERT-base: 768d, ~200ms on CPU — too slow for 60s SLO
- all-mpnet-base-v2: better accuracy but ~100ms on CPU, borderline

**Trade-off:** Lower accuracy than larger models. Fine-tune on our crawled data
post-launch to improve semantic search quality.

---

### ADR-008: Trafilatura for HTML content extraction

**Chosen:** Trafilatura 1.8

**Why:**
- Consistently outperforms newspaper3k, readability-lxml, and justext on
  content extraction benchmarks (measured on CLEF CLEANEVAL dataset)
- Outputs JSON with title, author, date, main text, language
- Handles edge cases: paywalls, login walls, empty pages
- Supports multiple output formats (JSON, XML, CSV, text)

**Rejected:** BeautifulSoup + custom rules, newspaper3k, justext
- BeautifulSoup: requires custom boilerplate-removal logic per site
- newspaper3k: good accuracy but slow (2–3× slower than Trafilatura)
- justext: good precision but lower recall (misses valid content)

---

### ADR-009: Celery for the processing pipeline

**Chosen:** Celery 5.4 with Redis broker

**Why:**
- Mature task queue with retry logic, dead-letter queue, priority queues
- Worker concurrency model matches our use case (CPU-bound NLP + I/O for index writes)
- Built-in rate limiting per task type
- Flower dashboard for real-time worker monitoring

**Rejected:** Redis queue + custom workers, Kafka, RQ (Redis Queue)
- Custom workers: we'd re-implement retry, failure handling, worker management
- Kafka: better for streaming at massive scale, more complex
- RQ: simpler than Celery but fewer features (no priority, limited retry logic)

---

### ADR-010: FastAPI for the Query API

**Chosen:** FastAPI 0.111

**Why:**
- Async-native — handles 10,000+ concurrent connections without blocking
- Auto-generates OpenAPI/Swagger docs from type hints
- Pydantic v2 validation is fast (Rust under the hood)
- WebSocket support built-in (needed for live indexing dashboard)
- Fastest Python web framework for I/O-bound workloads

**Rejected:** Django REST Framework, Flask, Starlette
- DRF: synchronous by default, heavier, not suited for real-time WebSocket
- Flask: synchronous, requires external WebSocket library
- Starlette: FastAPI is built on Starlette; no benefit to using raw Starlette

---

*ADR document | PRN: 22510103 | Last updated: Day 1*