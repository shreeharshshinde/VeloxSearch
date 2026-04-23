"""
shared/models/crawled_page.py
==============================
CrawledPage is the output of the Crawler (Phase 2) and the
input to the Processor (Phase 3).

Lifecycle:
    Crawler fetches URL → stores raw HTML in MinIO
                        → creates CrawledPage
                        → publishes to STREAM:crawl_complete
    Processor consumes STREAM:crawl_complete → reads CrawledPage
                        → runs NLP pipeline
                        → produces IndexedDocument

Fields are kept deliberately raw at this stage — we store everything
the crawler knows and let the processor decide what to use.
"""

import json
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Optional


class FetcherType(str, Enum):
    """Which fetcher was used to get this page."""
    HTTP       = "http"        # Go HTTP fetcher (static HTML)
    PLAYWRIGHT = "playwright"  # Python Playwright (JS-rendered)


class CrawlStatus(str, Enum):
    """Result of the crawl attempt."""
    SUCCESS      = "success"       # page fetched and stored
    ROBOTS_BLOCK = "robots_block"  # blocked by robots.txt
    HTTP_ERROR   = "http_error"    # 4xx/5xx response
    TIMEOUT      = "timeout"       # request timed out
    REDIRECT_MAX = "redirect_max"  # too many redirects
    PARSE_ERROR  = "parse_error"   # could not parse response
    DUPLICATE    = "duplicate"     # content hash matches previous crawl


@dataclass
class CrawledPage:
    """
    Everything the crawler knows about a fetched page.

    Stored as JSON in Redis Stream STREAM:crawl_complete.
    The processor reads this to begin the NLP pipeline.

    Attributes:
        url             : Final URL after all redirects
        original_url    : URL as it was in the queue (before redirects)
        domain          : Hostname of the final URL
        status          : Crawl result (success, error type, etc.)
        fetcher_type    : HTTP (static) or Playwright (JS)
        http_status     : HTTP response status code (200, 301, 404, ...)
        content_type    : Content-Type response header value
        content_hash    : xxHash64 hex of raw body — used for dedup
        minio_key       : Object key in MinIO where raw HTML is stored
                          Format: "{domain}/{content_hash}.html.gz"
                          Empty string if not stored (error or duplicate)
        fetch_bytes     : Raw response body size in bytes
        fetch_ms        : Time from request start to response complete (ms)
        crawled_at      : Unix timestamp when crawl completed
        discovered_at   : Unix timestamp from the URLTask (queue entry time)
        queue_wait_ms   : Time the URL waited in queue before crawling
        depth           : Link depth from seed URL (from URLTask)
        discovery_source: How the URL was found (from URLTask)
        response_headers: Selected response headers (Content-Language, etc.)
        redirect_chain  : List of URLs in the redirect chain
        error           : Error message if status != SUCCESS
    """
    url:              str
    original_url:     str
    domain:           str
    status:           CrawlStatus
    fetcher_type:     FetcherType

    # HTTP details
    http_status:      int   = 0
    content_type:     str   = ""
    content_hash:     str   = ""         # xxHash64 hex, 16 chars
    minio_key:        str   = ""         # empty if not stored

    # Size & timing
    fetch_bytes:      int   = 0
    fetch_ms:         int   = 0
    crawled_at:       float = field(default_factory=time.time)
    discovered_at:    float = 0.0        # from URLTask
    queue_wait_ms:    int   = 0          # crawled_at - discovered_at (ms)

    # Provenance
    depth:            int   = 0
    discovery_source: str   = ""

    # Extra data
    response_headers: dict  = field(default_factory=dict)
    redirect_chain:   list  = field(default_factory=list)
    error:            str   = ""

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_json(self) -> str:
        d = asdict(self)
        d["status"]       = self.status.value
        d["fetcher_type"] = self.fetcher_type.value
        return json.dumps(d)

    @classmethod
    def from_json(cls, data: str | bytes) -> "CrawledPage":
        d = json.loads(data)
        d["status"]       = CrawlStatus(d["status"])
        d["fetcher_type"] = FetcherType(d["fetcher_type"])
        return cls(**d)

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def is_success(self) -> bool:
        return self.status == CrawlStatus.SUCCESS

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    @property
    def total_latency_ms(self) -> int:
        """Total ms from discovery to crawl complete."""
        return int((self.crawled_at - self.discovered_at) * 1000)

    def __repr__(self) -> str:
        return (
            f"CrawledPage(url={self.url!r}, status={self.status.value}, "
            f"fetcher={self.fetcher_type.value}, "
            f"http={self.http_status}, bytes={self.fetch_bytes})"
        )