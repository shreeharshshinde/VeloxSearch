"""
services/api/cache/result_cache.py
=====================================
Redis-backed result cache for search queries.

WHY CACHE SEARCH RESULTS?
--------------------------
Each search query requires:
  - BM25 search in Meilisearch  (~10ms)
  - ANN search in Qdrant        (~5ms)
  - RRF fusion                  (~1ms)
  Total:                        ~16ms per query

Popular queries ("machine learning", "python tutorial") are asked
thousands of times per minute. Without caching, each request hits
both search backends. With a 15-second cache:
  - First request: 16ms (full search)
  - Requests 2–1000: <1ms (cache hit) for 15 seconds
  - Cache miss rate: < 1% for popular queries

CACHE KEY:
  SHA-256(query + lang + domain + schema_type + mode + limit + offset)[:24]
  Prefixed with "CACHE:search:"

  Different parameters → different cache key → independent cache entries.

TTL: 15 seconds
  Short enough to see newly indexed pages quickly.
  Long enough to absorb heavy query traffic on popular terms.

CACHE INVALIDATION VIA PUB/SUB:
  When the Indexer (Phase 4) writes a new document, it publishes to
  CHANNEL:index_commits with the URL, domain, and language.

  The API service subscribes to this channel. On each commit event,
  it invalidates cache keys that might be affected by the new document.

  In practice: we invalidate all cache entries containing the document's
  domain or language. This is a conservative invalidation strategy
  (may invalidate more than necessary) but is simple and safe.

IMPLEMENTATION DETAIL:
  We can't precisely know which cached queries a new document affects.
  Instead, we store a "last_commit_at" timestamp and compare it against
  each cache entry's creation time. Stale entries (created before the
  last commit) are considered invalid and regenerated.
"""

import hashlib
import json
import time
from typing import Optional

import redis.asyncio as aioredis

from shared.logging import get_logger

log = get_logger("api.cache")

CACHE_PREFIX   = "CACHE:search:"
CACHE_TTL      = 15      # seconds
LAST_COMMIT_KEY = "LAST_INDEX_COMMIT"
COMMIT_CHANNEL  = "CHANNEL:index_commits"


class ResultCache:
    """
    Redis-backed search result cache with automatic invalidation.
    """

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client

    async def get(self, cache_key: str) -> Optional[dict]:
        """
        Retrieve cached search results.

        Returns the cached result dict if found and still valid,
        or None if the cache is empty or expired.
        """
        try:
            raw = await self._redis.get(cache_key)
            if raw is None:
                return None

            cached = json.loads(raw)

            # Check if cache was created before the last index commit
            # If so, the results may not include the newest documents
            last_commit = await self._get_last_commit_time()
            cached_at   = cached.get("_cached_at", 0)

            if last_commit and cached_at < last_commit:
                # Stale — new documents were indexed since this was cached
                await self._redis.delete(cache_key)
                log.debug("Cache invalidated (new index commit)", key=cache_key[-8:])
                return None

            log.debug("Cache hit", key=cache_key[-8:])
            return cached

        except Exception as e:
            log.warning("Cache get error", error=str(e))
            return None

    async def set(self, cache_key: str, result: dict) -> None:
        """
        Store search results in the cache.

        Adds _cached_at timestamp for invalidation comparison.
        """
        try:
            result_with_ts = {**result, "_cached_at": time.time()}
            await self._redis.set(
                cache_key,
                json.dumps(result_with_ts, default=str),
                ex=CACHE_TTL,
            )
            log.debug("Cache set", key=cache_key[-8:], ttl=CACHE_TTL)
        except Exception as e:
            log.warning("Cache set error", error=str(e))

    async def invalidate_pattern(self, pattern: str) -> int:
        """
        Delete all cache keys matching a pattern.
        Used for bulk invalidation (e.g., when reindexing a domain).
        """
        try:
            keys = await self._redis.keys(f"{CACHE_PREFIX}{pattern}*")
            if keys:
                return await self._redis.delete(*keys)
            return 0
        except Exception:
            return 0

    async def record_index_commit(self, url: str, domain: str, language: str) -> None:
        """
        Record that a new document was indexed.
        This timestamp is compared against cache entry creation times.
        """
        try:
            await self._redis.set(LAST_COMMIT_KEY, str(time.time()))
        except Exception:
            pass

    async def _get_last_commit_time(self) -> Optional[float]:
        """Get the timestamp of the most recent index commit."""
        try:
            v = await self._redis.get(LAST_COMMIT_KEY)
            return float(v) if v else None
        except Exception:
            return None

    async def get_index_freshness(self) -> str:
        """
        Return ISO 8601 timestamp of last index commit.
        Included in every search response as X-Index-Freshness.
        """
        ts = await self._get_last_commit_time()
        if ts:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return "unknown"

    async def subscribe_to_commits(self) -> None:
        """
        Subscribe to the index commit channel and update last-commit time.
        Run this in a background task at startup.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(COMMIT_CHANNEL)
        log.info("Subscribed to index commit channel", channel=COMMIT_CHANNEL)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = message["data"]
                parts = data.split("|")
                url      = parts[0] if len(parts) > 0 else ""
                domain   = parts[1] if len(parts) > 1 else ""
                language = parts[2] if len(parts) > 2 else ""

                await self.record_index_commit(url, domain, language)
                log.debug("Index commit received", domain=domain, language=language)

            except Exception as e:
                log.warning("Commit event parse error", error=str(e))


def build_cache_key(
    query:       str,
    mode:        str,
    lang:        Optional[str],
    domain:      Optional[str],
    schema_type: Optional[str],
    limit:       int,
    offset:      int,
) -> str:
    """
    Build a deterministic cache key for a search request.
    All parameters that affect results must be included.
    """
    key_data = "|".join([
        query.strip().lower(),
        mode,
        lang    or "",
        domain  or "",
        schema_type or "",
        str(limit),
        str(offset),
    ])
    h = hashlib.sha256(key_data.encode()).hexdigest()[:24]
    return f"{CACHE_PREFIX}{h}"