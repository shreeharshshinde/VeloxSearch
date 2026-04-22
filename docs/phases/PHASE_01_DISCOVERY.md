# Phase 1 — Discovery Layer
## PRN: 22510103 | Real-Time Web Indexing System

> **Goal of Phase 1:** Detect new URLs the instant they appear on the web and push
> them into the crawl queue within **0–10 seconds** of discovery.

---

## Table of Contents

1. [What Phase 1 Does](#1-what-phase-1-does)
2. [Architecture of the Discovery Layer](#2-architecture-of-the-discovery-layer)
3. [Component Deep-Dive](#3-component-deep-dive)
   - 3.1 [Bloom Filter — URL Deduplication](#31-bloom-filter--url-deduplication)
   - 3.2 [Priority Queue — Redis Sorted Set](#32-priority-queue--redis-sorted-set)
   - 3.3 [Sitemap Watcher](#33-sitemap-watcher)
   - 3.4 [WebSub Subscriber](#34-websub-subscriber)
   - 3.5 [CT Log Scanner](#35-ct-log-scanner)
   - 3.6 [Link Extractor](#36-link-extractor)
4. [Data Models](#4-data-models)
5. [Configuration Reference](#5-configuration-reference)
6. [File Reference](#6-file-reference)
7. [How to Run Phase 1](#7-how-to-run-phase-1)
8. [How to Test Phase 1](#8-how-to-test-phase-1)
9. [Metrics & Observability](#9-metrics--observability)
10. [Common Issues & Fixes](#10-common-issues--fixes)
11. [What Phase 2 Receives](#11-what-phase-2-receives)

---

## 1. What Phase 1 Does

When a new web page is published anywhere on the internet, Phase 1 is
responsible for detecting it and placing it in the crawl queue.

**Input:**  The internet — new pages published by any site, any time.

**Output:** A populated Redis Sorted Set (`QUEUE:urls`) where each item is a
`URLTask` JSON object ready to be fetched by the crawler (Phase 2).

**Time budget:** 0 to 10 seconds from publish to queued.

### The Discovery Problem

Traditional crawlers revisit pages on a fixed schedule (Google re-crawls most
pages every few days). This is too slow for a 60-second SLO. We need to learn
about new pages from multiple real-time signals:

| Signal | How fast? | Coverage |
|---|---|---|
| WebSub ping | < 2 seconds | Sites that support WebSub |
| CT Log (new domains) | < 5 seconds | All brand-new HTTPS domains |
| Sitemap `lastmod` change | < 30 seconds | Sites with sitemaps |
| Link extraction | < 60 seconds | Pages linked from already-indexed pages |

Running all four in parallel ensures near-complete coverage.

---

## 2. Architecture of the Discovery Layer

```
┌─────────────────────────────────────────────────────────────────────┐
│                     DISCOVERY SERVICE (Python asyncio)               │
│                                                                       │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌───────────┐ │
│   │   WebSub    │  │   Sitemap   │  │   CT Log    │  │   Link    │ │
│   │ Subscriber  │  │   Watcher   │  │   Scanner   │  │ Extractor │ │
│   │             │  │             │  │             │  │           │ │
│   │ Receives    │  │ Polls       │  │ Streams     │  │ Consumes  │ │
│   │ HTTP POST   │  │ sitemap.xml │  │ Certstream  │  │ crawl     │ │
│   │ pings       │  │ every 30s   │  │ WebSocket   │  │ events    │ │
│   └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └─────┬─────┘ │
│          │                │                 │               │        │
│          └────────────────┴─────────────────┴───────────────┘        │
│                                    │                                  │
│                                    ▼                                  │
│                         on_url_found(URLTask)                         │
│                                    │                                  │
│                    ┌───────────────┴──────────────┐                  │
│                    │                              │                  │
│                    ▼                              ▼                  │
│           ┌──────────────────┐         ┌──────────────────┐         │
│           │   BLOOM FILTER   │ ──new── │  PRIORITY QUEUE  │         │
│           │  (Redis BF)      │         │ (Redis Sorted Set)│         │
│           │  100M URLs       │         │  ZADD score=pri  │         │
│           │  ~120MB RAM      │         │                  │         │
│           └──────────────────┘         └────────┬─────────┘         │
│                                                  │                   │
└──────────────────────────────────────────────────┼───────────────────┘
                                                   │
                                                   ▼
                                         Phase 2: Crawler
                                         (BZPOPMAX queue)
```

### Key Design Decisions

**Why `asyncio` (not threads)?**
All four watchers are I/O-bound — they wait for network responses.
`asyncio` runs them concurrently on a single thread with zero locking.
Threads would add context-switch overhead and require careful locking.

**Why a Bloom filter (not a Redis Set)?**
At 100 million URLs, a Redis Set uses ~6 GB RAM (each URL string stored).
A Bloom filter stores the same 100M URLs in ~120 MB (50× smaller)
at the cost of a 1% false-positive rate (1 in 100 already-seen URLs
is incorrectly accepted as new — acceptable for a crawler).

**Why a Sorted Set (not a List)?**
A List (LPUSH/BRPOP) is FIFO — first in, first out. We want *priority* —
WebSub pings and high-authority domains should be crawled before random
link extractions. A Sorted Set (ZADD/BZPOPMAX) is a priority queue.

---

## 3. Component Deep-Dive

### 3.1 Bloom Filter — URL Deduplication

**File:** `services/discovery/queue/bloom_filter.py`

#### What it is

A probabilistic data structure that answers "have I seen this URL before?"
in O(1) time and O(1) space per query, regardless of how many URLs are stored.

#### How it works

A Bloom filter is an array of bits (initially all 0). When you add a URL:
1. Hash the URL with k different hash functions.
2. Set the bit at each of the k positions to 1.

When you check if a URL exists:
1. Hash the URL with the same k hash functions.
2. If ALL k bits are 1 → "probably seen" (may be a false positive).
3. If ANY bit is 0 → "definitely not seen" (no false negative possible).

```
URL: "https://example.com/page"
Hash 1 → position 3  → set bit 3
Hash 2 → position 17 → set bit 17
Hash 3 → position 42 → set bit 42

Check URL: are bits 3, 17, 42 all set? YES → probably seen.
```

#### Our configuration

```
Capacity:    100,000,000 URLs  (100M)
Error rate:  1% false positives
RAM usage:   ~120 MB
Operations:  BF.ADD, BF.EXISTS, BF.MADD (batch)
TTL:         No TTL on filter itself (persists across restarts)
Reset:       Manual — use bloom.reset() to start fresh
```

#### URL normalisation

Before adding any URL to the filter, we normalise it:
- Strip trailing slash: `example.com/` → `example.com`
- Strip fragment: `example.com/page#section` → `example.com/page`
- Lowercase scheme and host: `HTTPS://EXAMPLE.COM` → `https://example.com`

This ensures `example.com/` and `example.com` are treated as the same URL.

#### Key operations

```python
is_new = await bloom.add(url)        # True = new, False = duplicate
exists = await bloom.exists(url)     # True = probably seen
results = await bloom.add_many(urls) # batch add, returns list[bool]
await bloom.reset()                  # DANGER: wipes all history
```

---

### 3.2 Priority Queue — Redis Sorted Set

**File:** `services/discovery/queue/redis_queue.py`

#### What it is

A Redis Sorted Set where every member is a serialised `URLTask` and the score
is its crawl priority. The crawler pops from the high end (highest score first).

#### Priority scoring

```
Source          Base score  + domain_authority (0–1)  = final priority
────────────────────────────────────────────────────────────────────────
MANUAL          15.0        + 0.8                      = 15.8
WEBSUB          10.0        + 0.9                      = 10.9
SITEMAP          7.0        + 0.5                      = 7.5
CT_LOG           5.0        + 0.3                      = 5.3
LINK             3.0        + 0.7                      = 3.7
```

Higher priority = crawled sooner. A manually submitted URL from a
high-authority domain is crawled before an auto-discovered link from an
unknown domain.

#### Redis commands used

```
ZADD QUEUE:urls NX 10.9 <url_task_json>  → add (NX = skip if exists)
BZPOPMAX QUEUE:urls 30                   → blocking pop, timeout 30s
ZPOPMAX QUEUE:urls 10                    → non-blocking pop, up to 10
ZCARD QUEUE:urls                         → queue depth
ZREVRANGE QUEUE:urls 0 4 WITHSCORES      → peek top 5
```

#### Queue depth monitoring

The `_monitor_queue_depth()` coroutine in `main.py` updates the Prometheus
`indexer_queue_depth` gauge every 5 seconds. If depth exceeds 10,000, a
warning is logged — it means crawlers are falling behind discoveries.

---

### 3.3 Sitemap Watcher

**File:** `services/discovery/watchers/sitemap.py`

#### What it does

Polls `sitemap.xml` files for registered domains every 30 seconds and enqueues
URLs whose `lastmod` timestamp has changed since the last poll.

#### Discovery flow

```
1. Load domain list from Redis set SITEMAP:domains
   (populated via API or manually via scripts/seed_urls.py)

2. For each domain, find its sitemap URL:
   a. Check robots.txt for "Sitemap:" directive  ← preferred
   b. Try /sitemap_index.xml
   c. Try /sitemap.xml

3. Fetch and parse the sitemap XML (auto-decompresses .gz)

4. For sitemap index files: recursively fetch child sitemaps

5. For each <url> in the urlset:
   a. Extract <loc> and <lastmod>
   b. Check Redis: SITEMAP:lastmod:{hash(url)} == lastmod?
   c. If changed or new → enqueue URLTask
   d. Update cached lastmod in Redis (TTL = 7 days)
```

#### Change detection

```python
cache_key = f"SITEMAP:lastmod:{hash(url)}"

# If we have a cached lastmod and it matches → skip (no change)
cached = await redis.get(cache_key)
if cached == lastmod:
    skip()
else:
    enqueue()
    await redis.setex(cache_key, 604800, lastmod)  # cache for 7 days
```

This means on each 30-second poll, only genuinely new or changed URLs are
enqueued. A sitemap with 50,000 URLs might enqueue only 3–4 changed pages.

#### Supported sitemap types

| Format | Example | Notes |
|---|---|---|
| Standard urlset | `sitemap.xml` | Most common |
| Sitemap index | `sitemap_index.xml` | Points to child sitemaps |
| Gzip compressed | `sitemap.xml.gz` | Auto-decompressed |
| News sitemap | `<news:news>` | Extra metadata parsed |

---

### 3.4 WebSub Subscriber

**File:** `services/discovery/watchers/websub.py`

#### What it does

WebSub (formerly PubSubHubbub) is a W3C standard for real-time content updates.
Sites that support it notify our server the instant they publish new content.
This is our fastest discovery mechanism — latency is typically under 2 seconds.

#### Protocol flow

```
Our server                Hub                      Publisher site
─────────────────────────────────────────────────────────────────
                          ← site publishes new content
POST /subscribe ─────────→                          (subscribe)
                          GET /callback?challenge=X  (verify)
GET /callback → return X ←                          (confirm)

Later, when site publishes:
                          ← POST /callback (content) (notify)
Extract URLs, enqueue ←
```

#### Subscription management

We subscribe to feeds using Google's public WebSub hub
(`pubsubhubbub.appspot.com`). Subscriptions expire after 7 days and are
auto-renewed by the `_renew_subscriptions()` method.

Each subscription is stored in Redis:
```
WEBSUB:sub:{topic_hash} → {
    topic:        "https://techcrunch.com/feed/",
    hub:          "https://pubsubhubbub.appspot.com/subscribe",
    secret:       "ab3f9c...",    ← HMAC signing secret
    subscribed_at: 1704067200,
    expires_at:    1704672000
}
```

#### Security: HMAC signature verification

The hub signs every notification with HMAC-SHA256 using our secret.
We verify the `X-Hub-Signature: sha256=<hex>` header before processing.
This prevents fake notifications from being injected.

```python
expected = hmac.new(secret, body, sha256).hexdigest()
received = header[len("sha256="):]
if not hmac.compare_digest(expected, received):
    reject()
```

#### The callback endpoint

The WebSub callback is registered in the API service at:
```
GET  /websub/callback   → handles hub verification challenge
POST /websub/callback   → handles content notifications
```

The API service calls `websub.handle_verification()` and
`websub.handle_notification()` which are defined here.

---

### 3.5 CT Log Scanner

**File:** `services/discovery/watchers/ct_logs.py`

#### What it does

Every SSL/TLS certificate issued worldwide must be published to a public
Certificate Transparency (CT) log. By streaming these logs we discover
*brand new domains* within seconds of them obtaining a certificate.

We connect to **Certstream** — a free WebSocket service that aggregates
all major CT logs and delivers new certificates in real time.

#### Why this matters

This is our only mechanism for discovering truly *new* domains that have
never been seen before. Without CT log scanning, we would only index
pages from domains we already know about.

#### Certstream message format

```json
{
  "message_type": "certificate_update",
  "data": {
    "cert_index": 9876543210,
    "leaf_cert": {
      "subject": { "CN": "example.com" },
      "all_domains": ["example.com", "www.example.com"],
      "issuer":  { "O": "Let's Encrypt" },
      "not_after": "2024-07-01T00:00:00Z"
    }
  }
}
```

#### Domain filtering

Not every domain in a certificate is worth crawling:

| Filter | Example skipped | Reason |
|---|---|---|
| Wildcard | `*.example.com` | Not a real URL |
| IP address | `192.168.1.1` | Not a domain |
| Local TLD | `myapp.local` | Internal network |
| Too short | `x.io` | Usually not public content |
| Bad TLDs | `*.onion` | Dark web, not indexable |

After filtering, we construct `https://{domain}/` and enqueue it.

#### Reconnection logic

Certstream can disconnect. We reconnect with exponential backoff:
```
1s → 2s → 4s → 8s → 16s → 32s → 60s (max)
```

Throughput: ~5–10 certificates/second → ~20–50 new domains/second.

---

### 3.6 Link Extractor

**File:** `services/discovery/watchers/link_extractor.py`

#### What it does

When the crawler finishes fetching a page, it emits a `crawl_complete` event
to a Redis Stream. The link extractor consumes these events, parses all
`<a href>` links from the HTML, filters them, and enqueues unseen URLs.

This is how traditional crawlers find depth — following links from known
pages to discover unknown ones.

#### Consumer group setup

We use a Redis Consumer Group on the `STREAM:crawl_complete` stream:
```
XGROUP CREATE STREAM:crawl_complete link_extractor 0 MKSTREAM
XREADGROUP GROUP link_extractor extractor_1 STREAMS STREAM:crawl_complete >
XACK STREAM:crawl_complete link_extractor {msg_id}
```

Consumer groups allow multiple link extractor instances to share the
work without processing the same event twice.

#### Link filtering rules

| Filter | Skipped | Reason |
|---|---|---|
| Non-HTTP | `mailto:`, `tel:`, `javascript:` | Not web pages |
| Binary files | `.pdf`, `.zip`, `.jpg`, `.mp4` | Not text content |
| Blocked domains | `facebook.com`, CDNs, ad networks | Not useful content |
| Too long | URL > 2000 chars | Usually generated/broken |
| Deep links | depth > 5 hops | Prevent infinite crawl |

#### Focused vs. open crawl mode

```python
# Focused mode (default for MVP):
focused = True   # only follow links within the same domain
# Good for: deep-indexing a specific site

# Open mode:
focused = False  # follow links to any domain (filtered)
# Good for: discovering new sites from known ones
```

#### Crawl depth tracking

Each URLTask carries a `depth` field (hops from seed).
```
Seed URL (depth=0)
  └── Link extracted (depth=1)
        └── Link extracted (depth=2)
              └── ... (depth=5 = MAX_DEPTH, stop here)
```

---

## 4. Data Models

### URLTask

The core message passed through the entire discovery → crawl pipeline.

```python
@dataclass
class URLTask:
    url:              str             # "https://example.com/article/1"
    source:           DiscoverySource # WEBSUB | SITEMAP | CT_LOG | LINK | MANUAL
    domain:           str             # "example.com"  (auto-extracted)
    discovered_at:    float           # Unix timestamp
    depth:            int             # 0 = seed, 1 = direct link, etc.
    domain_authority: float           # 0.0–1.0 (boosts priority)
    priority:         float           # computed: base_score + domain_authority
    metadata:         dict            # source-specific extra data
```

**Serialisation:** URLTask is stored in Redis as JSON:
```json
{
  "url": "https://example.com/article/1",
  "source": "websub",
  "domain": "example.com",
  "discovered_at": 1704067200.123,
  "depth": 0,
  "domain_authority": 0.9,
  "priority": 10.9,
  "metadata": {"feed_url": "https://example.com/feed/"}
}
```

### DiscoverySource enum

```python
class DiscoverySource(str, Enum):
    WEBSUB  = "websub"   # base priority 10, fastest signal
    SITEMAP = "sitemap"  # base priority 7,  reliable
    CT_LOG  = "ct_log"   # base priority 5,  new domains only
    LINK    = "link"     # base priority 3,  depth crawl
    MANUAL  = "manual"   # base priority 15, human submitted
```

---

## 5. Configuration Reference

All configuration is read from environment variables (set in `.env`).

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `REDIS_BLOOM_CAPACITY` | `100000000` | Max URLs in Bloom filter |
| `REDIS_BLOOM_ERROR_RATE` | `0.01` | False positive rate (1%) |
| `SITEMAP_POLL_INTERVAL` | `30` | Seconds between sitemap polls |
| `WEBSUB_CALLBACK_URL` | `http://localhost:8000/websub/callback` | Public callback URL |
| `CERTSTREAM_URL` | `wss://certstream.calidog.io` | Certstream WebSocket |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `LOG_FORMAT` | `json` | `json` or `text` (use `text` for dev) |

**Priority scores** (hardcoded in `shared/models/url_task.py`):

| Source | Base priority |
|---|---|
| MANUAL | 15.0 |
| WEBSUB | 10.0 |
| SITEMAP | 7.0 |
| CT_LOG | 5.0 |
| LINK | 3.0 |

---

## 6. File Reference

```
services/discovery/
├── __init__.py
├── Dockerfile                     Container definition
├── requirements.txt               Python dependencies
├── main.py                        ★ Service entrypoint — orchestrates all watchers
│
├── queue/
│   ├── __init__.py
│   ├── bloom_filter.py            ★ URL dedup (RedisBloom)
│   └── redis_queue.py             ★ Priority queue (Redis Sorted Set)
│
└── watchers/
    ├── __init__.py
    ├── sitemap.py                 ★ Polls sitemap.xml every 30s
    ├── websub.py                  ★ Receives real-time WebSub pings
    ├── ct_logs.py                 ★ Streams new certificates from Certstream
    └── link_extractor.py          ★ Follows links from crawled pages

shared/
├── config.py                      ★ All config from .env
├── logging.py                     Structured JSON logging
├── metrics.py                     All Prometheus metric definitions
└── models/
    └── url_task.py                ★ URLTask dataclass + DiscoverySource enum

★ = files you must understand to work on Phase 1
```

---

## 7. How to Run Phase 1

### Prerequisites

```bash
# 1. Copy and configure environment
cp .env.example .env

# 2. Start Redis (required by discovery service)
docker compose up -d redis

# Verify Redis is running
docker compose ps redis
# Should show: healthy
```

### Start the discovery service

```bash
# Option A: Docker (recommended)
docker compose up discovery

# Option B: Local Python (for development)
source .venv/bin/activate
cd services/discovery
LOG_FORMAT=text python main.py
```

### Verify it's working

```bash
# Watch the logs
docker compose logs -f discovery

# Expected output (LOG_FORMAT=text):
# 2024-01-15 10:23:45 [INFO    ] discovery    | Discovery service starting
# 2024-01-15 10:23:45 [INFO    ] discovery    | Redis connected
# 2024-01-15 10:23:45 [INFO    ] discovery    | Bloom filter created capacity=100000000
# 2024-01-15 10:23:45 [INFO    ] discovery    | Sitemap watcher started poll_interval=30
# 2024-01-15 10:23:45 [INFO    ] discovery    | CT log scanner starting
# 2024-01-15 10:23:46 [INFO    ] discovery    | Connected to Certstream — streaming certificates
# 2024-01-15 10:23:46 [INFO    ] discovery    | Discovery service running

# Check queue depth in Redis
docker exec indexer_redis redis-cli ZCARD QUEUE:urls
# Returns the number of URLs currently queued (should grow over time)

# Peek at the top 5 highest-priority URLs
docker exec indexer_redis redis-cli ZREVRANGE QUEUE:urls 0 4 WITHSCORES
```

### Seed URLs manually (for testing)

```bash
# Add 50 test URLs
python scripts/seed_urls.py

# Add a single specific URL with high priority
python scripts/seed_urls.py --url https://en.wikipedia.org/wiki/Web_crawler

# Clear queue and start fresh
python scripts/seed_urls.py --clear --count 20
```

---

## 8. How to Test Phase 1

### Run unit tests

```bash
# All Phase 1 tests
pytest services/discovery/tests/ -v

# Individual test files
pytest services/discovery/tests/test_bloom_filter.py -v
pytest services/discovery/tests/test_watchers.py -v
pytest services/discovery/tests/test_queue.py -v

# With coverage report
pytest services/discovery/tests/ --cov=services/discovery --cov-report=term-missing
```

### Expected test output

```
PASSED tests/test_bloom_filter.py::TestBloomFilter::test_url_normalisation
PASSED tests/test_bloom_filter.py::TestBloomFilter::test_url_equivalence
PASSED tests/test_bloom_filter.py::TestURLNormalisation::test_different_paths_are_distinct
PASSED tests/test_bloom_filter.py::TestURLTask::test_serialise_deserialise
PASSED tests/test_bloom_filter.py::TestURLTask::test_priority_auto_computed
PASSED tests/test_bloom_filter.py::TestURLTask::test_domain_authority_boosts_priority
PASSED tests/test_bloom_filter.py::TestURLTask::test_domain_auto_extracted
PASSED tests/test_watchers.py::TestCTLogScanner::test_skip_wildcard_domains
PASSED tests/test_watchers.py::TestCTLogScanner::test_skip_ip_addresses
...
PASSED tests/test_queue.py::TestURLQueue::test_priority_ordering
PASSED tests/test_queue.py::TestURLQueue::test_task_json_roundtrip

18 passed in 0.32s
```

### Integration test (requires running Redis)

```bash
# Check that a submitted URL appears in the queue
python scripts/seed_urls.py --url https://example.com/test-page --clear

# Verify it's in the queue
docker exec indexer_redis redis-cli ZREVRANGE QUEUE:urls 0 0 WITHSCORES
# Should show the URL task JSON with priority=15.8 (manual + da bonus)
```

---

## 9. Metrics & Observability

### Prometheus metrics exposed on port 9101

| Metric | Type | Labels | Description |
|---|---|---|---|
| `indexer_urls_discovered_total` | Counter | `source` | Total URLs found, per watcher |
| `indexer_queue_depth` | Gauge | — | Current queue size |
| `indexer_bloom_filter_hits_total` | Counter | — | Duplicate URLs rejected |

### Grafana queries

```promql
# Discovery rate by source (per minute)
rate(indexer_urls_discovered_total[1m]) * 60

# Dedup ratio (% of discoveries that were duplicates)
rate(indexer_bloom_filter_hits_total[5m]) /
(rate(indexer_urls_discovered_total[5m]) + rate(indexer_bloom_filter_hits_total[5m]))

# Queue depth over time (should not grow unboundedly)
indexer_queue_depth
```

### Log fields (JSON format)

Every log line contains:
```json
{
  "timestamp": "2024-01-15T10:23:45.123Z",
  "level": "INFO",
  "service": "discovery",
  "logger": "discovery",
  "message": "URL queued",
  "url": "https://example.com/article/1",
  "source": "websub",
  "priority": 10.9
}
```

---

## 10. Common Issues & Fixes

### "BF.RESERVE failed — module not found"

**Cause:** Redis instance does not have the RedisBloom module loaded.
**Fix:** Use `redis/redis-stack` image (specified in `docker-compose.yml`), not plain `redis`.

```bash
docker compose pull redis
docker compose up -d redis
```

### "Connected to Certstream — then disconnects immediately"

**Cause:** Network timeout or Certstream service is down.
**Fix:** The scanner auto-reconnects. Check logs for the backoff timer.
If Certstream is down, the other three watchers still operate normally.

### "Queue depth stays at 0"

**Cause A:** Sitemap watcher is running but all URLs are already in the Bloom filter (seen before).
**Fix:** Reset the Bloom filter to start fresh: `python scripts/seed_urls.py --clear`

**Cause B:** No domains registered for sitemap polling.
**Fix:** Add domains: `docker exec indexer_redis redis-cli SADD SITEMAP:domains https://techcrunch.com`

**Cause C:** WebSub not receiving pings (callback URL not publicly accessible in dev).
**Fix:** This is expected in local dev. Use `--url` mode to manually submit URLs.

### "Redis connection refused"

**Cause:** Redis not running.
**Fix:**
```bash
docker compose up -d redis
docker compose ps redis   # verify healthy
```

### "URL enqueued but not crawled"

**Cause:** Crawler (Phase 2) service not running yet.
**Fix:** Phase 1 (discovery) and Phase 2 (crawler) are separate services.
Phase 1 only fills the queue — the crawler empties it.
```bash
docker compose up crawler
```

---

## 11. What Phase 2 Receives

When Phase 1 finishes its job, the Redis Sorted Set `QUEUE:urls` contains
URLTask objects ready for the crawler. Here's exactly what Phase 2 gets:

```python
# Phase 2 (crawler) pops from the queue like this:
task = await queue.pop_blocking()

# task is a URLTask with:
task.url              # "https://example.com/article/1"
task.source           # DiscoverySource.WEBSUB
task.domain           # "example.com"
task.priority         # 10.9  (already popped in priority order)
task.discovered_at    # 1704067200.123  (used to measure queue wait time)
task.depth            # 0
task.domain_authority # 0.9
task.metadata         # {"feed_url": "https://example.com/feed/"}
```

Phase 2 uses:
- `task.url` → the URL to fetch
- `task.domain` → to check robots.txt and apply rate limiting
- `task.discovered_at` → to calculate how long it waited in the queue
- `task.depth` → to control how deep to follow links from this page

**Phase 1 guarantees:**
- Every URL in the queue has been deduplicated (no duplicates)
- Every URL has a valid `https://` or `http://` scheme
- Every URL has a domain extracted
- Priority is set correctly — manual > websub > sitemap > ct_log > link

---

*Phase 1 documentation | PRN: 22510103 | Version 1.0*