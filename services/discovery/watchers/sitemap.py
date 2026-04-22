"""
services/discovery/watchers/sitemap.py
========================================
Sitemap watcher — discovers new URLs by polling sitemap.xml files.

HOW IT WORKS:
1. A list of seed domains is maintained (from DB + manually registered sites).
2. Every SITEMAP_POLL_INTERVAL seconds, we fetch each domain's sitemap.
3. We compare the `lastmod` timestamps against what we've seen before.
4. Only URLs with a new/changed `lastmod` are enqueued for crawling.

SITEMAP FORMATS SUPPORTED:
- Plain sitemap.xml       (urlset > url entries)
- Sitemap index files     (sitemapindex > sitemap entries pointing to child sitemaps)
- Compressed .xml.gz      (auto-decompressed)
- news:news sitemaps      (Google News sitemap extension)

WHY NOT JUST CRAWL THE WHOLE SITEMAP EVERY TIME?
Large sitemaps can have 50,000+ URLs.  We track `lastmod` in Redis so we only
enqueue genuinely changed/new URLs, keeping queue pressure low.
"""

import asyncio
import gzip
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

from shared.config import settings
from shared.logging import get_logger
from shared.models.url_task import URLTask, DiscoverySource

log = get_logger("discovery")

# XML namespaces used in sitemap files
NS = {
    "sm":    "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news":  "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
    "video": "http://www.google.com/schemas/sitemap-video/1.1",
}


