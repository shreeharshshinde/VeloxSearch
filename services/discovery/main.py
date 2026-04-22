"""
services/discovery/main.py
===========================
Discovery Service — entrypoint.

This service runs all URL discovery watchers concurrently:
  1. Sitemap watcher    — polls sitemap.xml files every 30s
  2. WebSub subscriber  — receives real-time content change pings
  3. CT Log scanner     — streams new SSL certificates (new domains)
  4. Link extractor     — follows links from crawled pages

Every watcher calls `on_url_found(task)` when it discovers a URL.
That callback:
  1. Checks the Bloom filter (skip if already seen)
  2. Pushes to the Redis priority queue (if new)
  3. Updates Prometheus metrics

Run this service with:
  python main.py

In Docker:
  docker compose up discovery
"""

import asyncio
import os
import signal
import sys

# Add shared module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger
from shared.metrics import METRICS, start_metrics_server
from shared.models.url_task import URLTask

from queue.bloom_filter import BloomFilter
from queue.redis_queue import URLQueue
from watchers.sitemap import SitemapWatcher
from watchers.websub import WebSubSubscriber
from watchers.ct_logs import CTLogScanner
from watchers.link_extractor import LinkExtractor

log = get_logger("discovery")


class DiscoveryService:
    """
    Orchestrates all URL discovery mechanisms.

    Handles graceful shutdown on SIGINT/SIGTERM.
    """

    def __init__(self):
        self._redis: aioredis.Redis | None = None
        self._bloom: BloomFilter | None = None
        self._queue: URLQueue | None = None
        self._running = False

        # Watchers (initialised in start())
        self._sitemap_watcher: SitemapWatcher | None = None
        self._websub: WebSubSubscriber | None = None
        self._ct_scanner: CTLogScanner | None = None
        self._link_extractor: LinkExtractor | None = None

    async def start(self) -> None:
        """Initialise all components and start watchers."""
        log.info("Discovery service starting", version="1.0.0")

        # ── Redis connection ──────────────────────────────────────────────────
        self._redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=False,   # keep bytes for BloomFilter commands
            max_connections=20,
        )
        await self._redis.ping()
        log.info("Redis connected", url=settings.redis_url)

        # ── Core components ───────────────────────────────────────────────────
        self._bloom = BloomFilter(self._redis)
        self._queue = URLQueue(self._redis)

        # ── Watchers ──────────────────────────────────────────────────────────
        self._sitemap_watcher = SitemapWatcher(
            redis_client=self._redis,
            on_url_found=self._on_url_found,
        )
        self._websub = WebSubSubscriber(
            redis_client=self._redis,
            on_url_found=self._on_url_found,
        )
        self._ct_scanner = CTLogScanner(
            on_url_found=self._on_url_found,
        )
        self._link_extractor = LinkExtractor(
            redis_client=self._redis,
            on_url_found=self._on_url_found,
        )

        # ── Start Prometheus metrics server ───────────────────────────────────
        start_metrics_server("discovery")
        log.info("Metrics server started", port=9101)

        # ── Start queue depth monitor ─────────────────────────────────────────
        self._running = True

        log.info("All components initialised — starting watchers")

        # Run all watchers concurrently
        tasks = [
            asyncio.create_task(self._sitemap_watcher.start(), name="sitemap"),
            asyncio.create_task(self._ct_scanner.start(),      name="ct_logs"),
            asyncio.create_task(self._link_extractor.start(),  name="link_extractor"),
            asyncio.create_task(self._websub.start(),          name="websub"),
            asyncio.create_task(self._monitor_queue_depth(),   name="queue_monitor"),
        ]

        log.info(
            "Discovery service running",
            watchers=["sitemap", "ct_logs", "link_extractor", "websub"],
        )

        # Wait for all tasks (or until one fails)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        # If any task raised, log it
        for task in done:
            if task.exception():
                log.error(
                    "Watcher crashed",
                    task=task.get_name(),
                    error=str(task.exception()),
                )

        # Cancel remaining tasks on shutdown
        for task in pending:
            task.cancel()

    async def stop(self) -> None:
        """Graceful shutdown."""
        log.info("Discovery service shutting down...")
        self._running = False
        if self._ct_scanner:
            await self._ct_scanner.stop()
        if self._sitemap_watcher:
            await self._sitemap_watcher.stop()
        if self._websub:
            await self._websub.stop()
        if self._redis:
            await self._redis.aclose()
        log.info("Discovery service stopped")

    # ── Core URL handler ──────────────────────────────────────────────────────

    async def _on_url_found(self, task: URLTask) -> None:
        """
        Called by every watcher when a URL is discovered.

        Flow:
          1. Check Bloom filter — skip if already seen
          2. Push to priority queue
          3. Update metrics
        """
        # Step 1: Bloom filter dedup
        is_new = await self._bloom.add(task.url)
        if not is_new:
            METRICS.bloom_filter_hits.inc()
            return

        # Step 2: Enqueue
        added = await self._queue.push(task)
        if not added:
            # Race condition: another worker added it between BF.ADD and ZADD
            return

        # Step 3: Metrics
        METRICS.urls_discovered.labels(source=task.source.value).inc()

        log.debug(
            "URL queued",
            url=task.url,
            source=task.source.value,
            priority=task.priority,
        )

    # ── Queue depth monitor ───────────────────────────────────────────────────

    async def _monitor_queue_depth(self) -> None:
        """
        Periodically update the queue depth Prometheus gauge.
        Runs every 5 seconds.
        """
        while self._running:
            try:
                depth = await self._queue.depth()
                METRICS.queue_depth.set(depth)
                if depth > 10_000:
                    log.warning(
                        "Queue depth high — crawlers may be falling behind",
                        depth=depth,
                    )
            except Exception as e:
                log.error("Queue depth monitor error", error=str(e))
            await asyncio.sleep(5)


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    service = DiscoveryService()

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        log.info("Shutdown signal received", signal=sig.name)
        loop.create_task(service.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    await service.start()


if __name__ == "__main__":
    asyncio.run(main())