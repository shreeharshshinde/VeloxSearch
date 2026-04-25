"""
services/api/routers/search.py
================================
Search API endpoints — the user-facing interface to all four search backends.

ENDPOINTS:
  GET  /api/v1/search           — main search (query params)
  POST /api/v1/search           — main search (request body, for complex filters)
  POST /api/v1/urls/submit      — submit URL for immediate indexing
  GET  /api/v1/urls/{task_id}/status  — poll indexing progress

RESPONSE HEADERS (on every search response):
  X-Index-Freshness: 2024-01-15T10:23:45Z   — last index commit time
  X-Query-Time-Ms:   23                      — total query latency
  X-Results-From-Cache: false                — served from Redis cache?

SEARCH FLOW (hybrid mode):
  1. Check Redis cache (cache hit → return immediately, ~1ms)
  2. Launch BM25 search (Meilisearch) and ANN search (Qdrant) in parallel
  3. RRF fusion merges the two result lists
  4. Composite reranking (freshness + authority signals)
  5. Format response, set cache, return
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query, Request, Response, HTTPException
from fastapi.responses import JSONResponse

from shared.config import settings
from shared.logging import get_logger
from shared.metrics import METRICS

from ..models.request  import SearchRequest, SearchMode, URLSubmitRequest
from ..models.response import SearchResponse, SearchResult, URLSubmitResponse
from ..retrieval.bm25  import BM25Retriever
from ..retrieval.ann   import ANNRetriever
from ..retrieval.fusion import HybridFusion
from ..cache.result_cache import ResultCache, build_cache_key

log = get_logger("api.search")

router = APIRouter(prefix="/api/v1", tags=["Search"])

# ── Module-level singletons (initialised in app lifespan) ─────────────────────
_bm25:    BM25Retriever | None = None
_ann:     ANNRetriever  | None = None
_fusion:  HybridFusion  | None = None
_cache:   ResultCache   | None = None
_redis:   aioredis.Redis | None = None
_start_time = time.time()


def init_search_components(redis_client: aioredis.Redis) -> None:
    """Called at startup to initialise all search components."""
    global _bm25, _ann, _fusion, _cache, _redis
    _redis  = redis_client
    _bm25   = BM25Retriever()
    _ann    = ANNRetriever(redis_client=redis_client)
    _fusion = HybridFusion()
    _cache  = ResultCache(redis_client)
    log.info("Search components initialised")


# ── GET /search ───────────────────────────────────────────────────────────────

@router.get("/search", response_model=SearchResponse, summary="Search indexed pages")
async def search_get(
    response: Response,
    q:           str           = Query(..., min_length=1, max_length=500, description="Search query"),
    mode:        str           = Query("hybrid",  description="bm25 | semantic | hybrid"),
    lang:        Optional[str] = Query(None,      description="Language filter (e.g. 'en')"),
    domain:      Optional[str] = Query(None,      description="Domain filter"),
    schema_type: Optional[str] = Query(None,      description="Schema.org type filter"),
    since:       Optional[float] = Query(None,    description="Unix timestamp lower bound"),
    limit:       int           = Query(10, ge=1, le=50),
    offset:      int           = Query(0,  ge=0, le=950),
):
    """
    Search for pages in the real-time index using keyword, semantic, or hybrid retrieval.

    **Modes:**
    - `hybrid` (default): Best of both worlds — combines BM25 keyword matching with semantic vector search via RRF
    - `bm25`: Pure keyword search (fastest, best for exact term matching)
    - `semantic`: Pure semantic search (best for concept/meaning queries)

    **Filters:** Apply `lang`, `domain`, `schema_type`, `since` to narrow results.

    **Response includes** freshness header showing how recently the index was updated.
    """
    req = SearchRequest(
        q=q, mode=SearchMode(mode), lang=lang, domain=domain,
        schema_type=schema_type, since=since, limit=limit, offset=offset,
    )
    return await _execute_search(req, response)


# ── POST /search ──────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse, summary="Search (POST body)")
async def search_post(req: SearchRequest, response: Response):
    """
    POST variant of the search endpoint. Accepts a JSON body for complex queries.
    Identical functionality to GET /search.
    """
    return await _execute_search(req, response)


# ── Core search logic ─────────────────────────────────────────────────────────

async def _execute_search(req: SearchRequest, response: Response) -> SearchResponse:
    """
    Execute a search query through the full retrieval pipeline.

    Flow:
      1. Build cache key → check Redis cache
      2. Run retrieval (BM25 / ANN / hybrid) in parallel where applicable
      3. Fuse results (RRF for hybrid)
      4. Format SearchResult objects
      5. Cache results, set response headers, return
    """
    total_start = time.monotonic()

    # ── Cache check ───────────────────────────────────────────────────────────
    cache_key = build_cache_key(
        query=req.q, mode=req.mode.value,
        lang=req.lang, domain=req.domain,
        schema_type=req.schema_type,
        limit=req.limit, offset=req.offset,
    )

    if _cache:
        cached = await _cache.get(cache_key)
        if cached:
            METRICS.search_cache_hits.inc()
            took_ms = int((time.monotonic() - total_start) * 1000)
            _set_search_headers(response, took_ms, from_cache=True,
                                freshness=cached.get("index_freshness", ""))
            return SearchResponse(**{**cached, "took_ms": took_ms, "from_cache": True})

    METRICS.search_cache_misses.inc()

    # ── Retrieval ─────────────────────────────────────────────────────────────
    bm25_hits, ann_hits = [], []

    if req.mode in (SearchMode.HYBRID, SearchMode.BM25):
        bm25_future = asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _bm25.search(
                req.q, lang=req.lang, domain=req.domain,
                schema_type=req.schema_type, since=req.since,
                limit=50,  # fetch 50 candidates for fusion
            ),
        )

    if req.mode in (SearchMode.HYBRID, SearchMode.SEMANTIC):
        ann_future = asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _ann.search(
                req.q, lang=req.lang, domain=req.domain,
                schema_type=req.schema_type, since=req.since,
                limit=50,
            ),
        )

    # Await in parallel
    if req.mode == SearchMode.HYBRID:
        bm25_result, ann_result = await asyncio.gather(bm25_future, ann_future)
        bm25_hits = bm25_result.get("hits", [])
        ann_hits  = ann_result.get("hits", [])
        total     = max(bm25_result.get("total", 0), ann_result.get("total", 0))
    elif req.mode == SearchMode.BM25:
        bm25_result = await bm25_future
        bm25_hits   = bm25_result.get("hits", [])
        total       = bm25_result.get("total", 0)
    else:  # SEMANTIC
        ann_result  = await ann_future
        ann_hits    = ann_result.get("hits", [])
        total       = ann_result.get("total", 0)

    # ── Fusion & reranking ────────────────────────────────────────────────────
    if req.mode == SearchMode.HYBRID:
        fused = _fusion.fuse(bm25_hits, ann_hits, limit=req.limit, offset=req.offset)
    elif req.mode == SearchMode.BM25:
        fused = _fusion.bm25_only(bm25_hits, limit=req.limit, offset=req.offset)
    else:
        fused = _fusion.ann_only(ann_hits, limit=req.limit, offset=req.offset)

    # ── Format results ────────────────────────────────────────────────────────
    results = [_format_result(doc) for doc in fused]

    # ── Build response ────────────────────────────────────────────────────────
    freshness = await _cache.get_index_freshness() if _cache else "unknown"
    took_ms   = int((time.monotonic() - total_start) * 1000)

    search_resp = SearchResponse(
        query           = req.q,
        mode            = req.mode.value,
        total           = total,
        took_ms         = took_ms,
        index_freshness = freshness,
        from_cache      = False,
        results         = results,
    )

    # Cache the response
    if _cache:
        await _cache.set(cache_key, {
            **search_resp.model_dump(),
            "index_freshness": freshness,
        })

    # Metrics
    METRICS.search_requests.labels(mode=req.mode.value).inc()
    METRICS.search_latency.observe(took_ms / 1000)

    _set_search_headers(response, took_ms, from_cache=False, freshness=freshness)

    log.info(
        "Search complete",
        query=req.q[:50],
        mode=req.mode.value,
        results=len(results),
        total=total,
        took_ms=took_ms,
    )

    return search_resp


def _format_result(doc: dict) -> SearchResult:
    """Convert a fused result dict into a SearchResult model."""
    highlights = {}
    formatted = doc.get("_formatted", {})
    if formatted:
        for field in ("title", "og_title", "summary", "og_description"):
            if field in formatted:
                highlights[field] = formatted[field]

    return SearchResult(
        url             = doc.get("url", ""),
        domain          = doc.get("domain", ""),
        title           = doc.get("og_title") or doc.get("title") or doc.get("domain", ""),
        summary         = doc.get("summary") or doc.get("og_description", ""),
        og_image        = doc.get("og_image", ""),
        author          = doc.get("author", ""),
        published_at    = doc.get("published_at", ""),
        language        = doc.get("language", ""),
        schema_type     = doc.get("schema_type", ""),
        score           = round(float(doc.get("_composite_score", 0)), 4),
        bm25_score      = round(float(doc.get("_bm25_score", 0)), 4),
        semantic_score  = round(float(doc.get("_ann_score", 0)), 4),
        freshness_score = round(float(doc.get("freshness_score", 0.5)), 4),
        domain_authority= round(float(doc.get("domain_authority", 0.5)), 4),
        indexed_at      = doc.get("indexed_at"),
        crawled_at      = doc.get("crawled_at"),
        highlights      = highlights,
    )


def _set_search_headers(
    response: Response,
    took_ms:  int,
    from_cache: bool,
    freshness: str,
) -> None:
    """Set custom response headers on every search response."""
    response.headers["X-Query-Time-Ms"]      = str(took_ms)
    response.headers["X-Results-From-Cache"] = str(from_cache).lower()
    response.headers["X-Index-Freshness"]    = freshness


# ── URL submission ────────────────────────────────────────────────────────────

@router.post("/urls/submit", response_model=URLSubmitResponse, summary="Submit URL for indexing")
async def submit_url(req: URLSubmitRequest):
    """
    Submit a URL for immediate priority indexing.

    The URL is added to the discovery queue with `manual` priority
    (highest priority — crawled before all other queued URLs).
    Typically indexed within 45–60 seconds.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

    from shared.models.url_task import URLTask, DiscoverySource

    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    task_id = str(uuid.uuid4())[:16]

    try:
        task = URLTask(
            url=url,
            source=DiscoverySource.MANUAL,
            domain_authority=0.8,
            metadata={"task_id": task_id},
        )

        # Push to Redis queue
        if _redis:
            from shared.models.url_task import URLTask
            await _redis.zadd(
                settings.queue_key,
                {task.to_json(): task.priority},
                nx=True,
            )

        log.info("URL submitted for indexing", url=url, task_id=task_id)

        return URLSubmitResponse(
            task_id=task_id,
            url=url,
            queued_at=datetime.now(tz=timezone.utc).isoformat(),
            estimated_index_time="45–60 seconds",
            priority="high" if req.priority == "high" else "normal",
        )

    except Exception as e:
        log.error("URL submission failed", url=url, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))