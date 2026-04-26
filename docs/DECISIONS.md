# Design Decisions
## PRN: 22510103 — Real-Time Web Indexing System
## Status: FINAL

This document captures **high-level design decisions** — the "why" behind the overall system shape. For per-technology decisions (why Meilisearch over Elasticsearch, why Go over Python for the fetcher), see [`ADR.md`](ADR.md).

---

## 1. Why real-time indexing matters

Traditional crawlers work on a schedule. Google re-crawls most pages every few days. Bing: weekly. This means a breaking news article, a product launch announcement, or a new research paper can take **days** before it appears in any search index.

This project proves a different architecture is possible: event-driven, streaming, targeting **60 seconds** from URL discovery to searchable result. This is the same latency target as Google News and Bing News — systems built by thousands of engineers. We replicate the core idea in a single codebase.

---

## 2. Event-driven pipeline over scheduled batch jobs

**Decision:** Every phase is triggered by an event, not a cron job.

```
URL discovered → event → crawl → event → process → event → index → searchable
```

**Why:** Batch jobs introduce latency proportional to batch interval. A 30-second batch interval means a URL discovered at second 1 waits up to 30 seconds just to enter the crawl batch. Chain four phases with 30-second batches and the minimum latency is 2 minutes — already outside our 60-second SLO before any actual work is done.

**How it's implemented:**
- Phase 1 → Phase 2: Redis Sorted Set (ZADD → BZPOPMAX)
- Phase 2 → Phase 3: Redis Stream `STREAM:crawl_complete` (XADD → XREADGROUP)
- Phase 3 → Phase 4: Redis Stream `STREAM:docs_ready` (XADD → XREADGROUP)
- Phase 4 → Phase 5: Meilisearch NRT commit + Qdrant immediate + Redis pub/sub cache invalidation

Each handoff is sub-millisecond. The pipeline is limited by actual work (HTTP fetch, NLP), not scheduling overhead.

---

## 3. Microservices per phase, not a monolith

**Decision:** Five separate services (discovery, crawler, processor, indexer, api), each in its own Docker container.

**Why:**
- **Independent scaling:** The processor (CPU-bound NLP) needs more cores than the indexer (fast I/O writes). With separate services, we scale each independently: `docker compose up --scale processor=4`.
- **Independent failure domains:** A processor crash doesn't take down the crawler. The crawler queue keeps filling; the processor restarts and drains it.
- **Independent deployment:** In production, update the indexer without touching the API.
- **Technology isolation:** Go lives in the crawler container only. Python + PyTorch lives in the processor. No dependency conflicts.

**Trade-off:** More Docker containers to manage. Mitigated by `docker-compose.yml` which starts all 12 services (5 app + 7 infra) with a single command.

---

## 4. Hybrid retrieval: BM25 + ANN, not either-or

**Decision:** Both full-text (BM25) and semantic (ANN) retrieval, combined via Reciprocal Rank Fusion.

**Why this matters in practice:**

| Query | Best retrieval | Why |
|---|---|---|
| `"GPT-4 API pricing"` | BM25 | Exact product name + exact term |
| `"how do neural nets learn?"` | ANN | Casual language, no exact term matches |
| `"Python web scraping library"` | Hybrid | Both: keyword "Python" + semantic "web scraping" |
| `"climate change effects"` | Hybrid | Both: keywords exist + semantic enriches recall |

Neither retrieval method dominates across all query types. Hybrid wins on average. This is validated by every major IR benchmark (BEIR, MTEB).

**Implementation:** BM25 (Meilisearch) and ANN (Qdrant) run in parallel via `asyncio.gather()`. Results merged with RRF (k=60). Composite reranker applies freshness and authority signals on top.

---

## 5. Four storage backends — why not just one?

**Decision:** Meilisearch (BM25) + Qdrant (ANN) + PostgreSQL (metadata) + Redis (signals/cache). Each purpose-built.

**Why not just PostgreSQL with FTS + pgvector?**

| Need | PostgreSQL FTS | pgvector | Purpose-built |
|---|---|---|---|
| BM25 keyword search | ✓ (tsvector) | ✗ | Meilisearch: instant NRT, typo tolerance, highlights |
| ANN vector search | ✗ | ✓ (slow at scale) | Qdrant: HNSW, <5ms at 10M vectors |
| Relational metadata | ✓✓ | ✓✓ | PostgreSQL: SLO queries, JOIN, aggregations |
| Sub-ms signal cache | ✗ | ✗ | Redis: HGET in <1ms, pub/sub for cache invalidation |

