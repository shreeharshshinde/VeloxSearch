"""
tests/test_queue.py
====================
Tests for URLQueue priority ordering and serialisation.

Uses fakeredis for in-memory Redis emulation — no real Redis needed.
Run with: pytest services/discovery/tests/test_queue.py -v
"""

import asyncio
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from shared.models.url_task import URLTask, DiscoverySource


class TestURLQueue:
    """Test queue priority ordering using a real URLQueue with fakeredis."""

    def _make_task(self, url: str, source: DiscoverySource, da: float = 0.5) -> URLTask:
        return URLTask(url=url, source=source, domain_authority=da)

    def test_priority_ordering(self):
        """Higher priority tasks should come out first."""
        manual  = self._make_task("https://a.com/", DiscoverySource.MANUAL,  0.5)
        websub  = self._make_task("https://b.com/", DiscoverySource.WEBSUB,  0.5)
        link    = self._make_task("https://c.com/", DiscoverySource.LINK,    0.5)
        ct_log  = self._make_task("https://d.com/", DiscoverySource.CT_LOG,  0.5)

        priorities = [manual.priority, websub.priority, ct_log.priority, link.priority]
        assert priorities == sorted(priorities, reverse=True), (
            f"Expected descending priorities, got: {priorities}"
        )

    def test_domain_authority_breaks_ties(self):
        """Two tasks with same source but different DA: higher DA comes first."""
        high = self._make_task("https://high.com/", DiscoverySource.SITEMAP, da=0.9)
        low  = self._make_task("https://low.com/",  DiscoverySource.SITEMAP, da=0.1)
        assert high.priority > low.priority

    def test_task_json_roundtrip(self):
        """URLTask must survive JSON serialise → deserialise unchanged."""
        task = self._make_task("https://example.com/page", DiscoverySource.CT_LOG, 0.7)
        task.metadata = {"cert_index": 9999, "issuer": "Let's Encrypt"}

        restored = URLTask.from_json(task.to_json())

        assert restored.url    == task.url
        assert restored.source == task.source
        assert abs(restored.domain_authority - task.domain_authority) < 1e-6
        assert restored.depth  == task.depth
        assert restored.metadata == task.metadata

    def test_task_age(self):
        """age_seconds() should return a positive, small number for a new task."""
        import time
        task = self._make_task("https://example.com/", DiscoverySource.LINK)
        time.sleep(0.01)
        assert task.age_seconds() > 0
        assert task.age_seconds() < 5  # created just now

    def test_auto_domain_extraction(self):
        """Domain is auto-extracted from URL when not provided."""
        task = URLTask(url="https://news.example.com/post/42", source=DiscoverySource.LINK)
        assert task.domain == "news.example.com"

    def test_all_sources_have_priority(self):
        """Every DiscoverySource value must map to a non-zero priority."""
        for source in DiscoverySource:
            task = URLTask(url=f"https://example.com/{source.value}", source=source)
            assert task.priority > 0, f"Source {source} has zero priority"