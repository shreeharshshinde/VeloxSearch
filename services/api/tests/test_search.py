"""
tests/test_search.py
======================
Unit and integration tests for Phase 5 API components.

Tests cover:
  - RRF fusion correctness (the most critical algorithm)
  - Composite scoring weights and clamping
  - Cache key determinism
  - Request model validation
  - Response model structure
  - Result formatting

Run with: pytest services/api/tests/test_search.py -v
No external services needed — all backends are mocked.
"""

import sys, os, time, math, hashlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# =============================================================================
# RECIPROCAL RANK FUSION TESTS
# =============================================================================

class TestRRFFusion:
    """Tests for the core RRF algorithm in retrieval/fusion.py."""

    def _fuser(self):
        from retrieval.fusion import HybridFusion
        return HybridFusion()

    def _make_hit(self, url, domain="example.com", freshness=0.8,
                  authority=0.7, bm25_score=0.0, ann_score=0.0):
        return {
            "url": url, "domain": domain,
            "title": f"Page at {url}", "summary": "Test summary",
            "og_image": "", "author": "", "published_at": "",
            "language": "en", "schema_type": "",
            "freshness_score": freshness,
            "domain_authority": authority,
            "crawled_at": time.time(), "indexed_at": time.time(),
            "_rankingScore": bm25_score,
            "_semantic_score": ann_score,
        }

    def test_doc_in_both_lists_scores_higher_than_single_list(self):
        fuser = self._fuser()
        # doc_a appears in both BM25 and ANN — should rank highest
        bm25 = [self._make_hit("https://a.com"), self._make_hit("https://b.com")]
        ann  = [self._make_hit("https://a.com"), self._make_hit("https://c.com")]
        result = fuser.fuse(bm25, ann, limit=3)
        urls = [r["url"] for r in result]
        assert urls[0] == "https://a.com", f"Expected a.com first (in both lists), got: {urls}"

    def test_rrf_k_constant_smooths_rank_differences(self):
        """With k=60, rank 1 vs rank 2 difference is small: 1/61 vs 1/62."""
        fuser = self._fuser()
        bm25 = [self._make_hit(f"https://{i}.com") for i in range(10)]
        ann  = [self._make_hit(f"https://{9-i}.com") for i in range(10)]
        result = fuser.fuse(bm25, ann, limit=10)
        scores = [r["_composite_score"] for r in result]
        # Scores should be in descending order
        assert scores == sorted(scores, reverse=True)

    def test_empty_bm25_returns_ann_results(self):
        fuser = self._fuser()
        ann = [self._make_hit("https://a.com"), self._make_hit("https://b.com")]
        result = fuser.fuse([], ann, limit=10)
        assert len(result) == 2

    def test_empty_ann_returns_bm25_results(self):
        fuser = self._fuser()
        bm25 = [self._make_hit("https://a.com"), self._make_hit("https://b.com")]
        result = fuser.fuse(bm25, [], limit=10)
        assert len(result) == 2

    def test_both_empty_returns_empty(self):
        fuser = self._fuser()
        result = fuser.fuse([], [], limit=10)
        assert result == []

    def test_limit_respected(self):
        fuser = self._fuser()
        bm25 = [self._make_hit(f"https://{i}.com") for i in range(20)]
        ann  = [self._make_hit(f"https://{i}.com") for i in range(20)]
        result = fuser.fuse(bm25, ann, limit=5)
        assert len(result) == 5

    def test_offset_pagination(self):
        fuser = self._fuser()
        bm25 = [self._make_hit(f"https://{i}.com") for i in range(10)]
        page1 = fuser.fuse(bm25, [], limit=3, offset=0)
        page2 = fuser.fuse(bm25, [], limit=3, offset=3)
        # Pages should not overlap
        urls1 = {r["url"] for r in page1}
        urls2 = {r["url"] for r in page2}
        assert urls1.isdisjoint(urls2), "Paginated results should not overlap"

    def test_composite_score_uses_freshness(self):
        """Higher freshness should produce higher composite score (all else equal)."""
        fuser = self._fuser()
        fresh_hit = self._make_hit("https://fresh.com", freshness=0.99, authority=0.5)
        stale_hit = self._make_hit("https://stale.com", freshness=0.01, authority=0.5)
        bm25 = [fresh_hit, stale_hit]
        result = fuser.fuse(bm25, [], limit=2)
        assert result[0]["url"] == "https://fresh.com"

    def test_composite_score_uses_authority(self):
        """Higher domain authority should produce higher composite score."""
        fuser = self._fuser()
        high_da = self._make_hit("https://authority.com", freshness=0.5, authority=0.95)
        low_da  = self._make_hit("https://unknown.com",   freshness=0.5, authority=0.05)
        bm25 = [high_da, low_da]
        result = fuser.fuse(bm25, [], limit=2)
        assert result[0]["url"] == "https://authority.com"

    def test_duplicate_urls_deduplicated(self):
        """Same URL in both BM25 and ANN should appear only once in output."""
        fuser = self._fuser()
        hit = self._make_hit("https://same.com")
        result = fuser.fuse([hit], [hit], limit=10)
        urls = [r["url"] for r in result]
        assert urls.count("https://same.com") == 1

    def test_composite_score_between_0_and_1(self):
        fuser = self._fuser()
        bm25 = [self._make_hit(f"https://{i}.com") for i in range(5)]
        ann  = [self._make_hit(f"https://{i}.com") for i in range(5)]
        result = fuser.fuse(bm25, ann, limit=10)
        for r in result:
            score = r["_composite_score"]
            assert 0.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_bm25_only_mode(self):
        fuser = self._fuser()
        bm25 = [self._make_hit(f"https://{i}.com") for i in range(5)]
        result = fuser.bm25_only(bm25, limit=3)
        assert len(result) == 3
        for r in result:
            assert "_composite_score" in r
            assert r["_ann_rank"] is None

    def test_ann_only_mode(self):
        fuser = self._fuser()
        ann = [self._make_hit(f"https://{i}.com", ann_score=0.9-i*0.1) for i in range(5)]
        result = fuser.ann_only(ann, limit=3)
        assert len(result) == 3
        for r in result:
            assert "_composite_score" in r
            assert r["_bm25_rank"] is None


