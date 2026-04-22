"""
services/discovery/watchers/link_extractor.py
===============================================
Link extractor — discovers new URLs by parsing outbound links from
already-crawled pages.

HOW IT WORKS:
1. When a page is successfully crawled, its raw HTML is stored in MinIO.
2. The link extractor consumes a Redis stream of crawl-complete events.
3. For each completed page, it parses all <a href> links.
4. Links pointing to unseen URLs are enqueued for crawling.

This is how traditional crawlers find depth — starting from a set of seed
URLs and following links outward.

LINK FILTERING:
Not every link on a page should be crawled.  We filter out:
- Non-HTTP(S) links (mailto:, tel:, javascript:, etc.)
- Fragment-only links (#section)
- Links to binary files (PDF, ZIP, images, etc.)
- Links that are too long (> 2000 chars — usually generated/broken)
- Links to excluded domains (social media, CDNs, ad networks)

CRAWL DEPTH CONTROL:
We track link depth (how many hops from the seed URL).
Pages beyond MAX_DEPTH are not enqueued, preventing infinite crawl loops.

DOMAIN FOCUSING:
In focused crawl mode, we only follow links within the same domain as the
source page.  This prevents crawling the entire web from one seed.
"""

import asyncio
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger
from shared.models.url_task import URLTask, DiscoverySource

log = get_logger("discovery")

# File extensions we don't want to crawl
SKIP_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".eot",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm",
    ".css",  # stylesheets, not content pages
}

# Domains to never crawl (CDNs, social, analytics, ad networks)
BLOCKED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "tiktok.com", "linkedin.com",
    "google.com", "googleapis.com", "gstatic.com",
    "cloudflare.com", "fastly.com", "akamai.com",
    "doubleclick.net", "googlesyndication.com", "amazon-adsystem.com",
    "analytics.google.com", "googletagmanager.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
}

MAX_DEPTH = 5          # maximum link hops from seed
MAX_LINKS_PER_PAGE = 100  # max links to extract per page (avoid link farms)


class LinkParser(HTMLParser):
    """
    Lightweight HTML parser that extracts all <a href> links.
    Faster than BeautifulSoup for this specific task.
    """

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href", "").strip()

        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            return

        # Resolve relative URLs
        try:
            absolute = urljoin(self.base_url, href)
            # Strip fragment
            parsed = urlparse(absolute)
            clean = urlunparse(parsed._replace(fragment=""))
            if clean.startswith("http"):
                self.links.append(clean)
        except Exception:
            pass


class LinkExtractor:
    """
    Subscribes to crawl-complete events and extracts outbound links.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        on_url_found,           # async callback: (URLTask) -> None
        focused: bool = False,  # if True, only follow same-domain links
    ):
        self._redis = redis_client
        self._on_url_found = on_url_found
        self._focused = focused

        # Stream key where crawler publishes completed page events
        self._crawl_complete_stream = "STREAM:crawl_complete"
        self._consumer_group = "link_extractor"
        self._consumer_name = "extractor_1"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start consuming crawl-complete events and extracting links."""
        await self._ensure_consumer_group()
        log.info("Link extractor started", focused=self._focused)
        await self._consume_loop()

    async def _ensure_consumer_group(self) -> None:
        """Create the consumer group if it doesn't exist."""
        try:
            await self._redis.xgroup_create(
                self._crawl_complete_stream,
                self._consumer_group,
                id="0",
                mkstream=True,
            )
        except Exception as e:
            if "BUSYGROUP" in str(e):
                pass  # group already exists
            else:
                raise

    # ── Event consumption ─────────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """Main loop: read crawl-complete events and process them."""
        while True:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=self._consumer_group,
                    consumername=self._consumer_name,
                    streams={self._crawl_complete_stream: ">"},
                    count=10,
                    block=5000,  # block for 5s if no messages
                )

                if not messages:
                    continue

                for _stream, records in messages:
                    for msg_id, fields in records:
                        await self._process_event(fields)
                        # Acknowledge processing
                        await self._redis.xack(
                            self._crawl_complete_stream,
                            self._consumer_group,
                            msg_id,
                        )

            except Exception as e:
                log.error("Link extractor error", error=str(e))
                await asyncio.sleep(1)

    async def _process_event(self, fields: dict) -> None:
        """
        Process a crawl-complete event.

        Event fields:
            url:      The crawled URL
            html:     Raw HTML content (or empty if stored in MinIO)
            depth:    Crawl depth of the source page
            domain:   Source domain
        """
        url = fields.get(b"url", b"").decode()
        html = fields.get(b"html", b"").decode()
        depth = int(fields.get(b"depth", b"0"))
        source_domain = fields.get(b"domain", b"").decode()

        if depth >= MAX_DEPTH:
            log.debug("Max depth reached, not following links", url=url, depth=depth)
            return

        if not html:
            log.debug("No HTML in event, skipping link extraction", url=url)
            return

        # Extract and filter links
        links = self._extract_links(html, url)
        links = self._filter_links(links, source_domain)

        # Enqueue new links
        new_count = 0
        for link in links[:MAX_LINKS_PER_PAGE]:
            task = URLTask(
                url=link,
                source=DiscoverySource.LINK,
                depth=depth + 1,
                metadata={"referrer": url},
            )
            await self._on_url_found(task)
            new_count += 1

        if new_count > 0:
            log.debug(
                "Links extracted",
                source=url,
                extracted=len(links),
                enqueued=new_count,
                depth=depth + 1,
            )

    # ── Link processing ────────────────────────────────────────────────────────

    def _extract_links(self, html: str, base_url: str) -> list[str]:
        """Parse HTML and extract all valid absolute URLs."""
        parser = LinkParser(base_url)
        parser.feed(html)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for link in parser.links:
            if link not in seen:
                seen.add(link)
                unique.append(link)
        return unique

    def _filter_links(self, links: list[str], source_domain: str) -> list[str]:
        """Apply filtering rules to extracted links."""
        filtered = []
        for url in links:
            if not self._should_crawl(url, source_domain):
                continue
            filtered.append(url)
        return filtered

    def _should_crawl(self, url: str, source_domain: str) -> bool:
        """
        Decide whether a URL should be enqueued for crawling.
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return False

        # Must be HTTP(S)
        if parsed.scheme not in ("http", "https"):
            return False

        domain = parsed.netloc.lower()
        # Strip www. for comparison
        bare_domain = domain.removeprefix("www.")

        # Skip blocked domains
        for blocked in BLOCKED_DOMAINS:
            if bare_domain == blocked or bare_domain.endswith("." + blocked):
                return False

        # Skip unwanted file types
        path = parsed.path.lower()
        for ext in SKIP_EXTENSIONS:
            if path.endswith(ext):
                return False

        # Skip URLs that are too long (usually generated/broken)
        if len(url) > 2000:
            return False

        # Focused mode: only same-domain links
        if self._focused:
            source_bare = source_domain.removeprefix("www.")
            if bare_domain != source_bare:
                return False

        return True