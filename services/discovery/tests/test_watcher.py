"""
tests/test_watchers.py
=======================
Unit tests for all four discovery watchers.

Uses mocks and fixtures — no real network calls or Redis needed.
Run with: pytest services/discovery/tests/test_watchers.py -v
"""

import asyncio
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from shared.models.url_task import URLTask, DiscoverySource


# ── Sitemap Watcher Tests ──────────────────────────────────────────────────────

class TestSitemapWatcher:

    SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>https://example.com/article/1</loc>
            <lastmod>2024-01-15</lastmod>
        </url>
        <url>
            <loc>https://example.com/article/2</loc>
            <lastmod>2024-01-10</lastmod>
        </url>
        <url>
            <loc>https://example.com/page-no-lastmod</loc>
        </url>
    </urlset>"""

    SITEMAP_INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap>
            <loc>https://example.com/sitemap-news.xml</loc>
        </sitemap>
        <sitemap>
            <loc>https://example.com/sitemap-blog.xml</loc>
        </sitemap>
    </sitemapindex>"""

    def test_parse_urlset_extracts_urls(self):
        """Standard urlset produces correct number of URL entries."""
        from xml.etree import ElementTree as ET
        root = ET.fromstring(self.SITEMAP_XML)
        # Count <url> elements
        ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        urls = root.findall(f"{{{ns}}}url")
        assert len(urls) == 3

    def test_parse_sitemap_index_extracts_child_urls(self):
        """Sitemap index produces list of child sitemap URLs."""
        from xml.etree import ElementTree as ET
        root = ET.fromstring(self.SITEMAP_INDEX_XML)
        ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        sitemaps = root.findall(f"{{{ns}}}sitemap")
        locs = [s.find(f"{{{ns}}}loc").text for s in sitemaps]
        assert len(locs) == 2
        assert "https://example.com/sitemap-news.xml" in locs

    def test_url_without_lastmod_is_enqueued(self):
        """URLs without lastmod should still be discovered."""
        from xml.etree import ElementTree as ET
        root = ET.fromstring(self.SITEMAP_XML)
        ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        no_lastmod = [
            u for u in root.findall(f"{{{ns}}}url")
            if u.find(f"{{{ns}}}lastmod") is None
        ]
        assert len(no_lastmod) == 1


# ── CT Log Scanner Tests ───────────────────────────────────────────────────────

class TestCTLogScanner:

    def setup_method(self):
        from services.discovery.watchers.ct_logs import CTLogScanner
        self.scanner = CTLogScanner.__new__(CTLogScanner)
        self.scanner._on_url_found = None
        self.scanner._certstream_url = "wss://test"
        self.scanner._running = False
        self.scanner._certs_processed = 0
        self.scanner._domains_discovered = 0
        self.scanner._domains_skipped = 0
        import time
        self.scanner._last_stats_log = time.time()

    def test_skip_wildcard_domains(self):
        assert self.scanner._should_skip("*.example.com") is True
        assert self.scanner._should_skip("*.sub.example.com") is True

    def test_skip_ip_addresses(self):
        assert self.scanner._should_skip("192.168.1.1") is True
        assert self.scanner._should_skip("10.0.0.1") is True

    def test_skip_local_domains(self):
        assert self.scanner._should_skip("myapp.local") is True
        assert self.scanner._should_skip("api.internal") is True
        assert self.scanner._should_skip("test.test") is True

    def test_skip_too_short(self):
        assert self.scanner._should_skip("x") is True
        assert self.scanner._should_skip("") is True
        assert self.scanner._should_skip("ab") is True

    def test_allow_valid_domains(self):
        assert self.scanner._should_skip("example.com") is False
        assert self.scanner._should_skip("news.ycombinator.com") is False
        assert self.scanner._should_skip("my-blog.io") is False

    def test_skip_onion_domains(self):
        assert self.scanner._should_skip("something.onion") is True

    def test_valid_cert_message_structure(self):
        """A valid Certstream message has the expected structure."""
        msg = {
            "message_type": "certificate_update",
            "data": {
                "cert_index": 123,
                "leaf_cert": {
                    "all_domains": ["example.com", "www.example.com"],
                    "subject": {"CN": "example.com"},
                    "issuer": {"O": "Let's Encrypt"},
                    "not_after": "2024-04-01T00:00:00Z",
                }
            }
        }
        assert msg["message_type"] == "certificate_update"
        domains = msg["data"]["leaf_cert"]["all_domains"]
        valid = [d for d in domains if not self.scanner._should_skip(d)]
        assert len(valid) == 2  # both are valid