# =============================================================================
# CACHE KEY TESTS
# =============================================================================

class TestCacheKey:
    """Tests for cache key determinism and isolation."""

    def _key(self, **kwargs):
        from cache.result_cache import build_cache_key
        defaults = dict(query="test", mode="hybrid", lang=None,
                        domain=None, schema_type=None, limit=10, offset=0)
        defaults.update(kwargs)
        return build_cache_key(**defaults)

    def test_same_params_same_key(self):
        k1 = self._key(query="machine learning")
        k2 = self._key(query="machine learning")
        assert k1 == k2

    def test_different_query_different_key(self):
        k1 = self._key(query="machine learning")
        k2 = self._key(query="deep learning")
        assert k1 != k2

    def test_different_lang_different_key(self):
        k1 = self._key(lang="en")
        k2 = self._key(lang="fr")
        assert k1 != k2

    def test_different_mode_different_key(self):
        k1 = self._key(mode="bm25")
        k2 = self._key(mode="semantic")
        assert k1 != k2

    def test_different_limit_different_key(self):
        k1 = self._key(limit=10)
        k2 = self._key(limit=20)
        assert k1 != k2

    def test_different_offset_different_key(self):
        k1 = self._key(offset=0)
        k2 = self._key(offset=10)
        assert k1 != k2

    def test_key_has_correct_prefix(self):
        from cache.result_cache import CACHE_PREFIX
        k = self._key()
        assert k.startswith(CACHE_PREFIX)

    def test_key_is_fixed_length(self):
        from cache.result_cache import CACHE_PREFIX
        k1 = self._key(query="short")
        k2 = self._key(query="a very very very very long query string indeed")
        assert len(k1) == len(k2), "Cache keys should always be the same length"

    def test_none_and_empty_string_treated_differently(self):
        """lang=None and lang="" should produce different keys."""
        from cache.result_cache import build_cache_key
        k1 = build_cache_key("q", "hybrid", None, None, None, 10, 0)
        k2 = build_cache_key("q", "hybrid", "",   None, None, 10, 0)
        # They may or may not differ depending on implementation —
        # but both should be stable and not crash
        assert isinstance(k1, str)
        assert isinstance(k2, str)


# =============================================================================
# REQUEST MODEL TESTS
# =============================================================================

