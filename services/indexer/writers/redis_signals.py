"""
services/indexer/writers/redis_signals.py
==========================================
Writes per-document ranking signals to Redis for fast retrieval
by the Phase 5 reranker and cache invalidation system.

WHY A SEPARATE REDIS SIGNALS STORE?
-------------------------------------
The Phase 5 API needs ranking signals (freshness, authority) during query
time to rerank results. Fetching these from PostgreSQL on every search
would add 5–15ms of latency per query. Redis can serve them in <1ms.

WHAT WE STORE:
  SIGNALS:{url_hash} → Hash {
    freshness_score:    "0.9832"
    domain_authority:   "0.8500"
    word_count:         "487"
    language:           "en"
    indexed_at:         "1704067245.3"
    content_hash:       "a3f9c2d8e1b74f56"
  }
  TTL: 7 days (refreshed on every re-crawl)

CACHE INVALIDATION:
  When a new document is indexed, we publish to the
  CHANNEL:index_commits pub/sub channel. The Phase 5 API
  subscribes to this and invalidates any cached search results
  that may be affected by the new document.

  This keeps the 15-second result cache consistent with the index
  — users always see the most recently indexed pages.
"""

import hashlib
import time
from typing import Optional

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger
from shared.models.indexed_document import IndexedDocument

log = get_logger("indexer.redis_signals")

SIGNALS_KEY_PREFIX   = "SIGNALS:"
INDEX_COMMIT_CHANNEL = "CHANNEL:index_commits"
SIGNALS_TTL          = 7 * 24 * 3600   # 7 days in seconds


class RedisSignalsWriter:
    """
    Stores per-document ranking signals in Redis and publishes
    cache-invalidation events after each index commit.
    """

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._ready = False

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=5,
        )
        await self._redis.ping()
        self._ready = True
        log.info("Redis signals writer ready")

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def write(self, doc: IndexedDocument) -> bool:
        """
        Write ranking signals for a document and publish commit event.

        Two operations:
          1. HSET SIGNALS:{hash} field value … (TTL refreshed)
          2. PUBLISH CHANNEL:index_commits {url}  (cache invalidation)

        Args:
            doc: Fully indexed document

        Returns:
            True on success, False on failure.
        """
        if not self._ready:
            log.error("Redis signals writer not connected")
            return False

        try:
            key = self._signals_key(doc.url)

            # Write signals hash
            await self._redis.hset(key, mapping={
                "freshness_score":    str(round(doc.freshness_score, 4)),
                "domain_authority":   str(round(doc.domain_authority, 4)),
                "word_count":         str(doc.word_count),
                "language":           doc.language,
                "indexed_at":         str(time.time()),
                "content_hash":       doc.content_hash,
                "domain":             doc.domain,
                "e2e_latency_ms":     str(doc.e2e_latency_ms),
            })

            # Refresh TTL (7 days from last index)
            await self._redis.expire(key, SIGNALS_TTL)

            # Publish cache-invalidation event
            # The API's result cache subscribes to this channel and
            # invalidates any cached queries that may include this URL.
            await self._redis.publish(
                INDEX_COMMIT_CHANNEL,
                f"{doc.url}|{doc.domain}|{doc.language}",
            )

            log.debug("Redis signals written", url=doc.url, key=key)
            return True

        except Exception as e:
            log.error("Redis signals write failed", url=doc.url, error=str(e))
            return False

    async def get_signals(self, url: str) -> dict:
        """
        Retrieve ranking signals for a URL.
        Used by the Phase 5 reranker.

        Returns empty dict if signals not found (document not yet indexed).
        """
        if not self._ready:
            return {}
        try:
            key = self._signals_key(url)
            data = await self._redis.hgetall(key)
            return {
                "freshness_score":  float(data.get("freshness_score", 0.5)),
                "domain_authority": float(data.get("domain_authority", 0.5)),
                "word_count":       int(data.get("word_count", 0)),
                "language":         data.get("language", "en"),
                "indexed_at":       float(data.get("indexed_at", 0)),
            } if data else {}
        except Exception as e:
            log.error("Redis signals get failed", url=url, error=str(e))
            return {}

    @staticmethod
    def _signals_key(url: str) -> str:
        """Derive stable Redis key from URL (truncated SHA-256)."""
        h = hashlib.sha256(url.encode()).hexdigest()[:16]
        return f"{SIGNALS_KEY_PREFIX}{h}"