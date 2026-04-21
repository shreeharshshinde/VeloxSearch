# MASTER PLAN — Real-Time Web Indexing System
## PRN: 22510103 | Deadline: 3 Days | Target SLO: Index new pages within 60 seconds

---

> **Mission:** Design, build, and demonstrate a production-grade web indexing system where any newly discovered URL becomes fully searchable within 60 seconds of discovery.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [System Architecture](#2-system-architecture)
3. [Technology Stack](#3-technology-stack)
4. [Project Directory Structure](#4-project-directory-structure)
5. [Pipeline Phases — Deep Dive](#5-pipeline-phases--deep-dive)
6. [Database & Storage Design](#6-database--storage-design)
7. [API Design](#7-api-design)
8. [Deployment Architecture](#8-deployment-architecture)
9. [Monitoring & Observability](#9-monitoring--observability)
10. [3-Day Sprint Plan](#10-3-day-sprint-plan)
11. [Environment Setup](#11-environment-setup)
12. [Configuration Reference](#12-configuration-reference)
13. [Testing Strategy](#13-testing-strategy)
14. [Demo Script](#14-demo-script)

---

## 1. Project Overview

### Problem Statement
Traditional web crawlers operate on a schedule — they re-crawl the web every few days or weeks. This means new pages can take days before they appear in any search index. This project eliminates that delay by building an **event-driven, real-time indexing pipeline** where the end-to-end latency from URL discovery to search availability is **under 60 seconds**.

### Core Requirements
| Requirement | Target |
|---|---|
| Discovery-to-index latency | < 60 seconds (P95) |
| URL ingestion rate | 50,000+ URLs / second |
| Concurrent crawl workers | 10,000 |
| Query response time | < 50ms (P99) |
| System uptime | 99.9% |
| robots.txt compliance | 100% |

### What Makes This Hard
- **Speed vs. politeness:** Crawling fast without hammering servers
- **Scale:** Deduplicating billions of URLs efficiently
- **Real-time enrichment:** NLP + embedding generation in seconds, not minutes
- **Index freshness:** Search indexes normally require minutes to refresh; we need seconds
- **Fault tolerance:** Any stage can fail — the pipeline must recover without losing data

### Scope (3-Day Build)
For the 3-day deadline, we build a **working MVP** that demonstrates the full pipeline end-to-end. We simplify infrastructure (single-node instead of distributed) but keep all the core logic intact so it can scale to production.

---

## 2. System Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      REAL-TIME INDEXING SYSTEM                       │
│                                                                       │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐   │
│  │  DISCOVERY   │───▶│   MESSAGE    │───▶│      CRAWLER         │   │
│  │    LAYER     │    │    QUEUE     │    │       POOL           │   │
│  │              │    │   (Redis     │    │  (Go + Playwright)   │   │
│  │ • WebSub     │    │   Streams)   │    │                      │   │
│  │ • CT Logs    │    └──────────────┘    └──────────┬───────────┘   │
│  │ • Link ext.  │                                    │               │
│  │ • Sitemaps   │                                    ▼               │
│  └──────────────┘                        ┌──────────────────────┐   │
│                                          │    OBJECT STORAGE    │   │
│                                          │  (Raw HTML + S3 API) │   │
│                                          └──────────┬───────────┘   │
│                                                      │               │
│                                                      ▼               │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    PROCESSING PIPELINE                        │   │
│  │                                                               │   │
│  │   HTML Cleaner ──▶ NLP Enrichment ──▶ Embedding ──▶ Features │   │
│  │  (trafilatura)     (spaCy/fastText)  (SentBERT)   (extruct)  │   │
│  └──────────────────────────────┬────────────────────────────────┘   │
│                                  │                                    │
│                    ┌─────────────┼─────────────┐                    │
│                    ▼             ▼              ▼                    │
│           ┌──────────────┐ ┌─────────┐ ┌────────────┐             │
│           │  INVERTED    │ │ VECTOR  │ │  METADATA  │             │
│           │   INDEX      │ │  INDEX  │ │   STORE    │             │
│           │(Elasticsearch│ │(Qdrant) │ │(PostgreSQL)│             │
│           │   / Meilisearch│        │ │            │             │
│           └──────┬───────┘ └────┬───┘ └─────┬──────┘             │
│                  └──────────────┼────────────┘                    │
│                                  ▼                                    │
│                        ┌──────────────────┐                         │
│                        │   QUERY API       │                         │
│                        │  (FastAPI/Python) │                         │
│                        │  Hybrid Retrieval │                         │
│                        │  BM25 + ANN + RRF │                         │
│                        └──────────────────┘                         │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow (60-Second Timeline)

```
T+00s  URL discovered (sitemap ping / CT log / link extraction)
T+02s  URL deduplicated via Bloom filter, pushed to Redis priority queue
T+05s  Crawler worker picks up URL, checks robots.txt cache
T+08s  HTTP fetch (static) or Playwright render (JS-heavy)
T+12s  Raw HTML written to MinIO object store, content hash checked
T+15s  Processing worker picks up document from queue
T+20s  HTML cleaning via Trafilatura — boilerplate stripped
T+25s  spaCy NLP pipeline runs: language, NER, keywords
T+32s  Sentence-BERT encodes semantic embedding (768d vector)
T+38s  extruct parses structured metadata (Schema.org, OG tags)
T+42s  All features assembled into document object
T+45s  Inverted index write → Meilisearch / Elasticsearch (NRT)
T+47s  Vector upsert → Qdrant HNSW index
T+49s  Metadata write → PostgreSQL
T+51s  Ranking signals updated → Redis feature store
T+55s  Index refresh committed, cache invalidated
T+58s  Page appears in search results ✓
```

### Component Interaction Map

```
                     ┌─────────────┐
                     │  WebSub Hub │ (external)
                     └──────┬──────┘
                            │ HTTP POST (ping)
                     ┌──────▼──────┐    ┌──────────────┐
                     │  Discovery  │───▶│ Bloom Filter │
                     │   Service   │    │  (Redis)     │
                     └──────┬──────┘    └──────────────┘
                            │ LPUSH
                     ┌──────▼──────┐
                     │  URL Queue  │ (Redis Streams / Sorted Set)
                     └──────┬──────┘
                            │ BRPOP / XREAD
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         ┌─────────┐  ┌─────────┐  ┌─────────┐
         │Crawler 1│  │Crawler 2│  │Crawler N│ (worker pool)
         └────┬────┘  └────┬────┘  └────┬────┘
              └────────────┼────────────┘
                           │ PUT object
                    ┌──────▼──────┐
                    │  MinIO (S3) │ (raw HTML store)
                    └──────┬──────┘
                           │ event notification
                    ┌──────▼──────┐
                    │  Processor  │ (NLP + embedding pipeline)
                    └──────┬──────┘
                           │ parallel writes
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌────────────┐  ┌──────────┐  ┌──────────────┐
   │Meilisearch │  │  Qdrant  │  │  PostgreSQL  │
   │(full-text) │  │(vectors) │  │  (metadata)  │
   └────────────┘  └──────────┘  └──────────────┘
          │                │                │
          └────────────────┼────────────────┘
                           │ query
                    ┌──────▼──────┐
                    │  Query API  │ (FastAPI)
                    └─────────────┘
```

---

## 3. Technology Stack

### Why Each Technology Was Chosen

#### Backend Language: Python 3.11 + Go 1.22
- **Python** — data processing, NLP, ML inference, API (rich ecosystem for AI/NLP tasks)
- **Go** — HTTP fetcher, high-throughput URL processor (10x faster for I/O-bound crawling)

#### Message Queue: Redis 7 (Streams + Sorted Sets)
- Sub-millisecond latency
- Sorted Set gives us priority-based URL scheduling (domain authority as score)
- Streams give us persistent, replayable event log
- Bloom filter module (RedisBloom) for O(1) dedup
- **Alternative considered:** Kafka — better for production at massive scale, but adds ops complexity; Redis is sufficient for MVP and early production

#### Full-Text Search: Meilisearch (MVP) / Elasticsearch (Production)
- **Meilisearch** — zero-config, instant NRT (< 1s refresh), REST API out of the box. Perfect for 3-day MVP.
- **Elasticsearch** — use in production for petabyte-scale, fine-grained shard control, richer query DSL
- Both support BM25 ranking out of the box

#### Vector Search: Qdrant
- Native HNSW index with < 5ms ANN query latency
- Payload filtering (filter by domain, date, language before vector search)
- gRPC + REST APIs
- Runs in a single Docker container for MVP

#### Object Storage: MinIO (S3-compatible)
- Local S3-compatible store for raw HTML
- Content-addressed storage — URL hash as key, dedup by content hash
- In production, replace with AWS S3 or GCS

#### Relational DB: PostgreSQL 16
- Stores page metadata, crawl history, domain authority scores
- Used by the ranking pipeline and admin dashboard

#### NLP: spaCy 3.7 + Trafilatura + sentence-transformers
- **Trafilatura** — state-of-the-art boilerplate removal, outperforms BeautifulSoup + justext
- **spaCy** with `en_core_web_sm` — fast NER, POS tagging, sentence segmentation
- **sentence-transformers** with `all-MiniLM-L6-v2` — 384d embeddings, 5x faster than BERT-large, runs on CPU

#### Web Crawling: Playwright (Python) + httpx
- **httpx** — async HTTP/2 client for static pages (sub-100ms per request)
- **Playwright** — headless Chromium for JS-rendered pages (SPAs, React, Next.js sites)

#### API Framework: FastAPI
- Async-native, auto OpenAPI docs, Pydantic validation
- WebSocket support for real-time indexing status dashboard

#### Monitoring: Prometheus + Grafana
- Custom metrics: discovery rate, crawl latency, index commit time, queue depth
- Pre-built dashboards for the 60s SLO tracking

#### Containerisation: Docker + Docker Compose
- Single `docker-compose up` launches entire stack for MVP
- Every service isolated; easy to swap components

### Full Technology Stack Table

| Layer | Technology | Version | Purpose |
|---|---|---|---|
| Language (ML/API) | Python | 3.11 | Processing, API, orchestration |
| Language (Crawler) | Go | 1.22 | High-perf HTTP fetcher |
| Queue | Redis | 7.2 | URL queue, Bloom filter, feature store |
| Full-text index | Meilisearch | 1.7 | Inverted index, BM25 search |
| Vector index | Qdrant | 1.8 | ANN dense retrieval |
| Object store | MinIO | latest | Raw HTML storage |
| Relational DB | PostgreSQL | 16 | Metadata, crawl history |
| HTML extractor | Trafilatura | 1.8 | Boilerplate removal |
| NLP | spaCy | 3.7 | NER, tokenisation, keywords |
| Embeddings | sentence-transformers | 2.6 | Semantic vector generation |
| Metadata parser | extruct | 0.16 | Schema.org, OG, JSON-LD |
| Language detect | fastText | 0.9 | Page language ID |
| Crawler (static) | httpx | 0.27 | Async HTTP/2 fetching |
| Crawler (JS) | Playwright | 1.43 | Headless Chromium |
| API framework | FastAPI | 0.111 | REST + WebSocket API |
| Validation | Pydantic | 2.7 | Data models |
| Task queue | Celery | 5.4 | Distributed processing workers |
| Monitoring | Prometheus | 2.52 | Metrics collection |
| Dashboards | Grafana | 11 | Visualisation |
| Container | Docker Compose | 2.27 | Local orchestration |
| CI/CD | GitHub Actions | — | Automated testing |

---

## 4. Project Directory Structure

```
realtime-indexer/
│
├── README.md                          # Quick start guide
├── MASTER_PLAN.md                     # This document
├── docker-compose.yml                 # Full stack: all services
├── docker-compose.dev.yml             # Dev overrides (hot reload)
├── .env.example                       # Environment variable template
├── .env                               # Your local config (gitignored)
├── Makefile                           # Common commands (make start, make test)
│
├── services/                          # One directory per microservice
│   │
│   ├── discovery/                     # URL Discovery Service (Python)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py                    # Service entrypoint
│   │   ├── watchers/
│   │   │   ├── __init__.py
│   │   │   ├── websub.py              # WebSub/PubSubHubbub subscriber
│   │   │   ├── sitemap.py             # Sitemap poller
│   │   │   ├── ct_logs.py             # Certificate transparency log scanner
│   │   │   └── link_extractor.py      # Outbound link parser
│   │   ├── queue/
│   │   │   ├── __init__.py
│   │   │   ├── redis_queue.py         # Priority queue operations
│   │   │   └── bloom_filter.py        # URL deduplication
│   │   └── tests/
│   │       ├── test_websub.py
│   │       └── test_dedup.py
│   │
│   ├── crawler/                       # Web Crawler Service (Go + Python)
│   │   ├── Dockerfile
│   │   ├── go.mod
│   │   ├── go.sum
│   │   ├── cmd/
│   │   │   └── main.go                # Go fetcher entrypoint
│   │   ├── internal/
│   │   │   ├── fetcher/
│   │   │   │   ├── http_fetcher.go    # Static page fetcher
│   │   │   │   └── robots.go          # robots.txt parser + cache
│   │   │   ├── ratelimit/
│   │   │   │   └── token_bucket.go    # Per-domain rate limiter
│   │   │   └── storage/
│   │   │       └── minio_client.go    # Raw HTML upload to MinIO
│   │   ├── playwright_crawler/        # Python subprocess for JS pages
│   │   │   ├── requirements.txt
│   │   │   ├── crawler.py
│   │   │   └── page_classifier.py     # Detect if JS rendering needed
│   │   └── tests/
│   │       ├── fetcher_test.go
│   │       └── robots_test.go
│   │
│   ├── processor/                     # NLP Processing Service (Python)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py                    # Celery worker entrypoint
│   │   ├── pipeline/
│   │   │   ├── __init__.py
│   │   │   ├── cleaner.py             # Trafilatura HTML cleaner
│   │   │   ├── nlp.py                 # spaCy NER + keywords
│   │   │   ├── embedder.py            # sentence-transformers encoder
│   │   │   ├── metadata.py            # extruct Schema.org parser
│   │   │   └── language.py            # fastText language detection
│   │   ├── models/
│   │   │   ├── document.py            # Pydantic document schema
│   │   │   └── features.py            # Feature vector schema
│   │   └── tests/
│   │       ├── test_cleaner.py
│   │       ├── test_embedder.py
│   │       └── fixtures/
│   │           └── sample_pages/      # HTML fixtures for testing
│   │
│   ├── indexer/                       # Index Writer Service (Python)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py
│   │   ├── writers/
│   │   │   ├── __init__.py
│   │   │   ├── meilisearch_writer.py  # Full-text index writer
│   │   │   ├── qdrant_writer.py       # Vector index writer
│   │   │   ├── postgres_writer.py     # Metadata store writer
│   │   │   └── redis_signals.py       # Ranking signals writer
│   │   ├── schemas/
│   │   │   ├── meilisearch_schema.json
│   │   │   └── qdrant_collection.json
│   │   └── tests/
│   │       └── test_writers.py
│   │
│   └── api/                           # Query API Service (Python FastAPI)
│       ├── Dockerfile
│       ├── requirements.txt
│       ├── main.py                    # FastAPI app entrypoint
│       ├── routers/
│       │   ├── __init__.py
│       │   ├── search.py              # /search endpoint
│       │   ├── status.py              # /status, /health endpoints
│       │   └── websocket.py           # Real-time indexing events
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── bm25.py                # Meilisearch BM25 retrieval
│       │   ├── ann.py                 # Qdrant ANN retrieval
│       │   ├── fusion.py              # Reciprocal Rank Fusion
│       │   └── reranker.py            # LightGBM reranker (optional)
│       ├── cache/
│       │   └── result_cache.py        # 15s Redis result cache
│       ├── models/
│       │   ├── request.py             # SearchRequest Pydantic model
│       │   └── response.py            # SearchResponse Pydantic model
│       └── tests/
│           └── test_search.py
│
├── shared/                            # Shared code across services
│   ├── __init__.py
│   ├── config.py                      # Centralised config from .env
│   ├── logging.py                     # Structured JSON logging
│   ├── metrics.py                     # Prometheus metrics definitions
│   └── models/
│       ├── url_task.py                # URLTask dataclass (queue message)
│       └── indexed_doc.py             # IndexedDocument dataclass
│
├── infra/                             # Infrastructure configuration
│   ├── redis/
│   │   └── redis.conf                 # Redis config (memory limits, persistence)
│   ├── meilisearch/
│   │   └── config.toml                # Meilisearch index settings
│   ├── qdrant/
│   │   └── config.yaml                # Qdrant collection config
│   ├── postgres/
│   │   └── init.sql                   # DB schema + initial data
│   ├── prometheus/
│   │   └── prometheus.yml             # Scrape config
│   └── grafana/
│       └── dashboards/
│           └── indexing_pipeline.json # Pre-built dashboard
│
├── scripts/                           # Utility scripts
│   ├── setup.sh                       # One-time environment setup
│   ├── seed_urls.py                   # Load test URLs into queue
│   ├── benchmark.py                   # Measure end-to-end latency
│   └── demo.py                        # Live demo script for HoD presentation
│
└── docs/                              # Additional documentation
    ├── API.md                         # API endpoint reference
    ├── SCALING.md                     # Production scaling guide
    └── DECISIONS.md                   # Architecture decision records
```

---

## 5. Pipeline Phases — Deep Dive

### Phase 1: Discovery Service

**File:** `services/discovery/`

The discovery service runs 4 watchers concurrently using Python's `asyncio`. Each watcher produces URL tasks and pushes them to the shared priority queue.

#### WebSub Subscriber (`watchers/websub.py`)
Sites that support WebSub (PubSubHubbub) can notify our server the instant their content changes. We expose a `/websub/callback` HTTP endpoint. When a ping arrives, we extract all URLs from the notification payload and enqueue them.

```python
# Core logic sketch
async def handle_ping(notification: WebSubNotification):
    urls = extract_urls_from_feed(notification.body)
    for url in urls:
        if not await bloom_filter.exists(url):
            await bloom_filter.add(url)
            await queue.push(URLTask(url=url, source="websub", priority=10))
```

#### Sitemap Poller (`watchers/sitemap.py`)
Polls `sitemap.xml` and `sitemap_index.xml` for registered domains every 30 seconds. Uses `lastmod` attribute to detect genuinely new/changed URLs only.

#### CT Log Scanner (`watchers/ct_logs.py`)
Consumes the Certstream WebSocket feed — a real-time stream of all SSL certificate issuances worldwide. Every new cert = potential new domain. We extract the domain, construct the root URL, and enqueue it.

```python
# Certstream gives us new domains in real time
async def on_cert(message):
    domains = message['data']['leaf_cert']['all_domains']
    for domain in domains:
        url = f"https://{domain}/"
        await enqueue_if_new(url, source="ct_log", priority=5)
```

#### URL Queue (`queue/redis_queue.py`)
Redis Sorted Set where score = priority (higher = crawled sooner). Domain authority (Moz/Majestic score, approximated locally) boosts priority. Real-time sites (news, blogs) get higher priority than static pages.

```python
# Priority scoring
def compute_priority(url: str, source: str, domain_authority: int) -> float:
    base = {"websub": 10, "sitemap": 7, "ct_log": 5, "link": 3}[source]
    return base + (domain_authority / 100)  # 0–1 domain authority bonus
```

#### Bloom Filter (`queue/bloom_filter.py`)
RedisBloom BF.ADD / BF.EXISTS operations. 1% false positive rate at 100M URLs uses only ~120MB RAM. Prevents duplicate crawl of the same URL. TTL-based reset every 24 hours to allow re-crawl of updated pages.

---

### Phase 2: Crawler Service

**File:** `services/crawler/`

The crawler pool is a set of workers that pull from the URL queue and fetch pages. Two fetching modes:

#### Static Fetcher (Go — `internal/fetcher/http_fetcher.go`)
Used for: plain HTML pages, blogs, news sites, documentation.

```go
func FetchPage(ctx context.Context, url string) (*Page, error) {
    req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
    req.Header.Set("User-Agent", "RealtimeIndexer/1.0 (+https://your-domain/bot)")
    resp, err := client.Do(req)
    // Hash content for dedup
    hash := xxhash.Sum64(body)
    return &Page{URL: url, Body: body, Hash: hash, FetchedAt: time.Now()}, nil
}
```

#### JS Renderer (Python Playwright — `playwright_crawler/crawler.py`)
Used for: React/Next.js/Vue/Angular SPAs, JS-rendered content. Playwright launches headless Chromium, waits for `networkidle`, then extracts the fully rendered DOM.

Heuristic classifier (`page_classifier.py`) decides which fetcher to use based on response headers, `<script>` tag density, and known SPA frameworks in the HTML.

#### robots.txt Cache (`internal/fetcher/robots.go`)
robots.txt is fetched once per domain and cached in Redis for 24 hours. Before crawling any URL, the worker checks if the path is allowed. Non-compliant crawls are logged and dropped.

#### Rate Limiter (`internal/ratelimit/token_bucket.go`)
Per-domain token bucket. Default: 1 request per second per domain. Respects `Crawl-delay` in robots.txt. Backed by Redis atomic counters so rate limits work across the worker pool.

#### Content Storage (`internal/storage/minio_client.go`)
Raw HTML stored as `s3://raw-pages/{domain}/{url-hash}.html.gz`. Content hash prevents re-processing of unchanged pages — if a page hasn't changed since last crawl, it's skipped. MinIO fires an event notification to trigger the processing pipeline.

---

### Phase 3: Processing Pipeline

**File:** `services/processor/`

Celery workers consume MinIO event notifications and run each document through the pipeline sequentially. Workers are horizontally scalable — add more workers to increase throughput.

#### HTML Cleaner (`pipeline/cleaner.py`)
Trafilatura is the best open-source content extractor available. It outperforms `newspaper3k`, `readability-lxml`, and `justext` on most benchmarks.

```python
import trafilatura

def clean_html(raw_html: str, url: str) -> dict:
    result = trafilatura.extract(
        raw_html,
        url=url,
        include_comments=False,
        include_tables=True,
        output_format='json',
        with_metadata=True,
    )
    return json.loads(result) if result else None
```

Output: clean article text, title, author, publish date, main image URL.

#### NLP Enrichment (`pipeline/nlp.py`)
spaCy pipeline runs: tokenisation → sentence segmentation → NER → POS tagging → noun chunks.

```python
# NLP pipeline
nlp = spacy.load("en_core_web_sm")

def enrich(text: str) -> dict:
    doc = nlp(text)
    return {
        "entities": [(e.text, e.label_) for e in doc.ents],
        "keywords": extract_keywords(doc),        # TF-IDF on noun chunks
        "sentences": [s.text for s in doc.sents],
        "word_count": len([t for t in doc if not t.is_stop]),
    }
```

#### Semantic Embeddings (`pipeline/embedder.py`)
We use `all-MiniLM-L6-v2` from sentence-transformers: 384-dimensional embeddings, runs in ~50ms on CPU per document. We embed the title + first 512 tokens of the cleaned text.

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')

def embed(title: str, text: str) -> list[float]:
    combined = f"{title}. {text[:2000]}"
    return model.encode(combined, normalize_embeddings=True).tolist()
```

#### Metadata Parser (`pipeline/metadata.py`)
extruct parses all structured data formats in parallel:

```python
import extruct

def parse_metadata(html: str, url: str) -> dict:
    data = extruct.extract(html, base_url=url, syntaxes=['json-ld','opengraph','microdata'])
    return {
        "schema_org": data.get('json-ld', []),
        "og_title": data.get('opengraph', [{}])[0].get('og:title'),
        "og_description": data.get('opengraph', [{}])[0].get('og:description'),
        "og_image": data.get('opengraph', [{}])[0].get('og:image'),
    }
```

#### Final Document Object
After all pipeline stages, a fully enriched document is assembled:

```python
@dataclass
class IndexedDocument:
    url: str
    domain: str
    title: str
    clean_text: str
    summary: str                    # First 300 chars of clean_text
    language: str                   # ISO 639-1 code
    entities: list[tuple[str,str]]  # (entity, type) pairs
    keywords: list[str]
    embedding: list[float]          # 384d vector
    schema_org: list[dict]
    og_metadata: dict
    word_count: int
    crawled_at: datetime
    indexed_at: datetime
    content_hash: str
    domain_authority: float
    freshness_score: float
```

---

### Phase 4: Index Writers

**File:** `services/indexer/`

Three parallel writes happen once the document object is ready. All three use `asyncio.gather()` so they happen concurrently, not sequentially.

#### Meilisearch Writer (`writers/meilisearch_writer.py`)
```python
async def write_to_meilisearch(doc: IndexedDocument):
    index = client.index('pages')
    await index.add_documents([{
        'id': hash_url(doc.url),
        'url': doc.url,
        'title': doc.title,
        'text': doc.clean_text,
        'domain': doc.domain,
        'language': doc.language,
        'keywords': doc.keywords,
        'crawled_at': doc.crawled_at.isoformat(),
    }])
    # Meilisearch auto-commits within 1 second (NRT)
```

Meilisearch settings: searchable fields = `['title', 'text', 'keywords']`, filterable = `['domain', 'language', 'crawled_at']`, sortable = `['crawled_at', 'freshness_score']`.

#### Qdrant Writer (`writers/qdrant_writer.py`)
```python
async def write_to_qdrant(doc: IndexedDocument):
    await client.upsert(
        collection_name="pages",
        points=[PointStruct(
            id=hash_url_to_int(doc.url),
            vector=doc.embedding,
            payload={
                "url": doc.url,
                "title": doc.title,
                "domain": doc.domain,
                "crawled_at": doc.crawled_at.isoformat(),
            }
        )]
    )
```

#### PostgreSQL Writer (`writers/postgres_writer.py`)
Stores complete crawl history, domain metadata, and ranking signals. Used by the admin dashboard and for debugging.

```sql
-- Main pages table
INSERT INTO pages (url, domain, title, language, word_count, content_hash, crawled_at, indexed_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
ON CONFLICT (url) DO UPDATE SET
    content_hash = EXCLUDED.content_hash,
    indexed_at = NOW(),
    crawl_count = pages.crawl_count + 1;
```

---

### Phase 5: Query API

**File:** `services/api/`

#### Hybrid Retrieval (`retrieval/fusion.py`)
Two retrieval streams run in parallel, results merged with RRF:

```python
async def hybrid_search(query: str, limit: int = 10) -> list[SearchResult]:
    # Parallel retrieval
    bm25_results, ann_results = await asyncio.gather(
        bm25_search(query, limit=50),
        ann_search(query, limit=50),
    )
    # Reciprocal Rank Fusion
    return reciprocal_rank_fusion(bm25_results, ann_results, k=60)[:limit]

def reciprocal_rank_fusion(lists: list, k: int = 60) -> list:
    scores = {}
    for results in lists:
        for rank, doc in enumerate(results):
            scores[doc.url] = scores.get(doc.url, 0) + 1 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

#### Result Cache (`cache/result_cache.py`)
Short-TTL Redis cache. Cache key = SHA256(query + filters). TTL = 15 seconds. On every index commit, affected cache keys are invalidated via Redis pub/sub.

#### Search Endpoint
```
GET /search?q=machine+learning&lang=en&limit=10
POST /search  (for complex filters)

Response headers:
  X-Index-Freshness: 2024-01-15T10:23:45Z   (last index commit time)
  X-Query-Time-Ms: 23
  X-Results-From-Cache: false
```

#### WebSocket — Live Indexing Feed
```
WS /ws/indexing-feed

Emits events:
  {"type": "url_discovered", "url": "...", "timestamp": "..."}
  {"type": "crawl_started",  "url": "...", "timestamp": "..."}
  {"type": "indexed",        "url": "...", "timestamp": "...", "latency_ms": 54230}
```

This WebSocket powers the real-time demo dashboard.

---

## 6. Database & Storage Design

### PostgreSQL Schema

```sql
-- Domain registry
CREATE TABLE domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) UNIQUE NOT NULL,
    authority_score FLOAT DEFAULT 0.5,
    crawl_rate_limit INT DEFAULT 1,        -- requests per second
    robots_txt TEXT,
    robots_cached_at TIMESTAMP,
    first_seen_at TIMESTAMP DEFAULT NOW(),
    total_pages_indexed INT DEFAULT 0
);

-- Indexed pages
CREATE TABLE pages (
    id BIGSERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    domain VARCHAR(255) REFERENCES domains(domain),
    title TEXT,
    language CHAR(2),
    word_count INT,
    content_hash CHAR(16),                 -- xxHash
    crawl_count INT DEFAULT 1,
    crawled_at TIMESTAMP NOT NULL,
    indexed_at TIMESTAMP NOT NULL,
    discovery_source VARCHAR(50),          -- websub, sitemap, ct_log, link
    discovery_latency_ms INT,              -- time from discovery to index
    CONSTRAINT valid_latency CHECK (discovery_latency_ms > 0)
);

-- Crawl history (append-only log)
CREATE TABLE crawl_log (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL,
    crawl_started_at TIMESTAMP,
    crawl_ended_at TIMESTAMP,
    http_status INT,
    content_hash CHAR(16),
    fetch_ms INT,
    process_ms INT,
    index_ms INT,
    total_ms INT,
    error TEXT
);

-- Indexes for performance
CREATE INDEX idx_pages_domain ON pages(domain);
CREATE INDEX idx_pages_indexed_at ON pages(indexed_at DESC);
CREATE INDEX idx_crawl_log_url ON crawl_log(url);
CREATE INDEX idx_crawl_log_started ON crawl_log(crawl_started_at DESC);
```

### Redis Key Schema

```
# URL dedup Bloom filter
BF:url_dedup                          → Bloom filter (100M capacity)

# Priority crawl queue
QUEUE:urls                            → Sorted Set (score = priority)

# robots.txt cache
ROBOTS:{domain}                       → String (robots.txt content, TTL=86400)

# Rate limiting
RATELIMIT:{domain}:{minute}           → Counter (TTL=60)

# Ranking signals (feature store)
SIGNALS:{url_hash}                    → Hash { freshness, authority, ... }

# Result cache
CACHE:search:{query_hash}             → String (JSON results, TTL=15)

# Index commit pub/sub
CHANNEL:index_commits                 → Pub/Sub channel
```

### Meilisearch Index Settings

```json
{
  "searchableAttributes": ["title", "text", "keywords", "domain"],
  "filterableAttributes": ["domain", "language", "crawled_at"],
  "sortableAttributes": ["crawled_at", "freshness_score", "word_count"],
  "rankingRules": [
    "words", "typo", "proximity", "attribute", "sort", "exactness"
  ],
  "highlightPreTag": "<mark>",
  "highlightPostTag": "</mark>",
  "pagination": { "maxTotalHits": 1000 }
}
```

### Qdrant Collection Config

```json
{
  "name": "pages",
  "vectors": {
    "size": 384,
    "distance": "Cosine"
  },
  "hnsw_config": {
    "m": 16,
    "ef_construct": 100,
    "full_scan_threshold": 10000
  },
  "optimizers_config": {
    "default_segment_number": 2,
    "indexing_threshold": 20000
  }
}
```

---

## 7. API Design

### Base URL
```
http://localhost:8000/api/v1
```

### Endpoints

#### `GET /search`
Hybrid BM25 + semantic search.

**Query params:**
| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | required | Search query |
| `lang` | string | null | ISO 639-1 filter |
| `domain` | string | null | Domain filter |
| `since` | ISO datetime | null | Filter to pages indexed after this time |
| `limit` | int | 10 | Results per page (max 50) |
| `offset` | int | 0 | Pagination offset |
| `mode` | enum | `hybrid` | `bm25`, `semantic`, or `hybrid` |

**Response:**
```json
{
  "query": "machine learning transformers",
  "total": 1423,
  "took_ms": 23,
  "index_freshness": "2024-01-15T10:23:45Z",
  "results": [
    {
      "url": "https://example.com/ml-guide",
      "title": "A Beginner's Guide to Transformers",
      "summary": "Transformers are a type of neural network architecture...",
      "domain": "example.com",
      "language": "en",
      "indexed_at": "2024-01-15T10:23:12Z",
      "score": 0.94,
      "highlights": {
        "title": "A Beginner's Guide to <mark>Transformers</mark>",
        "text": "...understanding <mark>machine learning</mark> requires..."
      }
    }
  ]
}
```

#### `POST /urls/submit`
Manually submit a URL for immediate indexing.

```json
// Request
{ "url": "https://example.com/new-page", "priority": "high" }

// Response
{ "task_id": "abc123", "queued_at": "...", "estimated_index_time": "55s" }
```

#### `GET /urls/{task_id}/status`
Track indexing progress of a submitted URL.

```json
{
  "task_id": "abc123",
  "url": "https://example.com/new-page",
  "status": "indexing",       // queued | crawling | processing | indexing | done | failed
  "progress": {
    "discovered_at": "...",
    "crawl_started_at": "...",
    "crawl_ended_at": "...",
    "process_started_at": "...",
    "indexed_at": null,
    "elapsed_ms": 32100
  }
}
```

#### `GET /stats`
Pipeline health and performance stats.

```json
{
  "queue_depth": 1423,
  "urls_indexed_last_60s": 847,
  "avg_latency_ms": 43200,
  "p95_latency_ms": 57100,
  "active_crawlers": 48,
  "index_size": 2847291,
  "uptime_seconds": 86423
}
```

#### `WebSocket /ws/feed`
Real-time event stream for the demo dashboard.

---

## 8. Deployment Architecture

### Docker Compose Stack (3-Day MVP)

```yaml
# docker-compose.yml — simplified view
services:

  redis:          # URL queue, Bloom filter, cache, signals
  minio:          # Raw HTML object storage
  postgres:       # Metadata and crawl history
  meilisearch:    # Full-text inverted index
  qdrant:         # Vector index

  discovery:      # 1 instance — URL discovery (Python)
  crawler:        # 3 instances — web crawler (Go)
  processor:      # 2 instances — NLP pipeline (Python)
  indexer:        # 1 instance — index writer (Python)
  api:            # 1 instance — query API (FastAPI)

  prometheus:     # Metrics
  grafana:        # Dashboards
```

Single command to start everything:
```bash
docker compose up --build
```

### Resource Requirements (Local Dev)

| Service | RAM | CPU |
|---|---|---|
| Redis | 512MB | 0.5 core |
| Meilisearch | 1GB | 1 core |
| Qdrant | 1GB | 1 core |
| PostgreSQL | 512MB | 0.5 core |
| MinIO | 256MB | 0.25 core |
| Discovery | 256MB | 0.5 core |
| Crawler (×3) | 512MB each | 1 core each |
| Processor (×2) | 2GB each | 2 cores each |
| API | 512MB | 0.5 core |
| **Total** | **~10GB** | **~10 cores** |

Minimum recommended machine: 16GB RAM, 8-core CPU.

### Production Architecture (Post-Demo)

In production, each service becomes a Kubernetes Deployment with HPA (Horizontal Pod Autoscaler) scaling on queue depth:

- Crawlers: scale 10–10,000 pods based on queue depth
- Processors: scale 2–100 pods based on processing queue depth
- Elasticsearch/Qdrant: dedicated nodes with SSD storage
- Redis: Redis Cluster with 3 primary + 3 replica nodes

---

## 9. Monitoring & Observability

### Key Metrics (Prometheus)

```python
# Defined in shared/metrics.py

# Discovery
urls_discovered_total = Counter('urls_discovered_total', 'Total URLs discovered', ['source'])
queue_depth = Gauge('queue_depth', 'Current URL queue depth')

# Crawling
crawl_duration_seconds = Histogram('crawl_duration_seconds', 'HTTP fetch time', buckets=[0.1,0.5,1,2,5,10,30])
crawl_errors_total = Counter('crawl_errors_total', 'Crawl errors', ['error_type'])

# Processing
process_duration_seconds = Histogram('process_duration_seconds', 'NLP processing time')
embedding_duration_seconds = Histogram('embedding_duration_seconds', 'Embedding generation time')

# Indexing
index_commits_total = Counter('index_commits_total', 'Total documents indexed')
index_latency_seconds = Histogram('index_latency_seconds', 'Discovery-to-index latency',
    buckets=[10,20,30,40,50,60,90,120])

# API
search_requests_total = Counter('search_requests_total', 'Search API requests', ['mode'])
search_latency_seconds = Histogram('search_latency_seconds', 'Search query latency')
```

### Grafana Dashboard Panels

1. **60s SLO panel** — gauge showing % of URLs indexed within 60s (target: >95%)
2. **End-to-end latency histogram** — P50/P95/P99 per pipeline stage
3. **Throughput** — URLs discovered/crawled/indexed per minute (time series)
4. **Queue depth** — real-time queue backlog (should stay near 0)
5. **Error rate** — crawl failures, processing errors, index failures
6. **Active workers** — crawler and processor worker count

### Alerting Rules

```yaml
# If P95 latency exceeds 60s SLO
- alert: IndexLatencyBreached
  expr: histogram_quantile(0.95, index_latency_seconds_bucket) > 60
  severity: critical

# If queue depth grows (crawlers falling behind)
- alert: QueueDepthHigh
  expr: queue_depth > 10000
  severity: warning

# If processing pipeline stalls
- alert: ProcessorStalled
  expr: rate(index_commits_total[5m]) == 0
  severity: critical
```

---

## 10. 3-Day Sprint Plan

### Constraint: 3 days to working demo

We build depth-first: one fully working pipeline before adding features. Every day ends with a runnable system.

---

### Day 1 — Foundation & Discovery → Crawl pipeline

**Goal:** URL goes in, raw HTML comes out. All infrastructure running.

#### Morning (4h)
- [ ] Repo initialised, directory structure created
- [ ] `docker-compose.yml` written — all infrastructure services
- [ ] `docker compose up` — Redis, MinIO, PostgreSQL, Meilisearch, Qdrant all healthy
- [ ] PostgreSQL schema created (`infra/postgres/init.sql`)
- [ ] `.env` configured, `shared/config.py` reads all values

#### Afternoon (4h)
- [ ] Discovery service skeleton — sitemap poller working
- [ ] Redis Bloom filter dedup (`queue/bloom_filter.py`)
- [ ] Redis priority queue push/pop working
- [ ] Go HTTP fetcher — fetches a URL, uploads raw HTML to MinIO
- [ ] robots.txt cache in Redis

#### Evening (2h)
- [ ] End-to-end test: submit a URL → see it in MinIO ✓
- [ ] Basic logging with timestamps across all services
- [ ] `scripts/seed_urls.py` — loads 100 test URLs into queue

**Day 1 deliverable:** `curl -X POST localhost:8000/urls/submit -d '{"url":"..."}'` → URL crawled → HTML in MinIO

---

### Day 2 — Processing pipeline → Index writers → Search API

**Goal:** Full pipeline working. Submit URL → searchable within 60s.

#### Morning (4h)
- [ ] Celery worker setup for processor service
- [ ] MinIO event notification → Celery task trigger
- [ ] `pipeline/cleaner.py` — Trafilatura integration
- [ ] `pipeline/nlp.py` — spaCy pipeline
- [ ] `pipeline/embedder.py` — sentence-transformers

#### Afternoon (4h)
- [ ] `pipeline/metadata.py` — extruct integration
- [ ] Full `IndexedDocument` assembly
- [ ] `writers/meilisearch_writer.py` — index write tested
- [ ] `writers/qdrant_writer.py` — vector upsert tested
- [ ] `writers/postgres_writer.py` — metadata write tested

#### Evening (2h)
- [ ] FastAPI search endpoint (`/search`) — basic BM25 only
- [ ] Test: submit URL → wait → search for it → it appears ✓
- [ ] Measure end-to-end latency first time — record baseline

**Day 2 deliverable:** Full pipeline working. `GET /search?q=test` returns indexed pages.

---

### Day 3 — Hybrid retrieval + Monitoring + Demo polish

**Goal:** 60s SLO proven, demo ready for HoD.

#### Morning (3h)
- [ ] `retrieval/ann.py` — Qdrant ANN search
- [ ] `retrieval/fusion.py` — Reciprocal Rank Fusion
- [ ] Result cache (15s Redis TTL)
- [ ] WebSocket `/ws/feed` — real-time indexing events

#### Afternoon (3h)
- [ ] Prometheus metrics wired into all services
- [ ] Grafana dashboard imported and tested
- [ ] 60s SLO panel showing live latency
- [ ] `scripts/benchmark.py` — runs 100 URLs, measures P95 latency
- [ ] Tune Celery worker count and crawler pool size to hit <60s

#### Evening (2h — Demo prep)
- [ ] `scripts/demo.py` — scripted live demo
- [ ] Demo dashboard (web UI showing real-time pipeline events)
- [ ] Run full demo 3× — confirm consistent <60s indexing
- [ ] Prepare 2-minute live explanation for HoD

**Day 3 deliverable:** Live demo. Submit URL → watch it index in real time → search for it → under 60s, every time.

---

## 11. Environment Setup

### Prerequisites
```bash
# Required
Docker Desktop >= 4.28 (or Docker Engine + Compose Plugin)
Go 1.22+
Python 3.11+
Git

# Recommended
Make
```

### One-Time Setup
```bash
# 1. Clone repo
git clone https://github.com/your-username/realtime-indexer
cd realtime-indexer

# 2. Copy env template
cp .env.example .env

# 3. Run setup script (installs Python deps, Go modules, downloads NLP models)
chmod +x scripts/setup.sh
./scripts/setup.sh

# 4. Start all infrastructure
docker compose up -d redis minio postgres meilisearch qdrant

# 5. Wait for all services to be healthy (~30s)
docker compose ps

# 6. Start application services
docker compose up discovery crawler processor indexer api

# 7. Open dashboards
# API docs:    http://localhost:8000/docs
# Grafana:     http://localhost:3000  (admin/admin)
# MinIO:       http://localhost:9001  (minioadmin/minioadmin)
```

### .env Reference
```env
# Redis
REDIS_URL=redis://localhost:6379/0
REDIS_BLOOM_CAPACITY=100000000
REDIS_BLOOM_ERROR_RATE=0.01

# MinIO
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET=raw-pages

# PostgreSQL
DATABASE_URL=postgresql://indexer:password@localhost:5432/indexer

# Meilisearch
MEILISEARCH_URL=http://localhost:7700
MEILISEARCH_API_KEY=masterKey

# Qdrant
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=pages

# Crawler
CRAWLER_WORKERS=10
CRAWLER_TIMEOUT_SECONDS=30
CRAWLER_USER_AGENT=RealtimeIndexer/1.0

# Processor
PROCESSOR_WORKERS=4
EMBEDDING_MODEL=all-MiniLM-L6-v2
SPACY_MODEL=en_core_web_sm

# API
API_HOST=0.0.0.0
API_PORT=8000
API_RESULT_CACHE_TTL=15
```

### Makefile Commands
```makefile
make start          # docker compose up (all services)
make stop           # docker compose down
make logs           # tail all service logs
make test           # run all test suites
make benchmark      # run latency benchmark (100 URLs)
make demo           # run the live demo script
make clean          # remove all containers and volumes
make seed           # load 1000 test URLs into queue
```

---

## 12. Configuration Reference

### Tuning for 60s SLO

The following settings have the most impact on end-to-end latency:

| Setting | Default | Faster (lower latency) | Trade-off |
|---|---|---|---|
| `CRAWLER_WORKERS` | 10 | 50 | More CPU/memory, may trigger rate limits |
| `PROCESSOR_WORKERS` | 4 | 8 | GPU or more CPU required |
| `EMBEDDING_MODEL` | MiniLM-L6 | MiniLM-L6 (already fastest) | — |
| Meilisearch `embedders.source` | explicit | explicit | — |
| Qdrant `optimizers.indexing_threshold` | 20000 | 1000 | More disk I/O during heavy writes |
| Redis queue poll interval | 100ms | 10ms | More Redis ops/sec |

### Crawl Politeness Settings
```env
# Per-domain rate limit (requests per second)
DEFAULT_CRAWL_RATE=1
# Honour robots.txt Crawl-delay: true/false
RESPECT_CRAWL_DELAY=true
# Max redirects to follow
MAX_REDIRECTS=5
# Request timeout
FETCH_TIMEOUT_SECONDS=30
```

---

## 13. Testing Strategy

### Unit Tests
Each service has its own `tests/` directory. Run with:
```bash
pytest services/processor/tests/ -v
go test ./services/crawler/... -v
```

Key unit tests:
- Bloom filter: no false negatives, <1% false positives at scale
- robots.txt parser: handles all edge cases (wildcards, `Allow:`, `Crawl-delay:`)
- HTML cleaner: strips boilerplate, preserves article content
- Embedding: output is 384d, L2-normalised

### Integration Tests
```bash
# Test full pipeline with a known URL
python scripts/integration_test.py --url https://example.com

# Expected output:
# [00.0s] URL submitted
# [01.2s] URL enqueued (after dedup)
# [04.8s] Crawl started
# [07.3s] Raw HTML stored (12,847 bytes)
# [15.2s] Processing started
# [22.1s] NLP complete (143 entities, 28 keywords)
# [26.8s] Embedding generated (384d)
# [28.3s] Written to Meilisearch ✓
# [28.9s] Written to Qdrant ✓
# [29.1s] Written to PostgreSQL ✓
# [29.1s] INDEXED ✓ — total latency: 29.1s
```

### Latency Benchmark
```bash
python scripts/benchmark.py --count 100 --concurrent 10

# Output:
# URLs submitted: 100
# Indexed within 30s: 41 (41%)
# Indexed within 60s: 97 (97%)   ← must be ≥ 95%
# Indexed within 90s: 100 (100%)
# P50 latency: 28.4s
# P95 latency: 54.2s             ← must be < 60s
# P99 latency: 61.1s
# Mean latency: 31.7s
```

---

## 14. Demo Script

### Preparation (10 minutes before)
```bash
make clean          # fresh start
make start          # boot everything
make seed           # pre-warm with 1000 URLs so indexes aren't cold
```

### Live Demo (2 minutes)

**Step 1 — Show the empty search**
```bash
curl "http://localhost:8000/api/v1/search?q=realtime+indexing+demo"
# Returns: {"total": 0, "results": []}
```

**Step 2 — Submit the URL**
```bash
curl -X POST http://localhost:8000/api/v1/urls/submit \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Web_indexing", "priority": "high"}'
# Returns: {"task_id": "abc123", "estimated_index_time": "45s"}
```

**Step 3 — Open the live dashboard**
Open `http://localhost:8000/demo` in browser — shows real-time pipeline events streaming in as the URL moves through each stage.

**Step 4 — Watch it index**
The dashboard shows: discovered → crawling → processing → embedding → indexed

**Step 5 — Search for it**
```bash
# Run this at T+45s
curl "http://localhost:8000/api/v1/search?q=web+indexing"
# Returns the Wikipedia article ✓
# X-Index-Freshness header shows it was indexed seconds ago
```

**Step 6 — Show the Grafana dashboard**
Open `http://localhost:3000` — show the 60s SLO panel at 97%+ success rate.

---

## Summary — What We Are Building

| What | A real-time web indexing system |
|---|---|
| Who | Built by PRN-22510103 |
| Target | New pages searchable in < 60 seconds |
| Timeline | 3 days to working demo |
| Stack | Python + Go + Redis + Meilisearch + Qdrant + FastAPI |
| MVP scope | Full pipeline, single-node, Docker Compose |
| Demo | Live URL submission → search results in <60s |
| Proof | Grafana dashboard showing 95%+ of URLs indexed within SLO |

**Let's build.**

---

*Document version: 1.0 | PRN: 22510103 | Last updated: Day 0*