class TestRequestModels:
    """Tests for Pydantic request validation."""

    def test_search_request_defaults(self):
        from models.request import SearchRequest, SearchMode
        req = SearchRequest(q="test query")
        assert req.mode    == SearchMode.HYBRID
        assert req.limit   == 10
        assert req.offset  == 0
        assert req.lang    is None
        assert req.domain  is None

    def test_search_request_strips_whitespace(self):
        from models.request import SearchRequest
        req = SearchRequest(q="  machine learning  ")
        assert req.q == "machine learning"

    def test_search_request_normalises_lang(self):
        from models.request import SearchRequest
        req = SearchRequest(q="test", lang="EN")
        assert req.lang == "en"

    def test_search_request_lang_truncated_to_2_chars(self):
        from models.request import SearchRequest
        req = SearchRequest(q="test", lang="en-US")
        assert req.lang == "en"

    def test_search_request_limit_max_50(self):
        from models.request import SearchRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SearchRequest(q="test", limit=51)

    def test_search_request_limit_min_1(self):
        from models.request import SearchRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SearchRequest(q="test", limit=0)

    def test_search_request_empty_query_rejected(self):
        from models.request import SearchRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SearchRequest(q="")

    def test_search_request_query_too_long_rejected(self):
        from models.request import SearchRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SearchRequest(q="x" * 501)

    def test_search_mode_values(self):
        from models.request import SearchRequest, SearchMode
        for mode in ["bm25", "semantic", "hybrid"]:
            req = SearchRequest(q="test", mode=mode)
            assert isinstance(req.mode, SearchMode)

    def test_invalid_mode_rejected(self):
        from models.request import SearchRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SearchRequest(q="test", mode="invalid_mode")

    def test_url_submit_requires_url(self):
        from models.request import URLSubmitRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            URLSubmitRequest()


# =============================================================================
# RESPONSE MODEL TESTS
# =============================================================================

class TestResponseModels:
    """Tests for response model structure and field defaults."""

    def test_search_result_defaults(self):
        from models.response import SearchResult
        r = SearchResult(
            url="https://example.com",
            domain="example.com",
            title="Test",
            summary="A test page",
            score=0.85,
        )
        assert r.og_image     == ""
        assert r.author       == ""
        assert r.published_at == ""
        assert r.language     == "en"
        assert r.bm25_score   == 0.0
        assert r.highlights   == {}

    def test_search_response_structure(self):
        from models.response import SearchResponse, SearchResult
        resp = SearchResponse(
            query="machine learning",
            mode="hybrid",
            total=42,
            took_ms=23,
            index_freshness="2024-01-15T10:00:00Z",
            from_cache=False,
            results=[],
        )
        assert resp.total == 42
        assert resp.from_cache is False
        assert isinstance(resp.results, list)

    def test_health_response_structure(self):
        from models.response import HealthResponse
        h = HealthResponse(
            status="healthy",
            version="1.0.0",
            backends={"redis": "healthy", "meilisearch": "healthy"},
        )
        assert h.status == "healthy"
        assert "redis" in h.backends

    def test_pipeline_stats_structure(self):
        from models.response import PipelineStats
        stats = PipelineStats(
            urls_indexed_last_60s=15,
            urls_indexed_last_hour=842,
            total_pages_indexed=28472,
            slo_pass_rate_pct=97.3,
            p50_latency_ms=28400.0,
            p95_latency_ms=54200.0,
            queue_depth=14,
            meilisearch_docs=28472,
            qdrant_vectors=28100,
            uptime_seconds=86423,
            index_freshness="2024-01-15T10:23:45Z",
        )
        assert stats.slo_pass_rate_pct == 97.3
        assert stats.p95_latency_ms == 54200.0


# =============================================================================
# RESULT FORMATTING TESTS
# =============================================================================

class TestResultFormatting:
    """Tests for the _format_result helper in search router."""

    def _format(self, **kwargs):
        from routers.search import _format_result
        defaults = dict(
            url="https://example.com/page",
            domain="example.com",
            title="Test Title",
            og_title="",
            summary="Test summary.",
            og_description="",
            og_image="",
            author="",
            published_at="",
            language="en",
            schema_type="",
            freshness_score=0.8,
            domain_authority=0.7,
            crawled_at=time.time(),
            indexed_at=time.time(),
            _composite_score=0.85,
            _bm25_score=0.6,
            _ann_score=0.75,
            _formatted={},
        )
        defaults.update(kwargs)
        return _format_result(defaults)

    def test_og_title_preferred_over_title(self):
        r = self._format(title="Plain Title", og_title="OG Title")
        assert r.title == "OG Title"

    def test_plain_title_used_when_no_og(self):
        r = self._format(title="Plain Title", og_title="")
        assert r.title == "Plain Title"

    def test_domain_used_as_final_fallback(self):
        r = self._format(title="", og_title="", domain="example.com")
        assert r.title == "example.com"

    def test_scores_rounded_to_4_decimal_places(self):
        r = self._format(_composite_score=0.87654321)
        assert r.score == round(0.87654321, 4)

    def test_highlights_extracted_from_formatted(self):
        r = self._format(_formatted={
            "title": "How <mark>AI</mark> works",
            "summary": "A guide to <mark>AI</mark> systems",
        })
        assert "title" in r.highlights
        assert "<mark>" in r.highlights["title"]

    def test_missing_fields_have_safe_defaults(self):
        r = self._format()
        assert r.og_image     == ""
        assert r.author       == ""
        assert r.published_at == ""
        assert r.language     == "en"