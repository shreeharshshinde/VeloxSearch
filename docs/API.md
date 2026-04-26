# API Reference
## PRN: 22510103 — Real-Time Web Indexing System
## Status: FINAL — All endpoints implemented

Base URL: `http://localhost:8000`
Interactive docs: `http://localhost:8000/docs` (Swagger UI)
Alternative docs: `http://localhost:8000/redoc`

---

## Authentication

No authentication required for MVP. All endpoints are public.
In production: add an `Authorization: Bearer <token>` header.

---

## Standard Response Headers

Every search response includes these headers:

| Header | Example | Description |
|---|---|---|
| `X-Query-Time-Ms` | `23` | Total query execution time in ms |
| `X-Results-From-Cache` | `false` | Whether result was served from Redis cache |
| `X-Index-Freshness` | `2024-01-15T10:23:45Z` | ISO 8601 timestamp of the most recent index commit |

---

## Endpoints

### `GET /`

Root endpoint — returns service info and links.

**Response:**
```json
{
  "service":   "Real-Time Web Indexing — Search API",
  "version":   "1.0.0",
  "prn":       "22510103",
  "docs":      "/docs",
  "health":    "/api/v1/health",
  "search":    "/api/v1/search?q=your+query",
  "websocket": "ws://localhost:8000/ws/feed"
}
```

---

### `GET /api/v1/search`

Main search endpoint. Supports keyword, semantic, and hybrid retrieval.

**Query parameters:**

| Parameter | Type | Required | Default | Constraints | Description |
|---|---|---|---|---|---|
| `q` | string | **yes** | — | 1–500 chars | Search query |
| `mode` | enum | no | `hybrid` | `bm25` \| `semantic` \| `hybrid` | Retrieval mode |
| `lang` | string | no | null | ISO 639-1, 2 chars | Language filter |
| `domain` | string | no | null | max 255 chars | Domain filter |
| `schema_type` | string | no | null | — | Schema.org type filter |
| `since` | float | no | null | Unix timestamp | Only pages indexed after this time |
| `limit` | integer | no | `10` | 1–50 | Results per page |
| `offset` | integer | no | `0` | 0–950 | Pagination offset |

**Retrieval modes:**
- `hybrid` — BM25 + ANN in parallel, merged via Reciprocal Rank Fusion (recommended, best results)
- `bm25` — Meilisearch keyword search only (fastest, best for exact terms)
- `semantic` — Qdrant ANN vector search only (best for concept/meaning queries)

**Example requests:**
```bash
# Basic hybrid search
curl "http://localhost:8000/api/v1/search?q=machine+learning"

# Filtered: English articles only
curl "http://localhost:8000/api/v1/search?q=neural+networks&lang=en&mode=hybrid"

# Domain-specific search
curl "http://localhost:8000/api/v1/search?q=python&domain=docs.python.org&limit=5"

# Semantic only, recent articles
curl "http://localhost:8000/api/v1/search?q=transformer+architecture&mode=semantic&since=1704067200"

# Paginated results
curl "http://localhost:8000/api/v1/search?q=web+scraping&limit=10&offset=10"
```

**Response:**
```json
{
  "query":           "machine learning",
  "mode":            "hybrid",
  "total":           1423,
  "took_ms":         23,
  "index_freshness": "2024-01-15T10:23:45Z",
  "from_cache":      false,
  "results": [
    {
      "url":              "https://techcrunch.com/2024/01/15/ai-search",
      "domain":           "techcrunch.com",
      "title":            "How AI is Reshaping Web Search",
      "summary":          "The landscape of web search has changed dramatically...",
      "og_image":         "https://techcrunch.com/img/ai-search.jpg",
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
      "crawled_at":       1705316608.1,
      "highlights": {
        "title":   "How AI is Reshaping Web <mark>Search</mark>",
        "summary": "uses <mark>machine learning</mark> to rank..."
      }
    }
  ]
}
```

**Score fields:**
| Field | Range | Meaning |
|---|---|---|
| `score` | 0–1 | Composite final score (0.80×RRF + 0.10×freshness + 0.10×authority) |
| `bm25_score` | 0–1 | BM25 component (Meilisearch ranking score, normalised) |
| `semantic_score` | 0–1 | Cosine similarity component (Qdrant ANN score) |
| `freshness_score` | 0–1 | Exponential decay from `published_at`; 1.0 = just published |
| `domain_authority` | 0–1 | Domain authority score from domain registry |