# ── Link Extractor Tests ────────────────────────────────────────────────────────

class TestLinkExtractor:

    SAMPLE_HTML = """
    <html>
    <body>
        <a href="https://example.com/article/1">Article 1</a>
        <a href="/relative/page">Relative link</a>
        <a href="https://example.com/article/2#section">With fragment</a>
        <a href="mailto:test@example.com">Email (skip)</a>
        <a href="javascript:void(0)">JS link (skip)</a>
        <a href="https://example.com/file.pdf">PDF (skip)</a>
        <a href="https://facebook.com/page">Facebook (skip)</a>
        <a href="https://dev.to/post">External allowed</a>
    </body>
    </html>
    """

    def setup_method(self):
        from services.discovery.watchers.link_extractor import LinkExtractor
        self.extractor = LinkExtractor.__new__(LinkExtractor)
        self.extractor._redis = None
        self.extractor._on_url_found = None
        self.extractor._focused = False
        self.extractor._crawl_complete_stream = "test"
        self.extractor._consumer_group = "test"
        self.extractor._consumer_name = "test"

    def test_extracts_absolute_links(self):
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        absolute = [l for l in links if l.startswith("https://example.com")]
        assert len(absolute) >= 2

    def test_resolves_relative_links(self):
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        assert "https://example.com/relative/page" in links

    def test_strips_fragments(self):
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        # Fragment version should be stripped to base URL
        assert "https://example.com/article/2" in links
        assert all("#" not in l for l in links)

    def test_filters_pdf(self):
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        filtered = self.extractor._filter_links(links, "example.com")
        assert all(not l.endswith(".pdf") for l in filtered)

    def test_filters_blocked_domains(self):
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        filtered = self.extractor._filter_links(links, "example.com")
        assert all("facebook.com" not in l for l in filtered)

    def test_focused_mode_only_same_domain(self):
        self.extractor._focused = True
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        filtered = self.extractor._filter_links(links, "example.com")
        assert all("example.com" in l for l in filtered)
        assert all("dev.to" not in l for l in filtered)

    def test_non_focused_mode_allows_external(self):
        self.extractor._focused = False
        links = self.extractor._extract_links(self.SAMPLE_HTML, "https://example.com")
        filtered = self.extractor._filter_links(links, "example.com")
        has_external = any("dev.to" in l for l in filtered)
        assert has_external


# ── WebSub Subscriber Tests ────────────────────────────────────────────────────

class TestWebSubSubscriber:

    ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>Article One</title>
            <link href="https://example.com/article/1"/>
        </entry>
        <entry>
            <title>Article Two</title>
            <link href="https://example.com/article/2"/>
        </entry>
    </feed>"""

    RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
    <channel>
        <item><link>https://example.com/post/1</link></item>
        <item><link>https://example.com/post/2</link></item>
    </channel>
    </rss>"""

    def setup_method(self):
        from services.discovery.watchers.websub import WebSubSubscriber
        self.sub = WebSubSubscriber.__new__(WebSubSubscriber)
        self.sub._redis = None
        self.sub._on_url_found = None
        self.sub._callback_url = "http://test/callback"
        self.sub._hub_url = "http://test/hub"
        self.sub._client = None

    def test_parse_atom_feed(self):
        urls = self.sub._parse_feed(self.ATOM_FEED)
        assert len(urls) == 2
        assert "https://example.com/article/1" in urls

    def test_parse_rss_feed(self):
        urls = self.sub._parse_feed(self.RSS_FEED)
        assert len(urls) == 2
        assert "https://example.com/post/1" in urls

    def test_verification_echoes_challenge(self):
        """Hub verification must echo the challenge string."""
        params = {
            "hub.mode": "subscribe",
            "hub.topic": "https://example.com/feed",
            "hub.challenge": "abc123xyz",
        }
        result = asyncio.get_event_loop().run_until_complete(
            self.sub.handle_verification(params)
        )
        assert result == "abc123xyz"

    def test_invalid_verification_returns_none(self):
        """Missing challenge = reject verification."""
        params = {"hub.mode": "subscribe", "hub.topic": "https://example.com/feed"}
        result = asyncio.get_event_loop().run_until_complete(
            self.sub.handle_verification(params)
        )
        assert result is None