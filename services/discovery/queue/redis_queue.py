"""
services/discovery/queue/redis_queue.py
========================================
Priority URL queue backed by a Redis Sorted Set.

WHY A SORTED SET?
-----------------
Redis ZADD / ZPOPMAX on a Sorted Set gives us a priority queue with O(log N)
insert and O(log N) pop — fast enough for 50k+ operations/second.

The score stored with each URL is its priority.
  ZADD QUEUE:urls <priority> <url_task_json>
  ZPOPMAX QUEUE:urls          ← pops the highest-priority item

Higher score = crawled first.
  Manual submissions: 15+
  WebSub pings:       10+
  Sitemap:             7+
  CT log:              5+
  Link extraction:     3+

Domain authority (0.0–1.0) is added to the base score as a bonus,
so high-authority domains are prioritised within each source tier.

BLOCKING POP:
The crawler uses BZPOPMAX (blocking pop) so it idles with zero CPU
when the queue is empty, rather than polling in a tight loop.
"""

import asyncio
import time
from typing import Optional, AsyncIterator

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger
from shared.models.url_task import URLTask

log = get_logger("discovery")


class URLQueue:
    """
    Async priority queue for URL crawl tasks.

    Wraps a Redis Sorted Set with the URLTask serialisation layer.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        key: str = None,
    ):
        self._redis = redis_client
        self.key = key or settings.queue_key

    # ── Writing (Discovery side) ───────────────────────────────────────────────

    async def push(self, task: URLTask) -> bool:
        """
        Add a URL task to the queue.

        Uses ZADD NX (don't update if already present) so re-discovering the
        same URL doesn't reset its position in the queue.

        Args:
            task: URLTask to enqueue

        Returns:
            True if the task was added (new), False if it was already queued.
        """
        result = await self._redis.zadd(
            self.key,
            {task.to_json(): task.priority},
            nx=True,   # only add if not already present
        )
        if result:
            log.debug(
                "URL enqueued",
                url=task.url,
                source=task.source.value,
                priority=task.priority,
            )
        return bool(result)

    async def push_many(self, tasks: list[URLTask]) -> int:
        """
        Batch enqueue multiple URL tasks in a single Redis call.

        Returns:
            Number of new tasks added (existing ones skipped).
        """
        if not tasks:
            return 0

        mapping = {task.to_json(): task.priority for task in tasks}
        result = await self._redis.zadd(self.key, mapping, nx=True)
        log.info(
            "Batch enqueue",
            total=len(tasks),
            added=result,
            skipped=len(tasks) - result,
        )
        return result

    # ── Reading (Crawler side) ─────────────────────────────────────────────────

    async def pop(self) -> Optional[URLTask]:
        """
        Non-blocking pop — returns None if queue is empty.
        Use this when you want to do other work if the queue is empty.
        """
        result = await self._redis.zpopmax(self.key, count=1)
        if not result:
            return None
        raw_json, _score = result[0]
        return URLTask.from_json(raw_json)

    async def pop_blocking(self, timeout: int = 0) -> Optional[URLTask]:
        """
        Blocking pop — blocks until an item is available.
        Crawlers use this to idle efficiently when the queue is empty.

        Args:
            timeout: Seconds to wait. 0 = block indefinitely.

        Returns:
            URLTask when available, None on timeout.
        """
        result = await self._redis.bzpopmax(self.key, timeout=timeout)
        if not result:
            return None
        _key, raw_json, _score = result
        return URLTask.from_json(raw_json)

    async def stream(
        self,
        batch_size: int = 10,
        idle_sleep: float = 0.1,
    ) -> AsyncIterator[URLTask]:
        """
        Async generator that continuously yields URL tasks.

        Crawlers use this in a for loop:
            async for task in queue.stream():
                await crawl(task)

        Args:
            batch_size:  Pop this many tasks per Redis call (efficiency).
            idle_sleep:  Seconds to wait before retrying when queue is empty.
        """
        while True:
            # Pop up to batch_size items at once
            items = await self._redis.zpopmax(self.key, count=batch_size)
            if not items:
                await asyncio.sleep(idle_sleep)
                continue
            for raw_json, _score in items:
                task = URLTask.from_json(raw_json)
                yield task

    # ── Inspection ────────────────────────────────────────────────────────────

    async def depth(self) -> int:
        """Current number of URLs waiting in the queue."""
        return await self._redis.zcard(self.key)

    async def peek(self, count: int = 5) -> list[URLTask]:
        """
        Return the top-N highest-priority tasks without removing them.
        Useful for monitoring and debugging.
        """
        items = await self._redis.zrevrange(
            self.key, 0, count - 1, withscores=True
        )
        tasks = []
        for raw_json, score in items:
            task = URLTask.from_json(raw_json)
            tasks.append(task)
        return tasks

    async def remove(self, url: str) -> bool:
        """Remove a specific URL from the queue (e.g. if blocked by robots.txt after enqueueing)."""
        # We need to find the JSON key for this URL
        # This is O(N) but only used for maintenance operations
        all_items = await self._redis.zrange(self.key, 0, -1)
        for raw_json in all_items:
            task = URLTask.from_json(raw_json)
            if task.url == url:
                removed = await self._redis.zrem(self.key, raw_json)
                return bool(removed)
        return False

    async def clear(self) -> int:
        """Remove all items from the queue. Returns count of removed items."""
        depth = await self.depth()
        await self._redis.delete(self.key)
        log.warning("URL queue cleared", items_removed=depth)
        return depth