"""
tests/test_bloom_filter.py
===========================
Unit tests for the Bloom filter URL deduplication.

These tests use fakeredis to avoid needing a real Redis instance.
Run with: pytest tests/test_bloom_filter.py -v
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestBloomFilter:
    """Test Bloom filter deduplication logic."""

    def test_url_normalisation(self):
        """Test that URLs are normalised before being added."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
        from services.discovery.queue.bloom_filter import BloomFilter

        bf = BloomFilter.__new__(BloomFilter)

        # Trailing slash stripped
        assert bf._normalise("https://example.com/") == "https://example.com"
        assert bf._normalise("https://example.com/page/") == "https://example.com/page"

        # Fragment stripped
        assert bf._normalise("https://example.com/page#section") == "https://example.com/page"

        # Scheme and host lowercased
        assert bf._normalise("HTTPS://EXAMPLE.COM/page") == "https://example.com/page"

        # Query string preserved (different params = different page)
        n = bf._normalise("https://example.com/search?q=test")
        assert "q=test" in n

    def test_url_equivalence(self):
        """Test that equivalent URLs normalise to the same string."""
        from services.discovery.queue.bloom_filter import BloomFilter
        bf = BloomFilter.__new__(BloomFilter)

        # These should all normalise to the same value
        variants = [
            "https://example.com/page",
            "https://example.com/page/",
            "https://example.com/page#anchor",
        ]
        normalised = [bf._normalise(u) for u in variants]
        assert len(set(normalised)) == 1, f"Expected all same, got: {normalised}"


class TestURLNormalisation:
    """Additional URL normalisation edge cases."""

    def test_different_paths_are_distinct(self):
        from services.discovery.queue.bloom_filter import BloomFilter
        bf = BloomFilter.__new__(BloomFilter)

        assert bf._normalise("https://example.com/a") != bf._normalise("https://example.com/b")

    def test_different_queries_are_distinct(self):
        from services.discovery.queue.bloom_filter import BloomFilter
        bf = BloomFilter.__new__(BloomFilter)

        assert (
            bf._normalise("https://example.com/?page=1") !=
            bf._normalise("https://example.com/?page=2")
        )


class TestURLTask:
    """Test URLTask serialisation roundtrip."""

    def test_serialise_deserialise(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
        from shared.models.url_task import URLTask, DiscoverySource

        task = URLTask(
            url="https://example.com/article/1",
            source=DiscoverySource.SITEMAP,
            depth=2,
            domain_authority=0.7,
            metadata={"lastmod": "2024-01-01"},
        )

        json_str = task.to_json()
        restored = URLTask.from_json(json_str)

        assert restored.url == task.url
        assert restored.source == task.source
        assert restored.depth == task.depth
        assert abs(restored.domain_authority - task.domain_authority) < 0.001
        assert restored.metadata == task.metadata

    def test_priority_auto_computed(self):
        from shared.models.url_task import URLTask, DiscoverySource

        task_websub = URLTask(url="https://a.com/", source=DiscoverySource.WEBSUB)
        task_link   = URLTask(url="https://b.com/", source=DiscoverySource.LINK)
        task_manual = URLTask(url="https://c.com/", source=DiscoverySource.MANUAL)

        assert task_websub.priority > task_link.priority
        assert task_manual.priority > task_websub.priority

    def test_domain_authority_boosts_priority(self):
        from shared.models.url_task import URLTask, DiscoverySource

        low_da  = URLTask(url="https://a.com/", source=DiscoverySource.SITEMAP, domain_authority=0.1)
        high_da = URLTask(url="https://b.com/", source=DiscoverySource.SITEMAP, domain_authority=0.9)

        assert high_da.priority > low_da.priority

    def test_domain_auto_extracted(self):
        from shared.models.url_task import URLTask, DiscoverySource

        task = URLTask(url="https://news.example.com/article/42", source=DiscoverySource.LINK)
        assert task.domain == "news.example.com"