class SitemapWatcher:
    """
    Polls sitemap.xml files for a set of registered domains.

    Emits URLTask objects for every new or recently modified URL found.
    """

    def __init__(
        self,
        redis_client,         # aioredis.Redis — for lastmod cache
        on_url_found,         # async callback: (URLTask) -> None
        poll_interval: int = None,
    ):
        self._redis = redis_client
        self._on_url_found = on_url_found
        self._poll_interval = poll_interval or settings.sitemap_poll_interval
        self._client: Optional[httpx.AsyncClient] = None

        # Seed domains — in production these come from PostgreSQL
        # For MVP, we use a hardcoded list + any domains registered via API
        self._domains: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the sitemap polling loop. Runs forever."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
        )
        log.info("Sitemap watcher started", poll_interval=self._poll_interval)

        # Load initial domains
        await self._load_domains()

        while True:
            start = time.monotonic()
            await self._poll_all_domains()
            elapsed = time.monotonic() - start

            # Sleep for the remainder of the interval
            sleep_for = max(0, self._poll_interval - elapsed)
            log.debug(
                "Sitemap poll cycle complete",
                domains=len(self._domains),
                elapsed_s=round(elapsed, 2),
                next_poll_in_s=round(sleep_for, 1),
            )
            await asyncio.sleep(sleep_for)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()

    # ── Domain management ─────────────────────────────────────────────────────

    async def add_domain(self, domain: str) -> None:
        """Register a new domain for sitemap monitoring."""
        self._domains.add(domain)
        log.info("Domain added to sitemap watcher", domain=domain)

    async def _load_domains(self) -> None:
        """
        Load initial domain list.
        In MVP: reads from Redis set SITEMAP:domains.
        In production: queries PostgreSQL domains table.
        """
        raw = await self._redis.smembers("SITEMAP:domains")
        if raw:
            self._domains = {d.decode() for d in raw}
            log.info("Loaded domains from Redis", count=len(self._domains))
        else:
            # Fallback seed list for development
            self._domains = {
                "https://news.ycombinator.com",
                "https://www.wikipedia.org",
                "https://techcrunch.com",
            }
            log.info("Using seed domains for development", count=len(self._domains))

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_all_domains(self) -> None:
        """Poll all registered domains concurrently (bounded concurrency)."""
        semaphore = asyncio.Semaphore(10)  # max 10 concurrent sitemap fetches

        async def _bounded_poll(domain: str):
            async with semaphore:
                await self._poll_domain(domain)

        await asyncio.gather(
            *[_bounded_poll(d) for d in self._domains],
            return_exceptions=True,
        )

    async def _poll_domain(self, domain: str) -> None:
        """Discover the sitemap URL for a domain and process it."""
        # Try standard sitemap locations in order
        candidates = [
            urljoin(domain, "/sitemap_index.xml"),
            urljoin(domain, "/sitemap.xml"),
            urljoin(domain, "/sitemap-index.xml"),
        ]

        # Also check robots.txt for a Sitemap: directive
        robots_sitemap = await self._get_sitemap_from_robots(domain)
        if robots_sitemap:
            candidates.insert(0, robots_sitemap)

        for url in candidates:
            try:
                content = await self._fetch_sitemap(url)
                if content:
                    await self._process_sitemap(content, url)
                    return
            except Exception as e:
                log.debug("Sitemap candidate failed", url=url, error=str(e))

        log.debug("No sitemap found for domain", domain=domain)

    async def _get_sitemap_from_robots(self, domain: str) -> Optional[str]:
        """Parse robots.txt to find Sitemap: directive."""
        robots_url = urljoin(domain, "/robots.txt")
        try:
            resp = await self._client.get(robots_url)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return None

    async def _fetch_sitemap(self, url: str) -> Optional[bytes]:
        """Fetch a sitemap URL, handling gzip compression."""
        resp = await self._client.get(url)
        resp.raise_for_status()

        content = resp.content
        # Auto-detect and decompress gzip
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        return content

    async def _process_sitemap(self, content: bytes, source_url: str) -> None:
        """Parse sitemap XML and emit URL tasks for new/changed URLs."""
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            log.warning("Sitemap XML parse error", url=source_url, error=str(e))
            return

        # Sitemap index — contains links to child sitemaps
        if root.tag in (
            "sitemapindex",
            "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemapindex",
        ):
            await self._process_sitemap_index(root, source_url)
        else:
            # Regular urlset
            await self._process_urlset(root, source_url)

    async def _process_sitemap_index(self, root: ET.Element, source_url: str) -> None:
        """Process a sitemap index — recursively fetch child sitemaps."""
        child_urls = []
        for sitemap_el in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap"):
            loc_el = sitemap_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            if loc_el is not None and loc_el.text:
                child_urls.append(loc_el.text.strip())

        log.debug("Sitemap index found", parent=source_url, children=len(child_urls))

        # Process child sitemaps concurrently (bounded)
        semaphore = asyncio.Semaphore(5)

        async def _fetch_child(url: str):
            async with semaphore:
                try:
                    content = await self._fetch_sitemap(url)
                    if content:
                        await self._process_sitemap(content, url)
                except Exception as e:
                    log.debug("Child sitemap failed", url=url, error=str(e))

        await asyncio.gather(*[_fetch_child(u) for u in child_urls])

    async def _process_urlset(self, root: ET.Element, source_url: str) -> None:
        """Process a urlset — extract and enqueue individual page URLs."""
        new_count = 0
        total_count = 0

        for url_el in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
            loc_el = url_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            if loc_el is None or not loc_el.text:
                continue

            url = loc_el.text.strip()
            total_count += 1

            # Extract lastmod if available
            lastmod_el = url_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")
            lastmod = lastmod_el.text.strip() if lastmod_el is not None else None

            # Check if this URL/lastmod combination is new
            if not await self._is_new_or_changed(url, lastmod):
                continue

            new_count += 1
            task = URLTask(
                url=url,
                source=DiscoverySource.SITEMAP,
                metadata={
                    "lastmod": lastmod,
                    "sitemap_source": source_url,
                },
            )
            await self._on_url_found(task)

        if new_count > 0:
            log.info(
                "Sitemap URLs discovered",
                sitemap=source_url,
                total=total_count,
                new=new_count,
            )

    # ── Change detection ──────────────────────────────────────────────────────

    async def _is_new_or_changed(self, url: str, lastmod: Optional[str]) -> bool:
        """
        Check if a URL is genuinely new or has been modified since last crawl.

        Strategy:
        - No lastmod → always enqueue (we don't know if it changed)
        - Has lastmod → compare against cached lastmod in Redis
        """
        cache_key = f"SITEMAP:lastmod:{hash(url)}"

        if lastmod is None:
            # No lastmod — check if we've seen this URL at all recently
            seen = await self._redis.exists(cache_key)
            if not seen:
                # Cache that we've seen it (TTL = poll interval, so we re-check next cycle)
                await self._redis.setex(cache_key, self._poll_interval * 2, "seen")
                return True
            return False

        # Compare lastmod against cached value
        cached_lastmod = await self._redis.get(cache_key)
        if cached_lastmod and cached_lastmod.decode() == lastmod:
            return False  # unchanged

        # New or updated — store new lastmod (TTL = 7 days)
        await self._redis.setex(cache_key, 604800, lastmod)
        return True