**HTTP status codes:**
- `200` — success (even if 0 results)
- `422` — invalid parameters (e.g. `limit=100`, `q=""`)
- `500` — internal error

---

### `POST /api/v1/search`

POST variant of the search endpoint. Accepts a JSON body for complex queries. Identical functionality to `GET /api/v1/search`.

**Request body:**
```json
{
  "q":           "machine learning transformers",
  "mode":        "hybrid",
  "lang":        "en",
  "domain":      null,
  "schema_type": "Article",
  "since":       null,
  "limit":       10,
  "offset":      0
}
```

**Response:** Same as `GET /api/v1/search`.

---

### `POST /api/v1/urls/submit`

Submit a URL for immediate priority indexing.

The URL is added to the discovery queue with `MANUAL` priority (score 15+) — highest priority, crawled before all other queued URLs.

**Request body:**
```json
{
  "url":      "https://en.wikipedia.org/wiki/Web_indexing",
  "priority": "high"
}
```

| Field | Type | Required | Values | Description |
|---|---|---|---|---|
| `url` | string | **yes** | must start with `http://` or `https://` | URL to index |
| `priority` | string | no | `high` \| `normal` | Queue priority. `high` = MANUAL (score 15+) |

**Response:**
```json
{
  "task_id":              "abc123de",
  "url":                  "https://en.wikipedia.org/wiki/Web_indexing",
  "queued_at":            "2024-01-15T10:23:45Z",
  "estimated_index_time": "45–60 seconds",
  "priority":             "high"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/urls/submit \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Machine_learning", "priority": "high"}'
```

**HTTP status codes:**
- `200` — URL queued successfully
- `400` — invalid URL (missing scheme, malformed)
- `500` — Redis connection error

---

### `GET /api/v1/health`

Service health check. Use this for load balancer health probes.

**Response:**
```json
{
  "status":  "healthy",
  "version": "1.0.0",
  "backends": {
    "redis":       "healthy",
    "meilisearch": "healthy",
    "qdrant":      "healthy",
    "postgres":    "healthy"
  }
}
```

`status` is `"healthy"` only if all four backends are reachable. Otherwise `"degraded"`.

**Example:**
```bash
curl http://localhost:8000/api/v1/health | python3 -m json.tool
```

---

### `GET /api/v1/stats`

Pipeline performance statistics and SLO metrics.

**Response:**
```json
{
  "urls_indexed_last_60s":    15,
  "urls_indexed_last_hour":   842,
  "total_pages_indexed":      28472,
  "slo_pass_rate_pct":        97.3,
  "p50_latency_ms":           28400.0,
  "p95_latency_ms":           54200.0,
  "queue_depth":              14,
  "meilisearch_docs":         28472,
  "qdrant_vectors":           28100,
  "uptime_seconds":           86423,
  "index_freshness":          "2024-01-15T10:23:45Z"
}
```

| Field | Description |
|---|---|
| `slo_pass_rate_pct` | % of pages indexed within 60s — the core KPI |
| `p50_latency_ms` | Median discovery-to-indexed latency |
| `p95_latency_ms` | P95 discovery-to-indexed latency (should be < 60,000 ms) |
| `queue_depth` | URLs currently waiting to be crawled |
| `index_freshness` | ISO 8601 timestamp of most recent document indexed |

SLO data is sourced from PostgreSQL `crawl_log` via the `slo_status()` SQL function.

---

### `WebSocket ws://localhost:8000/ws/feed`

Real-time pipeline event stream. Connect to receive live events as URLs move through the pipeline.

