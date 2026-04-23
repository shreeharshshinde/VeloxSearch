# Phase 2 — Crawler Layer
## PRN: 22510103 | Real-Time Web Indexing System

> **Goal of Phase 2:** Fetch raw HTML from every URL in the crawl queue,
> store it in MinIO, and notify the Processor — all within a **10–25 second**
> window from the original URL discovery.

---

## Table of Contents

1. [What Phase 2 Does](#1-what-phase-2-does)
2. [Architecture of the Crawler Layer](#2-architecture-of-the-crawler-layer)
3. [Component Deep-Dive](#3-component-deep-dive)
   - 3.1 [Go HTTP Fetcher — Static Pages](#31-go-http-fetcher--static-pages)
   - 3.2 [Playwright Crawler — JS-Rendered Pages](#32-playwright-crawler--js-rendered-pages)
   - 3.3 [Page Classifier](#33-page-classifier)
   - 3.4 [robots.txt Cache](#34-robotstxt-cache)
   - 3.5 [Per-Domain Rate Limiter](#35-per-domain-rate-limiter)
   - 3.6 [MinIO Content Store](#36-minio-content-store)
4. [Data Models](#4-data-models)
5. [Configuration Reference](#5-configuration-reference)
6. [File Reference](#6-file-reference)
7. [How to Run Phase 2](#7-how-to-run-phase-2)
8. [How to Test Phase 2](#8-how-to-test-phase-2)
9. [Metrics & Observability](#9-metrics--observability)
10. [Common Issues & Fixes](#10-common-issues--fixes)
11. [What Phase 3 Receives](#11-what-phase-3-receives)

---

## 1. What Phase 2 Does

Phase 2 takes a URL from the Redis priority queue (Phase 1's output) and:

1. Checks `robots.txt` — is crawling this URL allowed?
2. Applies per-domain rate limiting — are we being polite?
3. Fetches the page — using Go (fast, static HTML) or Playwright (JS-rendered SPAs)
4. Content-hashes the body — skip if unchanged since last crawl
5. Stores raw HTML gzip-compressed in MinIO — durable, replayable handoff
6. Publishes a `CrawlEvent` to a Redis Stream — triggers Phase 3 (Processor)

**Input:**  Redis Sorted Set `QUEUE:urls` populated by Phase 1 (Discovery)

**Output:**
- Raw HTML stored at `s3://raw-pages/{domain}/{hash}.html.gz` in MinIO
- `CrawlEvent` JSON published to Redis Stream `STREAM:crawl_complete`
- Crawl log entry written to PostgreSQL `crawl_log` table

**Time budget:** 10 to 25 seconds (queue pop → MinIO stored)

### Why two fetchers?

Not all pages are created equal:

| Page type | Example | Fetcher | Why |
|---|---|---|---|
| Static HTML | Wikipedia, BBC News, docs | **Go HTTP** | < 1s, zero CPU overhead |
| JS-rendered SPA | Next.js, React apps | **Playwright** | Needs headless browser |

The **page classifier** inspects the initial response and routes to the correct fetcher.
Static pages (~80% of the web) are handled entirely in Go — extremely fast.
SPAs get Playwright — slower but accurate.

---

## 2. Architecture of the Crawler Layer

```
                         Redis QUEUE:urls
                              │
                     BZPOPMAX (priority order)
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ Worker 1 │   │ Worker 2 │   │ Worker N │   (Go goroutines)
        └────┬─────┘   └────┬─────┘   └────┬─────┘
             │              │               │
             └──────────────┼───────────────┘
                            │  (shared components)
             ┌──────────────┼─────────────────────────────┐
             │              │                              │
             ▼              ▼                              ▼
      ┌─────────────┐ ┌───────────────┐         ┌─────────────────┐
      │  robots.txt │ │  Rate Limiter │         │  Page Classifier│
      │   Cache     │ │ (token bucket)│         │ (Go first, then │
      │  (Redis)    │ │  (Redis)      │         │  Playwright?)   │
      └─────────────┘ └───────────────┘         └────────┬────────┘
                                                          │
                                    ┌─────────────────────┤
                                    │                     │
                              needs_playwright?          static
                                    │                     │
                                    ▼                     ▼
                           ┌──────────────┐     ┌─────────────────┐
                           │  Playwright  │     │   Go HTTPFetcher│
                           │  (Python)    │     │   (goroutine)   │
                           │  Chromium    │     │   httpx         │
                           └──────┬───────┘     └───────┬─────────┘
                                  │                      │
                                  └──────────┬───────────┘
                                             │
                                             ▼
                                    ┌─────────────────┐
                                    │   Storage Client │
                                    │   (Go MinIO SDK) │
                                    │                  │
                                    │  1. Hash content │
                                    │  2. Check Redis  │
                                    │     dedup cache  │
                                    │  3. gzip + store │
                                    │  4. Cache hash   │
                                    └────────┬─────────┘
                                             │
                               ┌─────────────┴──────────────┐
                               ▼                             ▼
                    ┌──────────────────┐          ┌──────────────────┐
                    │ MinIO raw-pages  │          │ Redis Stream     │
                    │ bucket           │          │ STREAM:crawl_    │
                    │ {domain}/        │          │ complete         │
                    │ {hash}.html.gz   │          │ → Phase 3        │
                    └──────────────────┘          └──────────────────┘
```

---

## 3. Component Deep-Dive

### 3.1 Go HTTP Fetcher — Static Pages

**File:** `services/crawler/internal/fetcher/http_fetcher.go`

The Go fetcher is used for the vast majority of pages. It is a standard
async HTTP/1.1 + HTTP/2 client that:

- Sends `User-Agent: RealtimeIndexer/1.0` (identifies our bot)
- Sets `Accept-Encoding: gzip, deflate, br` (automatic decompression)
- Follows redirects up to `MAX_REDIRECTS` (default 5)
- Times out after `CRAWLER_TIMEOUT_SECONDS` (default 30s)
- Reads up to `MaxBodySize` = 10MB (pages larger are skipped)
- Only reads `text/html` content-types (skips images, PDFs, JSON APIs)

**Worker pool:**
```
CRAWLER_WORKERS goroutines (default 10)
     │
     ├── Each goroutine loops: pop → fetch → store → publish
     └── All goroutines share: robots cache, rate limiter, storage client
```

Go goroutines are extremely lightweight (~8KB stack). 10,000 concurrent
goroutines use ~80MB RAM, vs ~10GB for OS threads.

**Connection pool:**
The shared `http.Client` maintains a persistent connection pool per host.
This means the 2nd request to `example.com` reuses the TLS handshake and
TCP connection from the 1st — much faster.

---

### 3.2 Playwright Crawler — JS-Rendered Pages

**File:** `services/crawler/playwright_crawler/crawler.py`

Playwright launches headless Chromium and renders the full page, including
executing JavaScript. This gives us the same HTML a real user sees.

**Key settings:**
```python
wait_until="networkidle"   # wait for no network activity for 500ms
                           # → JavaScript has finished loading content

# Resource blocking (speeds up rendering 30–50%):
BLOCKED: image, media, font, stylesheet
         google-analytics, doubleclick, facebook tracking
```

**Context isolation:**
Each page fetch uses an isolated browser context — cookies, localStorage,
and sessions don't bleed between fetches. This prevents site A's login
state from affecting site B.

**Fallback:**
If `networkidle` times out, we retry with `domcontentloaded` (faster but
misses async content). Better to have partial content than nothing.

**When is Playwright used?**
Only when the page classifier says `needs_playwright=True`. Typically:
- ~20% of discovered pages need Playwright
- These are React, Next.js, Vue, Angular, Gatsby sites

---

### 3.3 Page Classifier

**File:** `services/crawler/playwright_crawler/page_classifier.py`

Inspects the initial response to decide whether Playwright is needed.
Classification is based on signals with decreasing reliability:

```
Priority  Signal                          Confidence
────────────────────────────────────────────────────
HIGH      JSON/binary content-type        1.00  → never use Playwright
HIGH      X-Powered-By: Next.js header    0.95  → always use Playwright
HIGH      Known SPA domains               1.00  → always use Playwright
MED       id="__NEXT_DATA__" in HTML      0.90  → Next.js, use Playwright
MED       <div id="root"></div>           0.90  → React, use Playwright
MED       <div id="app"></div>            0.90  → Vue/Nuxt, use Playwright
MED       ng-version= attribute           0.90  → Angular, use Playwright
MED       gatsby-focus-wrapper            0.90  → Gatsby, use Playwright
LOW       Empty shell heuristic           0.75  → suspect SPA, use Playwright
DEFAULT   No signals detected             0.85  → use Go fetcher
```

**Empty shell heuristic:**
```python
html_length = len(html)    # total HTML bytes
text_length = len(re.sub(r'<[^>]+>', '', html).strip())  # visible text chars

if html_length > 2000 and text_length < 200:
    return needs_playwright=True  # big HTML file, no visible content
```

---

### 3.4 robots.txt Cache

**File:** `services/crawler/internal/fetcher/robots.go`

Before fetching any URL, we check `robots.txt` for the domain.

**Cache strategy:**
```
First request to example.com:
  1. Fetch https://example.com/robots.txt
  2. Parse: extract Disallow/Allow/Crawl-delay rules
  3. Cache in Redis: ROBOTS:example.com → content (TTL = 24h)
  4. Cache crawl delay: CRAWLDELAY:example.com → 2.0 (TTL = 24h)

Subsequent requests to example.com:
  1. Redis GET ROBOTS:example.com → cache hit
  2. Parse rules from cached content → O(1) lookup
  3. No HTTP request needed
```

**Rules we honour:**
```
User-agent: *                → applies to our bot (no specific rule)
User-agent: RealtimeIndexer  → applies specifically to us (takes precedence)
Disallow: /private/          → block paths starting with /private/
Allow: /private/public/      → allow this specific sub-path (overrides Disallow)
Crawl-delay: 2               → wait 2 seconds between requests to this domain
```

**Conflict resolution:**
When both `Allow` and `Disallow` match a path, the **more specific** (longer) rule wins:
```
Disallow: /products/         → blocks /products/anything
Allow: /products/public/     → allows /products/public/anything
                               (longer match wins → allowed)
```

---

### 3.5 Per-Domain Rate Limiter

**File:** `services/crawler/internal/ratelimit/token_bucket.go`

We enforce a maximum request rate per domain to avoid overloading sites.

**Why Redis-backed (not in-memory)?**
We run 3+ crawler replicas. If each had its own in-memory counter,
each could send at full rate — 3 replicas × 1 req/sec = 3 req/sec to one domain.
Redis is the shared counter, so total rate across all replicas = 1 req/sec.

**Algorithm: fixed window with millisecond resolution**
```
Redis key: RATELIMIT:{domain}
Value: timestamp (ms) of last request to this domain

On each request:
  last = GET RATELIMIT:{domain}
  elapsed = now_ms - last
  if elapsed >= gap_ms:
      SET RATELIMIT:{domain} now_ms
      allow request
  else:
      sleep(gap_ms - elapsed)
      retry
```

**Rate calculation:**
```
robots.txt Crawl-delay: 2  →  rate = 1/2 = 0.5 req/sec  →  gap = 2000ms
No Crawl-delay specified   →  rate = 1.0 req/sec          →  gap = 1000ms
CRAWLER_WORKERS=50 replicas → still only 1 req/sec total (Redis shared)
```

---

### 3.6 MinIO Content Store

**File:** `services/crawler/internal/storage/minio_client.go`

Raw HTML is stored content-addressed in MinIO (S3-compatible).

**Content-addressed storage:**
```
Key = {domain}/{fnv64a_hash(body)}.html.gz

Example:
  URL:  https://example.com/article/1
  Body: "<html>...</html>" (12,847 bytes)
  Hash: a3f9c2d8e1b74f56
  Key:  example.com/a3f9c2d8e1b74f56.html.gz
```

**Deduplication via Redis:**
```
On every store:
  1. Hash body → hash = "a3f9c2d8e1b74f56"
  2. GET CONTENTHASH:a3f9c2d8e1b74f56
     - If exists → page unchanged since last crawl → skip processing
     - If not → store in MinIO → SET CONTENTHASH:hash minio_key (TTL=7d)
```

This means if we re-crawl `example.com/article/1` and the content hasn't
changed, we store nothing and skip the NLP pipeline entirely — saving
significant CPU and time.

**Compression:**
All content is stored gzip-compressed.
- Typical compression ratio: 5–10× (HTML compresses very well)
- 12,847 bytes raw → ~1,800 bytes compressed
- Saves ~80% storage costs at scale

---

## 4. Data Models

### CrawledPage

The output of Phase 2, consumed by Phase 3 (Processor).

```python
@dataclass
class CrawledPage:
    # Identity
    url:              str         # final URL (after redirects)
    original_url:     str         # URL as queued
    domain:           str         # "example.com"

    # Result
    status:           CrawlStatus # SUCCESS | ROBOTS_BLOCK | HTTP_ERROR | TIMEOUT | DUPLICATE
    fetcher_type:     FetcherType # HTTP | PLAYWRIGHT

    # HTTP details
    http_status:      int         # 200, 301, 404, ...
    content_type:     str         # "text/html; charset=utf-8"
    content_hash:     str         # 16-char FNV-64a hex
    minio_key:        str         # "example.com/a3f9c2d8.html.gz"

    # Timing
    fetch_bytes:      int         # raw body size (bytes)
    fetch_ms:         int         # HTTP fetch duration (ms)
    crawled_at:       float       # unix timestamp
    discovered_at:    float       # from URLTask (for latency tracking)
    queue_wait_ms:    int         # time spent in queue

    # Provenance
    depth:            int         # link depth from seed
    discovery_source: str         # "websub" | "sitemap" | "ct_log" | "link"

    # Extras
    response_headers: dict
    redirect_chain:   list[str]
    error:            str         # error message if failed
```

### CrawlEvent (Redis Stream message)

The same data published to `STREAM:crawl_complete` as flat JSON fields:

```json
{
  "url":              "https://example.com/article/1",
  "original_url":     "https://example.com/article/1",
  "domain":           "example.com",
  "status":           "success",
  "fetcher_type":     "http",
  "http_status":      200,
  "content_type":     "text/html; charset=utf-8",
  "content_hash":     "a3f9c2d8e1b74f56",
  "minio_key":        "example.com/a3f9c2d8e1b74f56.html.gz",
  "fetch_bytes":      12847,
  "fetch_ms":         342,
  "crawled_at":       1704067215.3,
  "discovered_at":    1704067205.1,
  "queue_wait_ms":    8200,
  "depth":            0,
  "discovery_source": "websub",
  "error":            ""
}
```

### CrawlStatus enum

```
SUCCESS      → page fetched and stored in MinIO, processing will start
ROBOTS_BLOCK → blocked by robots.txt, skip completely
HTTP_ERROR   → 4xx/5xx response, will not be retried
TIMEOUT      → request timed out, may be retried later
REDIRECT_MAX → too many redirects, skip
DUPLICATE    → content hash matches previous crawl, skip processing
```

---

## 5. Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `CRAWLER_WORKERS` | `10` | Number of Go goroutine workers |
| `CRAWLER_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout |
| `CRAWLER_USER_AGENT` | `RealtimeIndexer/1.0` | Bot User-Agent string |
| `MAX_REDIRECTS` | `5` | Maximum redirects to follow |
| `DEFAULT_CRAWL_RATE` | `1.0` | Default requests/sec per domain |
| `RESPECT_CRAWL_DELAY` | `true` | Honour robots.txt Crawl-delay |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO server address |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `MINIO_BUCKET` | `raw-pages` | Bucket for raw HTML |
| `REDIS_ADDR` | `localhost:6379` | Redis address (Go format, no redis://) |

**Tuning for speed vs. politeness:**

| Goal | Setting | Value |
|---|---|---|
| Faster crawl | `CRAWLER_WORKERS` | 50 (more goroutines) |
| Faster crawl | `CRAWLER_TIMEOUT_SECONDS` | 15 (drop slow sites) |
| More polite | `DEFAULT_CRAWL_RATE` | 0.5 (1 req / 2s per domain) |
| More polite | `RESPECT_CRAWL_DELAY` | true (always) |

---

## 6. File Reference

```
services/crawler/
├── Dockerfile                          Multi-stage: Go binary + Python Playwright
├── go.mod                              Go module and dependencies
│
├── cmd/
│   └── main.go                         ★ Service entrypoint — worker pool, queue pop, event publish
│
├── internal/
│   ├── fetcher/
│   │   ├── http_fetcher.go             ★ Go HTTP/2 fetcher — static pages
│   │   └── robots.go                   ★ robots.txt parser + Redis cache
│   ├── ratelimit/
│   │   └── token_bucket.go             ★ Per-domain Redis rate limiter
│   └── storage/
│       └── minio_client.go             ★ Content-addressed MinIO store
│
├── playwright_crawler/
│   ├── crawler.py                      ★ Playwright headless Chromium fetcher
│   ├── page_classifier.py              ★ Decides HTTP vs Playwright
│   └── requirements.txt
│
└── tests/
    ├── fetcher_test.go                 Go unit tests (robots, hash, content-type)
    └── test_playwright.py              Python tests (classifier, CrawledPage)

shared/models/
└── crawled_page.py                     ★ CrawledPage dataclass — Phase 2 output

★ = files you must understand to work on Phase 2
```

---

## 7. How to Run Phase 2

### Prerequisites

Phase 1 must be running (or queue pre-seeded).

```bash
# Start infrastructure (Redis + MinIO)
docker compose up -d redis minio

# Verify both healthy
docker compose ps redis minio
```

### Start the crawler

```bash
# Docker (recommended — includes Go binary + Python Playwright)
docker compose up crawler

# Watch crawler logs
docker compose logs -f crawler
```

### Expected log output

```json
{"time":"2024-01-15T10:23:45Z","level":"INFO","msg":"redis connected","addr":"redis:6379"}
{"time":"2024-01-15T10:23:45Z","level":"INFO","msg":"minio connected","endpoint":"minio:9000"}
{"time":"2024-01-15T10:23:45Z","level":"INFO","msg":"starting crawler worker pool","workers":10}
{"time":"2024-01-15T10:23:45Z","level":"INFO","msg":"crawler service running","queue":"QUEUE:urls"}
{"time":"2024-01-15T10:23:48Z","level":"INFO","msg":"crawling","url":"https://example.com/article","queue_wait_ms":3100}
{"time":"2024-01-15T10:23:49Z","level":"INFO","msg":"crawled successfully","url":"https://example.com/article","bytes":12847,"fetch_ms":342,"minio_key":"example.com/a3f9c2d8.html.gz"}
```

### Verify pages are stored in MinIO

```bash
# Open MinIO console
open http://localhost:9001
# Login: minioadmin / minioadmin
# Navigate to: raw-pages bucket
# Should see folders per domain with .html.gz files

# Or via CLI:
docker exec indexer_minio mc ls local/raw-pages/example.com/
```

### Verify events in Redis Stream

```bash
# Check stream length
docker exec indexer_redis redis-cli XLEN STREAM:crawl_complete

# Read latest events
docker exec indexer_redis redis-cli XREVRANGE STREAM:crawl_complete + - COUNT 3
```

---

## 8. How to Test Phase 2

### Go tests (robots.txt, rate limiter, storage)

```bash
cd services/crawler
go test ./... -v

# Expected:
# --- PASS: TestParseRobots_AllowAll (0.00s)
# --- PASS: TestParseRobots_DisallowAll (0.00s)
# --- PASS: TestParseRobots_AllowOverridesDisallow (0.00s)
# --- PASS: TestParseRobots_CrawlDelay (0.00s)
# --- PASS: TestParseRobots_AgentSpecificRules (0.00s)
# --- PASS: TestIsHTMLContent (0.00s)
# --- PASS: TestContentHash_Consistent (0.00s)
# --- PASS: TestContentHash_DifferentContent (0.00s)
# PASS
```

### Python tests (page classifier, CrawledPage model)

```bash
pytest services/crawler/tests/test_playwright.py -v

# Expected:
# PASSED test_plain_html_page_no_playwright
# PASSED test_nextjs_data_attribute_needs_playwright
# PASSED test_react_root_empty_div_needs_playwright
# PASSED test_angular_ng_version_needs_playwright
# PASSED test_known_spa_domain_needs_playwright
# PASSED test_json_roundtrip
# PASSED test_is_success_property
```

---

## 9. Metrics & Observability

The crawler publishes these Prometheus metrics (port 9102):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `indexer_crawl_started_total` | Counter | — | Crawl jobs started |
| `indexer_crawl_completed_total` | Counter | `fetcher` (http/playwright) | Successful fetches |
| `indexer_crawl_errors_total` | Counter | `error_type` | Failed crawls |
| `indexer_crawl_duration_seconds` | Histogram | — | Per-fetch time |
| `indexer_crawl_bytes_total` | Counter | — | Total bytes downloaded |
| `indexer_robots_blocked_total` | Counter | — | robots.txt rejections |

### Key Grafana queries

```promql
# Crawl throughput (pages/min)
rate(indexer_crawl_completed_total[1m]) * 60

# Error rate (% of crawls failing)
rate(indexer_crawl_errors_total[5m]) /
  rate(indexer_crawl_started_total[5m]) * 100

# Go vs Playwright split
rate(indexer_crawl_completed_total{fetcher="http"}[5m]) /
  rate(indexer_crawl_completed_total[5m]) * 100

# P95 fetch latency
histogram_quantile(0.95, rate(indexer_crawl_duration_seconds_bucket[5m]))
```

---

## 10. Common Issues & Fixes

### "dial tcp: connection refused" (MinIO)

**Cause:** MinIO not started or not healthy.
```bash
docker compose up -d minio
docker compose ps minio   # should show: healthy
```

### "BucketNotFound: raw-pages"

**Cause:** MinIO started but bucket not created yet.
**Fix:** The `minio_init` service in docker-compose.yml creates it automatically.
If skipped:
```bash
docker exec indexer_minio mc alias set local http://minio:9000 minioadmin minioadmin
docker exec indexer_minio mc mb local/raw-pages
```

### "queue is empty — nothing to crawl"

**Cause:** Phase 1 (discovery) not running, or Bloom filter blocking all URLs.
```bash
# Seed the queue manually
python scripts/seed_urls.py --clear --count 20
# Then check queue depth
docker exec indexer_redis redis-cli ZCARD QUEUE:urls
```

### Playwright crashes or OOM

**Cause:** Headless Chromium uses significant RAM (~300MB per instance).
**Fix:** Limit Playwright worker concurrency:
```bash
# In .env:
PLAYWRIGHT_WORKERS=2
```

Or if `--disable-dev-shm-usage` isn't set, Chromium crashes in Docker:
```bash
# Already set in Dockerfile args, but verify:
docker compose logs crawler | grep "No usable sandbox"
# If seen → rebuild: docker compose build crawler
```

### "too many open files" error

**Cause:** 10,000 goroutines × file descriptors exceed OS limit.
```bash
# Increase OS limit
ulimit -n 65536
# Permanently: add to /etc/security/limits.conf
```

---

## 11. What Phase 3 Receives

When Phase 2 completes successfully, Phase 3 (Processor) gets:

**From Redis Stream `STREAM:crawl_complete`:**
```python
event = {
    "url":          "https://example.com/article/1",
    "domain":       "example.com",
    "status":       "success",            # only process if success
    "minio_key":    "example.com/a3f9c2d8e1b74f56.html.gz",
    "content_hash": "a3f9c2d8e1b74f56",
    "fetch_ms":     342,
    "discovered_at": 1704067205.1,
    "crawled_at":    1704067215.3,
    "depth":        0,
    "discovery_source": "websub",
}
```

**From MinIO (fetched by Processor using `minio_key`):**
```
Raw HTML, gzip-compressed
Decompressed: full HTML of the page at time of crawl
```

**Phase 2 guarantees:**
- `status == "success"` means the HTML is in MinIO, readable, and non-empty
- `content_hash` uniquely identifies this version of the content
- `minio_key` is the exact path to retrieve the HTML from MinIO
- `discovered_at` is preserved so Phase 3 can track end-to-end latency
- `status == "duplicate"` means content hasn't changed — Processor should skip

---

*Phase 2 documentation | PRN: 22510103 | Version 1.0*