Using PostgreSQL for everything would require:
- `tsvector` indexes that are 10× slower than Meilisearch at NRT refresh
- `pgvector` HNSW that is 5–20× slower than Qdrant at ANN search
- BLOBS for raw HTML (terrible for an OLTP database)

Specialised tools do their jobs 10× better. The cost is four backends instead of one — managed by Docker Compose.

---

## 6. Content-addressed storage for raw HTML

**Decision:** Store raw HTML in MinIO using `FNV-64a(body)` as the object key, not the URL.

**Why content hash, not URL hash?**

If we keyed by URL and a page's content changes between two crawls, the old HTML is overwritten. We lose the ability to detect that the content changed and trigger reprocessing.

With content hash:
- Same URL, same content → same key → no overwrite, no reprocessing (dedup)
- Same URL, changed content → new key → new MinIO object → triggers full reprocessing

This gives us **change detection for free**: if `CONTENTHASH:{hash}` exists in Redis, the content is unchanged. The HTML is never re-downloaded from MinIO for identical pages.

**Additional benefit:** Reprocessing the entire corpus when we improve the NLP pipeline costs only NLP time, not re-crawl time. The raw HTML is already stored.

---

## 7. Robots.txt compliance is non-negotiable

**Decision:** Check `robots.txt` before every crawl attempt. Block immediately if disallowed. Never override.

**Why:** This is both ethical and practical:
- **Ethical:** Sites publish `robots.txt` to communicate crawl preferences. Ignoring it is a violation of trust and potentially illegal in some jurisdictions.
- **Practical:** Sites that detect crawler misbehaviour block our IP, defeating the purpose.

**Implementation details:**
- `robots.txt` cached per domain in Redis with 24-hour TTL
- `Crawl-delay` from `robots.txt` is honoured via the token bucket rate limiter
- Our `User-Agent: RealtimeIndexer` allows sites to write agent-specific rules
- If `robots.txt` returns 404 or 5xx, we allow crawling (conservative interpretation)
- If `robots.txt` is malformed, we allow crawling (fail open)

**Trade-off:** Some content we could crawl is blocked. This is the correct trade-off.

---

## 8. The 60-second SLO target is P95, not average

**Decision:** Track and report P95 latency. Pass/fail the SLO at P95, not mean.

**Why P95?**
- Mean latency can look good even when 10% of pages take 5 minutes (averaging effect)
- P95 says: "95% of pages are indexed within 60 seconds"
- This is how Google, Cloudflare, AWS, and every major SRE team measures latency
- A mean of 30s sounds great; a P95 of 90s means your user will wait 90+ seconds 1 in 20 times

**Implementation:** PostgreSQL `slo_status()` function uses `PERCENTILE_CONT(0.95)` on `crawl_log.total_ms`. The Grafana dashboard shows the P95 gauge with a red/green threshold at 60,000ms.

---

## 9. Graceful degradation over hard failures

**Decision:** Every component fails open or degrades gracefully. No hard failures that crash the pipeline.

Examples:
- Bloom filter Redis unavailable → allow URL through (crawl it; dedup later)
- `robots.txt` fetch fails → allow crawl (fail open)
- Trafilatura returns `None` → log and skip document (don't crash worker)
- Meilisearch write fails → log error, ACK message, document still in Qdrant/PostgreSQL
- Query embedding fails → fall back to BM25 only

**Why:** In a pipeline with 5 stages and 12 services, transient failures are inevitable. Crashing the entire pipeline on any failure would make the system unreliable. Each stage is designed to be resilient and log failures clearly for debugging.

---

## 10. Observability built in from day one

**Decision:** Prometheus metrics, structured JSON logging, and Grafana dashboards are not afterthoughts — they're part of the initial design.

**Why:** The 60-second SLO is meaningless without measurement. We need to know:
- Is the SLO being met? (P95 latency histogram)
- Where is time being spent? (per-phase histograms)
- Is the queue backing up? (queue depth gauge)
- Which URLs are failing and why? (error counters by type)

**What we built:**
- `shared/metrics.py` defines all Prometheus metrics in one place
- Every service exports metrics on its own port (9101–9105)
- Prometheus scrapes all services every 15 seconds
- Grafana dashboard: queue depth, throughput, per-phase latency, SLO pass rate
- Structured JSON logging with consistent fields across all services

---

*Design decisions document · PRN: 22510103 · Status: FINAL*