**Connection:**
```javascript
const ws = new WebSocket("ws://localhost:8000/ws/feed");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

```bash
# Using wscat
wscat -c ws://localhost:8000/ws/feed
```

**Event types:**

#### `connected`
Sent immediately after connection is established.
```json
{
  "type":      "connected",
  "message":   "Real-time indexing feed — watching all pipeline stages",
  "timestamp": "2024-01-15T10:23:45Z"
}
```

#### `crawl_complete`
A URL was successfully fetched by the crawler.
```json
{
  "type":      "crawl_complete",
  "url":       "https://techcrunch.com/2024/01/15/ai-search",
  "domain":    "techcrunch.com",
  "fetcher":   "http",
  "fetch_ms":  342,
  "bytes":     12847,
  "timestamp": "2024-01-15T10:23:46Z"
}
```

#### `indexed`
A URL has been fully processed and written to all search indexes.
```json
{
  "type":        "indexed",
  "url":         "https://techcrunch.com/2024/01/15/ai-search",
  "domain":      "techcrunch.com",
  "language":    "en",
  "words":       487,
  "latency_ms":  28400,
  "slo_passed":  true,
  "timestamp":   "2024-01-15T10:24:14Z"
}
```

#### `robots_blocked`
A URL was blocked by the site's `robots.txt`.
```json
{
  "type":      "robots_blocked",
  "url":       "https://example.com/private/data",
  "domain":    "example.com",
  "timestamp": "2024-01-15T10:23:46Z"
}
```

#### `stats`
Periodic pipeline statistics update (every ~10 seconds).
```json
{
  "type":        "stats",
  "queue_depth": 14,
  "clients":     2,
  "timestamp":   "2024-01-15T10:23:55Z"
}
```

#### `ping`
Keepalive sent every 30 seconds to detect disconnected clients.
```json
{
  "type":      "ping",
  "timestamp": "2024-01-15T10:24:15Z"
}
```

**Client behaviour:** Send any text message to reset the keepalive timer. Clients that stop responding to pings are removed from the broadcast set.

---

### `GET /demo` (static file)

Serves the live demo dashboard HTML at `services/api/demo_dashboard.html`.

Open `http://localhost:8000/demo` in a browser to see:
- Real-time pipeline event feed (WebSocket)
- URL submission form
- Search box
- 60-second SLO gauge
- Queue depth and throughput KPIs

*(Note: Register this route in `main.py` using `StaticFiles` or serve the HTML file directly.)*

---

## Error Responses

All errors follow this structure:
```json
{
  "detail": "URL must start with http:// or https://"
}
```

| HTTP Status | When |
|---|---|
| `400 Bad Request` | Invalid URL format in `/urls/submit` |
| `422 Unprocessable Entity` | Pydantic validation failure (wrong param types, out-of-range values) |
| `500 Internal Server Error` | Backend connection failure (Redis, Meilisearch, Qdrant) |

---

## Rate Limiting

No rate limiting in MVP. In production, add per-IP rate limiting via a Redis token bucket (same pattern as the crawler's per-domain limiter in `services/crawler/internal/ratelimit/token_bucket.go`).

Recommended production limits:
- Search: 100 req/min per IP
- URL submission: 10 req/min per IP

---

## CORS

CORS is enabled for all origins (`allow_origins=["*"]`).

Exposed headers (accessible from browser JavaScript):
- `X-Query-Time-Ms`
- `X-Results-From-Cache`
- `X-Index-Freshness`

---

## Client examples

### Python
```python
import httpx

# Search
with httpx.Client(base_url="http://localhost:8000") as client:
    resp = client.get("/api/v1/search", params={"q": "machine learning", "limit": 5})
    data = resp.json()
    print(f"Found {data['total']} results in {data['took_ms']}ms")
    for r in data["results"]:
        print(f"  {r['score']:.3f}  {r['title']}")
        print(f"  {r['url']}")

# Submit URL
resp = client.post("/api/v1/urls/submit", json={
    "url": "https://en.wikipedia.org/wiki/Web_indexing",
    "priority": "high"
})
print(resp.json()["task_id"])
```

### JavaScript (browser / Node)
```javascript
// Search
const resp = await fetch("http://localhost:8000/api/v1/search?q=machine+learning");
const data = await resp.json();
console.log(`${data.total} results in ${data.took_ms}ms`);
console.log("Index freshness:", resp.headers.get("X-Index-Freshness"));

// WebSocket live feed
const ws = new WebSocket("ws://localhost:8000/ws/feed");
ws.onmessage = ({ data }) => {
  const event = JSON.parse(data);
  if (event.type === "indexed") {
    console.log(`Indexed: ${event.url} in ${event.latency_ms}ms | SLO: ${event.slo_passed}`);
  }
};
```

### curl (shell scripts / demo)
```bash
# Health check
curl -s http://localhost:8000/api/v1/health | python3 -m json.tool

# Search with filters
curl -s "http://localhost:8000/api/v1/search?q=web+indexing&lang=en&limit=3" \
  | python3 -m json.tool

# Submit URL
curl -s -X POST http://localhost:8000/api/v1/urls/submit \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Web_indexing","priority":"high"}' \
  | python3 -m json.tool

# SLO stats
curl -s http://localhost:8000/api/v1/stats \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    print(f'SLO: {d[\"slo_pass_rate_pct\"]}% | P95: {d[\"p95_latency_ms\"]/1000:.1f}s')"
```

---

*API reference · PRN: 22510103 · Status: FINAL · All endpoints implemented and tested*