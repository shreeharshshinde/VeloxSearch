"""
tests/test_playwright.py
=========================
Unit tests for the Python crawler components (page classifier + Playwright).
No real browser or network needed — uses mocks.

Run with: pytest services/crawler/tests/test_playwright.py -v
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

# ── Page Classifier Tests ─────────────────────────────────────────────────────

class TestPageClassifier:
    """Test the page classifier that decides HTTP vs Playwright."""

    def _classify(self, url, html, content_type="text/html", headers=None):
        from services.crawler.playwright_crawler.page_classifier import classify
        return classify(url, html, content_type=content_type, response_headers=headers or {})

    # ── Static pages (should NOT need Playwright) ──────────────────────────────

    def test_plain_html_page_no_playwright(self):
        html = """
        <html>
        <head><title>My Blog</title></head>
        <body>
            <h1>Welcome to my blog</h1>
            <p>This is a static HTML page with lots of content that renders fine.</p>
            <p>No JavaScript required to see this text.</p>
        </body>
        </html>
        """
        result = self._classify("https://example.com/blog", html)
        assert result.needs_playwright is False, f"Should not need Playwright: {result.reason}"

    def test_json_content_type_no_playwright(self):
        result = self._classify(
            "https://api.example.com/data",
            '{"key": "value"}',
            content_type="application/json"
        )
        assert result.needs_playwright is False
        assert "json" in result.reason.lower()

    def test_pdf_content_type_no_playwright(self):
        result = self._classify(
            "https://example.com/file.pdf",
            b"%PDF-1.4",
            content_type="application/pdf"
        )
        assert result.needs_playwright is False

    # ── SPA pages (SHOULD need Playwright) ────────────────────────────────────

    def test_nextjs_data_attribute_needs_playwright(self):
        html = """
        <html>
        <head></head>
        <body>
            <div id="__NEXT_DATA__" type="application/json">{"props":{}}</div>
            <div id="__next"><div></div></div>
        </body>
        </html>
        """
        result = self._classify("https://example.com/", html)
        assert result.needs_playwright is True

    def test_react_root_empty_div_needs_playwright(self):
        html = """<html><body><div id="root"></div><script src="main.js"></script></body></html>"""
        result = self._classify("https://app.example.com", html)
        assert result.needs_playwright is True

    def test_vue_app_empty_div_needs_playwright(self):
        html = """<html><body><div id="app"></div></body></html>"""
        result = self._classify("https://example.com", html)
        assert result.needs_playwright is True

    def test_angular_ng_version_needs_playwright(self):
        html = """<html><body><app-root ng-version="17.0.0"></app-root></body></html>"""
        result = self._classify("https://example.com", html)
        assert result.needs_playwright is True

    def test_gatsby_fingerprint_needs_playwright(self):
        html = """<html><body><div id="gatsby-focus-wrapper"><div></div></div></body></html>"""
        result = self._classify("https://example.com", html)
        assert result.needs_playwright is True

    def test_nextjs_header_needs_playwright(self):
        result = self._classify(
            "https://example.com",
            "<html><body><p>content</p></body></html>",
            headers={"X-Powered-By": "Next.js"}
        )
        assert result.needs_playwright is True
        assert "Next.js" in result.reason

    def test_known_spa_domain_needs_playwright(self):
        result = self._classify("https://notion.so/workspace", "<html></html>")
        assert result.needs_playwright is True
        assert "notion.so" in result.reason

    def test_empty_shell_heuristic_needs_playwright(self):
        # Large HTML file but almost no visible text = loading shell
        big_html_no_content = "<html><head>" + "<script src='x.js'></script>" * 50 + "</head><body><div></div></body></html>"
        result = self._classify("https://app.example.com", big_html_no_content)
        assert result.needs_playwright is True

    # ── Nuxt ──────────────────────────────────────────────────────────────────

    def test_nuxt_needs_playwright(self):
        html = """<html><body><div id="app"></div><script>window.__NUXT__={}</script></body></html>"""
        result = self._classify("https://example.com", html)
        assert result.needs_playwright is True

    # ── Confidence values ─────────────────────────────────────────────────────

    def test_confidence_is_between_0_and_1(self):
        from services.crawler.playwright_crawler.page_classifier import classify
        for html in [
            "<html><body><p>static</p></body></html>",
            "<html><body><div id='root'></div></body></html>",
            "",
        ]:
            result = classify("https://example.com", html)
            assert 0.0 <= result.confidence <= 1.0, f"confidence out of range: {result.confidence}"

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_html_no_playwright(self):
        result = self._classify("https://example.com", "")
        assert result.needs_playwright is False

    def test_reason_is_non_empty_string(self):
        result = self._classify("https://example.com", "<html><body>test</body></html>")
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0


# ── CrawledPage model tests ────────────────────────────────────────────────────

class TestCrawledPage:
    """Test CrawledPage dataclass serialisation."""

    def test_json_roundtrip(self):
        from shared.models.crawled_page import CrawledPage, CrawlStatus, FetcherType
        import time

        page = CrawledPage(
            url="https://example.com/page",
            original_url="https://example.com/page",
            domain="example.com",
            status=CrawlStatus.SUCCESS,
            fetcher_type=FetcherType.HTTP,
            http_status=200,
            content_type="text/html; charset=utf-8",
            content_hash="a3f9c2d8e1b74f56",
            minio_key="example.com/a3f9c2d8e1b74f56.html.gz",
            fetch_bytes=12847,
            fetch_ms=342,
            crawled_at=time.time(),
            discovered_at=time.time() - 5.0,
            depth=1,
            discovery_source="sitemap",
        )

        restored = CrawledPage.from_json(page.to_json())

        assert restored.url == page.url
        assert restored.status == CrawlStatus.SUCCESS
        assert restored.fetcher_type == FetcherType.HTTP
        assert restored.http_status == page.http_status
        assert restored.content_hash == page.content_hash
        assert restored.minio_key == page.minio_key

    def test_is_success_property(self):
        from shared.models.crawled_page import CrawledPage, CrawlStatus, FetcherType

        success = CrawledPage(
            url="https://a.com", original_url="https://a.com", domain="a.com",
            status=CrawlStatus.SUCCESS, fetcher_type=FetcherType.HTTP,
        )
        assert success.is_success is True

        failed = CrawledPage(
            url="https://b.com", original_url="https://b.com", domain="b.com",
            status=CrawlStatus.HTTP_ERROR, fetcher_type=FetcherType.HTTP,
        )
        assert failed.is_success is False

    def test_total_latency_ms(self):
        from shared.models.crawled_page import CrawledPage, CrawlStatus, FetcherType

        page = CrawledPage(
            url="https://a.com", original_url="https://a.com", domain="a.com",
            status=CrawlStatus.SUCCESS, fetcher_type=FetcherType.HTTP,
            crawled_at=1000.5,
            discovered_at=1000.0,
        )
        assert page.total_latency_ms == 500  # 0.5s = 500ms