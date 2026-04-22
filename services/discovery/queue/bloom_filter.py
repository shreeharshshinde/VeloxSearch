"""
services/discovery/queue/bloom_filter.py
=========================================
URL deduplication using a Redis Bloom filter (RedisBloom module).

WHY A BLOOM FILTER?
-------------------
A naïve approach would store every seen URL in a Redis Set.  At 100 million
URLs, each averaging 60 bytes, that's ~6 GB of RAM just for the set.

A Bloom filter gives us the same "have I seen this URL?" query at:
  - 100M URLs, 1% false-positive rate → ~120 MB RAM  (50× smaller)
  - O(1) lookup and insert time
  - Trade-off: 1% of *already-seen* URLs will be incorrectly reported as "new"
    (false positive) — meaning they get crawled twice.  This is acceptable
    and far better than crawling nothing at all due to OOM.

OPERATIONS:
  bloom.add(url)          → bool  (True = was new, False = already seen)
  bloom.exists(url)       → bool  (True = probably seen, False = definitely new)
  bloom.add_many(urls)    → list[bool]
  bloom.size()            → int   (approx. number of items added)

RESET POLICY:
  The filter is reset every 24 hours via a TTL on the Redis key.
  This allows re-crawling of pages that may have been updated.
"""

import hashlib
from typing import Optional

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger

log = get_logger("discovery")


class BloomFilter:
    """
    Async wrapper around the RedisBloom BF.ADD / BF.EXISTS commands.

    The filter is initialised lazily on first use (BF.RESERVE only runs
    if the key doesn't already exist, so safe to call on every startup).
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        key: str = None,
        capacity: int = None,
        error_rate: float = None,
    ):
        self._redis = redis_client
        self.key = key or settings.bloom_key
        self.capacity = capacity or settings.redis_bloom_capacity
        self.error_rate = error_rate or settings.redis_bloom_error_rate
        self._initialised = False

    async def _ensure_initialised(self) -> None:
        """
        Create the Bloom filter if it doesn't exist yet.
        BF.RESERVE key error_rate capacity
        Safe to call multiple times — Redis returns an error if key exists,
        which we silently ignore.
        """
        if self._initialised:
            return
        try:
            await self._redis.execute_command(
                "BF.RESERVE", self.key, self.error_rate, self.capacity
            )
            log.info(
                "Bloom filter created",
                key=self.key,
                capacity=self.capacity,
                error_rate=self.error_rate,
            )
        except Exception as e:
            if "ERR item exists" in str(e) or "BUSYKEY" in str(e):
                log.debug("Bloom filter already exists, reusing", key=self.key)
            else:
                log.error("Failed to create Bloom filter", error=str(e))
                raise
        self._initialised = True

    @staticmethod
    def _normalise(url: str) -> str:
        """
        Normalise URL before adding to filter.
        - Strip trailing slash (example.com/ == example.com)
        - Lowercase scheme and host
        - Remove #fragment (same page, different anchor)
        """
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url.strip())
        normalised = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            parsed.query,
            "",  # strip fragment
        ))
        return normalised

    async def add(self, url: str) -> bool:
        """
        Add a URL to the filter.

        Returns:
            True  → URL was new (not previously seen)
            False → URL was already in the filter (skip crawling)
        """
        await self._ensure_initialised()
        normalised = self._normalise(url)
        result = await self._redis.execute_command("BF.ADD", self.key, normalised)
        was_new = bool(result)
        if not was_new:
            log.debug("Bloom filter: duplicate URL skipped", url=url)
        return was_new

    async def exists(self, url: str) -> bool:
        """
        Check if a URL has probably been seen before.

        Returns:
            True  → probably seen (could be false positive)
            False → definitely not seen
        """
        await self._ensure_initialised()
        normalised = self._normalise(url)
        result = await self._redis.execute_command("BF.EXISTS", self.key, normalised)
        return bool(result)

    async def add_many(self, urls: list[str]) -> list[bool]:
        """
        Batch add multiple URLs.  More efficient than calling add() in a loop.

        Returns:
            List of booleans — True = URL was new, False = duplicate.
        """
        await self._ensure_initialised()
        if not urls:
            return []
        normalised = [self._normalise(u) for u in urls]
        results = await self._redis.execute_command(
            "BF.MADD", self.key, *normalised
        )
        new_count = sum(1 for r in results if r)
        log.debug(
            "Bloom filter batch add",
            total=len(urls),
            new=new_count,
            duplicates=len(urls) - new_count,
        )
        return [bool(r) for r in results]

    async def size(self) -> Optional[int]:
        """
        Return the approximate number of items in the filter.
        Uses BF.INFO which returns filter metadata.
        """
        try:
            info = await self._redis.execute_command("BF.INFO", self.key)
            # BF.INFO returns alternating key-value pairs
            info_dict = dict(zip(info[::2], info[1::2]))
            return info_dict.get(b"Number of items inserted", None)
        except Exception:
            return None

    async def reset(self) -> None:
        """
        Delete and recreate the filter (use when starting a fresh crawl).
        WARNING: this clears all deduplication history.
        """
        await self._redis.delete(self.key)
        self._initialised = False
        await self._ensure_initialised()
        log.warning("Bloom filter reset — all deduplication history cleared")