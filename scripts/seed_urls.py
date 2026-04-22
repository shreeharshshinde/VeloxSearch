"""
scripts/seed_urls.py
=====================
Load test URLs into the discovery queue.

Usage:
    python scripts/seed_urls.py                   # loads default seed list
    python scripts/seed_urls.py --count 1000      # loads 1000 generated test URLs
    python scripts/seed_urls.py --file urls.txt   # loads URLs from a file
    python scripts/seed_urls.py --url https://example.com  # single URL

This script is used to:
1. Pre-warm the queue before a demo
2. Load a batch of test URLs for benchmarking
3. Manually submit a URL without going through the API
"""

import argparse
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis.asyncio as aioredis

from shared.config import settings
from shared.models.url_task import URLTask, DiscoverySource
from shared.logging import get_logger

log = get_logger("seed")

# ── Seed URLs for development and demo ────────────────────────────────────────
SEED_URLS = [
    # Tech news
    "https://news.ycombinator.com/",
    "https://techcrunch.com/",
    "https://arstechnica.com/",
    "https://www.theverge.com/",
    "https://wired.com/",

    # Developer resources
    "https://github.com/trending",
    "https://dev.to/",
    "https://stackoverflow.com/questions?tab=newest",
    "https://docs.python.org/3/",

    # Wikipedia (good for testing — rich structured content)
    "https://en.wikipedia.org/wiki/Web_crawler",
    "https://en.wikipedia.org/wiki/Search_engine_indexing",
    "https://en.wikipedia.org/wiki/Inverted_index",
    "https://en.wikipedia.org/wiki/TF-IDF",
    "https://en.wikipedia.org/wiki/PageRank",
    "https://en.wikipedia.org/wiki/Web_search_engine",

    # Blogs
    "https://martinfowler.com/",
    "https://www.joelonsoftware.com/",
    "https://highscalability.com/",

    # Academic
    "https://arxiv.org/abs/2005.11401",   # RAG paper
    "https://arxiv.org/abs/1706.03762",   # Attention paper
]


async def seed(
    urls: list[str],
    source: DiscoverySource = DiscoverySource.MANUAL,
    priority: float = 15.0,
) -> dict:
    """
    Push a list of URLs into the Redis priority queue.

    Returns:
        dict with 'added', 'skipped', 'total' counts
    """
    redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=False,
    )

    added = 0
    skipped = 0

    try:
        # Check queue depth before
        depth_before = await redis.zcard(settings.queue_key)
        log.info("Seeding URLs", count=len(urls), queue_depth_before=depth_before)

        for url in urls:
            task = URLTask(
                url=url,
                source=source,
                priority=priority,
            )

            # ZADD NX — only add if not already in queue
            result = await redis.zadd(
                settings.queue_key,
                {task.to_json(): task.priority},
                nx=True,
            )

            if result:
                added += 1
                log.debug("Queued", url=url)
            else:
                skipped += 1

        depth_after = await redis.zcard(settings.queue_key)
        log.info(
            "Seeding complete",
            added=added,
            skipped=skipped,
            queue_depth_after=depth_after,
        )

    finally:
        await redis.aclose()

    return {"added": added, "skipped": skipped, "total": len(urls)}


async def main():
    parser = argparse.ArgumentParser(description="Seed URLs into the indexer queue")
    parser.add_argument("--url",   type=str, help="Single URL to add")
    parser.add_argument("--file",  type=str, help="File with one URL per line")
    parser.add_argument("--count", type=int, default=0,
                        help="Generate N synthetic test URLs")
    parser.add_argument("--source", type=str, default="manual",
                        choices=["manual", "sitemap", "websub", "ct_log", "link"])
    args = parser.parse_args()

    urls = []

    if args.url:
        urls = [args.url]
    elif args.file:
        with open(args.file) as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        print(f"Loaded {len(urls)} URLs from {args.file}")
    elif args.count:
        # Generate synthetic test URLs using Wikipedia articles
        base_articles = [
            "Python_(programming_language)", "Machine_learning", "Web_crawler",
            "Search_engine", "Natural_language_processing", "Deep_learning",
            "Transformer_(machine_learning_model)", "BERT_(language_model)",
        ]
        urls = []
        for i in range(args.count):
            article = base_articles[i % len(base_articles)]
            urls.append(f"https://en.wikipedia.org/wiki/{article}?test={i}")
        print(f"Generated {len(urls)} test URLs")
    else:
        urls = SEED_URLS
        print(f"Using {len(urls)} default seed URLs")

    source = DiscoverySource(args.source)
    result = await seed(urls, source=source)

    print(f"\n✓ Done: {result['added']} added, {result['skipped']} already queued")


if __name__ == "__main__":
    asyncio.run(main())