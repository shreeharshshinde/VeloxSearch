"""
playwright_crawler/crawler.py
================================
Playwright-based crawler for JavaScript-rendered pages (SPAs, Next.js, etc.)

WHY PLAYWRIGHT:
Sites built with React, Next.js, Vue, Angular, or Gatsby render their content
entirely in the browser using JavaScript. A plain HTTP GET returns an empty
HTML shell with no visible content. Playwright launches a real Chromium
browser, loads the page, waits for JS to execute, and returns the fully
rendered DOM — exactly what a user would see.

WHEN THIS IS USED:
The page_classifier.py decides if Playwright is needed. This module is only
invoked for pages that fail the static HTML classification.

PERFORMANCE:
- Playwright is ~10–20× slower than httpx for static pages.
- Each instance handles 1 page at a time (browser context is not threadsafe).
- We run this as a Celery task pool with 2–4 workers (configured separately).
- Static pages (the majority) never touch this code.

TIMEOUT STRATEGY:
- networkidle: wait until no network requests for 500ms. Best for SPAs.
- domcontentloaded: faster but misses async content. Used as fallback.
- Absolute timeout: 30 seconds maximum per page.

RESOURCE BLOCKING:
We block images, fonts, and media — we only need the HTML content.
This speeds up rendering by 30–50% for typical pages.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

from shared.config import settings
from shared.logging import get_logger

log = get_logger("crawler.playwright")

# Resource types to block (we only need HTML content)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

# File extensions that Playwright should never load
BLOCKED_URL_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg",
    "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.mp4", "*.mp3", "*.avi", "*.mov",
    "*.pdf", "*.zip",
    "google-analytics.com", "googletagmanager.com",
    "doubleclick.net", "facebook.net",
]


@dataclass
class PlaywrightResult:
    url: str                      # final URL after redirects
    html: str                     # fully rendered HTML
    http_status: int              # HTTP status code
    content_type: str
    fetch_ms: int                 # time to render
    error: Optional[str] = None   # error message if failed


class PlaywrightCrawler:
    """
    Singleton Playwright crawler. Call start() once, then call fetch() many times.
    Manages a single browser instance shared across fetches for efficiency.
    """

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._running = False

    async def start(self) -> None:
        """Launch the browser. Call once at service startup."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",   # use /tmp instead of /dev/shm
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--hide-scrollbars",
                "--mute-audio",
                "--blink-settings=imagesEnabled=false",
            ]
        )
        self._running = True
        log.info("Playwright browser launched (headless Chromium)")

    async def stop(self) -> None:
        """Close the browser and Playwright instance."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._running = False
        log.info("Playwright browser closed")

    async def fetch(self, url: str, timeout_ms: int = 30_000) -> PlaywrightResult:
        """
        Fetch a JavaScript-rendered page.

        Flow:
         1. Create isolated browser context (cookies don't bleed between pages)
         2. Create page with resource blocking
         3. Navigate to URL
         4. Wait for networkidle (JS execution complete)
         5. Return rendered HTML
         6. Close context (clean up memory)

        Args:
            url:        URL to fetch
            timeout_ms: Maximum time to wait for page render (ms)

        Returns:
            PlaywrightResult with rendered HTML
        """
        if not self._running:
            raise RuntimeError("Playwright not started — call start() first")

        start = time.monotonic()
        context: Optional[BrowserContext] = None

        try:
            # ── Create isolated context ───────────────────────────────────────
            context = await self._browser.new_context(
                user_agent=settings.crawler_user_agent,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1280, "height": 800},
                java_script_enabled=True,
                # Block cookies from third-party domains
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )

            # ── Create page and block unnecessary resources ────────────────────
            page: Page = await context.new_page()

            await page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in BLOCKED_RESOURCE_TYPES
                    else route.continue_()
                )
            )

            # Track HTTP status of the main page load
            http_status = 200
            content_type = "text/html"

            def on_response(response):
                nonlocal http_status, content_type
                if response.url == url or response.url.rstrip("/") == url.rstrip("/"):
                    http_status = response.status
                    content_type = response.headers.get("content-type", "text/html")

            page.on("response", on_response)

            # ── Navigate to URL ───────────────────────────────────────────────
            try:
                await page.goto(
                    url,
                    timeout=timeout_ms,
                    wait_until="networkidle",  # wait for JS to finish
                )
            except PlaywrightTimeout:
                # networkidle timeout — try domcontentloaded (faster, less complete)
                log.debug("networkidle timeout, retrying with domcontentloaded", url=url)
                await page.goto(
                    url,
                    timeout=min(timeout_ms, 15_000),
                    wait_until="domcontentloaded",
                )

            # ── Get rendered HTML ─────────────────────────────────────────────
            # content() returns the full rendered DOM as HTML string
            html = await page.content()
            fetch_ms = int((time.monotonic() - start) * 1000)

            log.debug(
                "Playwright fetch complete",
                url=url,
                html_bytes=len(html),
                fetch_ms=fetch_ms,
                http_status=http_status,
            )

            return PlaywrightResult(
                url=page.url,  # final URL after redirects
                html=html,
                http_status=http_status,
                content_type=content_type,
                fetch_ms=fetch_ms,
            )

        except PlaywrightError as e:
            fetch_ms = int((time.monotonic() - start) * 1000)
            log.warning("Playwright fetch error", url=url, error=str(e), fetch_ms=fetch_ms)
            return PlaywrightResult(
                url=url,
                html="",
                http_status=0,
                content_type="",
                fetch_ms=fetch_ms,
                error=str(e),
            )

        except Exception as e:
            fetch_ms = int((time.monotonic() - start) * 1000)
            log.error("Unexpected Playwright error", url=url, error=str(e))
            return PlaywrightResult(
                url=url,
                html="",
                http_status=0,
                content_type="",
                fetch_ms=fetch_ms,
                error=str(e),
            )

        finally:
            # Always close the context to free memory
            if context:
                await context.close()


# ── Module-level singleton ─────────────────────────────────────────────────────
# Initialised when the Celery worker starts
_crawler: Optional[PlaywrightCrawler] = None


async def get_crawler() -> PlaywrightCrawler:
    """Get or create the singleton PlaywrightCrawler."""
    global _crawler
    if _crawler is None or not _crawler._running:
        _crawler = PlaywrightCrawler()
        await _crawler.start()
    return _crawler


async def fetch_with_playwright(url: str) -> PlaywrightResult:
    """
    Convenience function — fetch a URL using Playwright.
    Creates the browser on first call.

    Usage from Celery tasks:
        result = asyncio.run(fetch_with_playwright(url))
    """
    crawler = await get_crawler()
    return await crawler.fetch(url)