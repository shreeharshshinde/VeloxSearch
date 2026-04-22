"""
shared/models/url_task.py
=========================
URLTask is the message format for every URL in the discovery queue.

When the discovery service finds a new URL it creates a URLTask and pushes
it to the Redis priority queue.  The crawler service pops URLTasks and fetches
the corresponding pages.

The dataclass is JSON-serialisable so it can be stored in Redis as a string
and deserialised by any service that pops it from the queue.

Lifecycle:
    Discovery → (URLTask) → Redis Queue → Crawler → MinIO
"""

import json
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Optional


class DiscoverySource(str, Enum):
    """
    Identifies how this URL was discovered.
    Used for priority scoring and analytics.
    """
    WEBSUB   = "websub"      # site pinged us via WebSub/PubSubHubbub
    SITEMAP  = "sitemap"     # found in a sitemap.xml
    CT_LOG   = "ct_log"      # discovered from certificate transparency logs
    LINK     = "link"        # extracted from an already-crawled page
    MANUAL   = "manual"      # manually submitted via API


# Priority scores per source.
# Higher score = crawled sooner (Redis Sorted Set: higher score = first out).
PRIORITY_MAP: dict[DiscoverySource, float] = {
    DiscoverySource.MANUAL:  15.0,
    DiscoverySource.WEBSUB:  10.0,
    DiscoverySource.SITEMAP:  7.0,
    DiscoverySource.CT_LOG:   5.0,
    DiscoverySource.LINK:     3.0,
}


@dataclass
class URLTask:
    """
    A single URL waiting to be crawled.

    Attributes:
        url         : Fully qualified URL, e.g. "https://example.com/post/1"
        source      : How this URL was discovered (see DiscoverySource)
        priority    : Redis Sorted Set score. Higher = crawled sooner.
                      Auto-calculated from source + domain_authority if not set.
        domain      : Extracted domain, e.g. "example.com"
        discovered_at: Unix timestamp when this URL was first seen
        depth       : Link depth from seed URL (0 = seed, 1 = direct link, etc.)
        domain_authority: 0.0–1.0 score boosting priority for high-DA domains
        metadata    : Optional extra data from the discovery source
                      (e.g. lastmod from sitemap, OG title from link)
    """
    url: str
    source: DiscoverySource
    domain: str = ""
    discovered_at: float = field(default_factory=time.time)
    depth: int = 0
    domain_authority: float = 0.5           # default mid-range authority
    priority: float = 0.0                   # computed in __post_init__
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        # Auto-extract domain if not provided
        if not self.domain:
            from urllib.parse import urlparse
            parsed = urlparse(self.url)
            self.domain = parsed.netloc

        # Auto-compute priority if not explicitly set
        if self.priority == 0.0:
            base = PRIORITY_MAP.get(self.source, 3.0)
            # domain_authority is 0–1, adds up to +1.0 bonus
            self.priority = base + self.domain_authority

        # Normalise source to enum
        if isinstance(self.source, str):
            self.source = DiscoverySource(self.source)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_json(self) -> str:
        """Serialise to JSON string for Redis storage."""
        d = asdict(self)
        d["source"] = self.source.value      # enum → string
        return json.dumps(d)

    @classmethod
    def from_json(cls, data: str | bytes) -> "URLTask":
        """Deserialise from JSON string retrieved from Redis."""
        d = json.loads(data)
        d["source"] = DiscoverySource(d["source"])
        return cls(**d)

    # ── Convenience ───────────────────────────────────────────────────────────

    def age_seconds(self) -> float:
        """How long this task has been waiting in the queue."""
        return time.time() - self.discovered_at

    def __repr__(self) -> str:
        return (
            f"URLTask(url={self.url!r}, source={self.source.value}, "
            f"priority={self.priority:.1f})"
        )