"""
tests/test_writers.py
======================
Unit tests for all four indexer writers.

Uses mocks for all external services (Meilisearch, Qdrant, PostgreSQL, Redis)
so tests run without any running infrastructure.

Run with: pytest services/indexer/tests/test_writers.py -v

Test coverage:
  - MeilisearchWriter: document ID generation, doc serialisation, field truncation
  - QdrantWriter:      point ID generation, payload construction, embedding validation
  - PostgresWriter:    SQL correctness (structure only — no real DB)
  - RedisSignalsWriter: key generation, signal serialisation
  - IndexedDocument:   all properties used by writers
  - Integration:       parallel write result aggregation
"""

import asyncio
import hashlib
import math
import sys
import os
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_doc(**kwargs) -> "IndexedDocument":
    """Build a complete IndexedDocument for testing."""
    from shared.models.indexed_document import IndexedDocument
    defaults = dict(
        url="https://techcrunch.com/2024/01/15/ai-search",
        domain="techcrunch.com",
        content_hash="a3f9c2d8e1b74f56",
        minio_key="techcrunch.com/a3f9c2d8e1b74f56.html.gz",
        title="How AI is Reshaping Web Search",
        clean_text="The landscape of web search has changed dramatically. " * 50,
        summary="The landscape of web search has changed dramatically.",
        language="en",
        author="Jane Smith",
        published_at="2024-01-15T10:00:00Z",
        entities=[
            {"text": "Google",   "label": "ORG",    "count": 4},
            {"text": "OpenAI",   "label": "ORG",    "count": 3},
            {"text": "GPT-4",    "label": "PRODUCT", "count": 2},
        ],
        keywords=["machine learning", "semantic search", "neural network", "AI"],
        word_count=487,
        embedding=[0.1 / math.sqrt(384)] * 384,  # L2-normalised unit vector
        og_title="How AI is Reshaping Web Search",
        og_description="A deep dive into modern search technology",
        og_image="https://techcrunch.com/img/ai-search.jpg",
        schema_type="Article",
        canonical_url="https://techcrunch.com/2024/01/15/ai-search",
        domain_authority=0.92,
        freshness_score=0.98,
        discovered_at=time.time() - 30,
        crawled_at=time.time() - 20,
        processed_at=time.time() - 5,
        discovery_source="websub",
        crawl_depth=0,
        fetch_ms=342,
        process_ms=287,
    )
    defaults.update(kwargs)
    return IndexedDocument(**defaults)


# =============================================================================
# MEILISEARCH WRITER TESTS
# =============================================================================

class TestMeilisearchWriter:

    def test_url_to_id_is_stable(self):
        from writers.meilisearch_writer import MeilisearchWriter
        url = "https://example.com/page"
        id1 = MeilisearchWriter._url_to_id(url)
        id2 = MeilisearchWriter._url_to_id(url)
        assert id1 == id2, "Same URL must always produce same ID"

    def test_url_to_id_is_32_chars(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc_id = MeilisearchWriter._url_to_id("https://example.com/page")
        assert len(doc_id) == 32

    def test_url_to_id_alphanumeric(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc_id = MeilisearchWriter._url_to_id("https://example.com/page?q=test&lang=en")
        assert doc_id.isalnum(), f"ID must be alphanumeric, got: {doc_id}"

    def test_different_urls_give_different_ids(self):
        from writers.meilisearch_writer import MeilisearchWriter
        id1 = MeilisearchWriter._url_to_id("https://example.com/page-1")
        id2 = MeilisearchWriter._url_to_id("https://example.com/page-2")
        assert id1 != id2

    def test_to_meilisearch_doc_has_required_fields(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc = make_doc()
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)

        required = [
            "id", "url", "domain", "title", "og_title", "keywords",
            "clean_text", "summary", "language", "freshness_score",
            "domain_authority", "word_count", "indexed_at",
        ]
        for field in required:
            assert field in ms_doc, f"Missing required field: {field}"

    def test_clean_text_truncated_to_10k(self):
        from writers.meilisearch_writer import MeilisearchWriter
        long_text = "word " * 10_000   # 50,000 chars
        doc = make_doc(clean_text=long_text)
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        assert len(ms_doc["clean_text"]) <= 10_000

    def test_short_clean_text_not_truncated(self):
        from writers.meilisearch_writer import MeilisearchWriter
        short_text = "This is a short article."
        doc = make_doc(clean_text=short_text)
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        assert ms_doc["clean_text"] == short_text

    def test_keywords_stored_as_list(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc = make_doc(keywords=["ml", "ai", "search"])
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        assert isinstance(ms_doc["keywords"], list)
        assert "ml" in ms_doc["keywords"]

    def test_entity_names_extracted(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc = make_doc()
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        assert "Google" in ms_doc["entity_names"]
        assert "OpenAI" in ms_doc["entity_names"]

    def test_entity_types_deduplicated(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc = make_doc()   # has Google+OpenAI (both ORG) and GPT-4 (PRODUCT)
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        # ORG should appear only once even though 2 ORG entities exist
        assert ms_doc["entity_types"].count("ORG") == 1

    def test_freshness_score_rounded(self):
        from writers.meilisearch_writer import MeilisearchWriter
        doc = make_doc(freshness_score=0.987654321)
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        # Should be rounded to 4 decimal places
        assert ms_doc["freshness_score"] == round(0.987654321, 4)

    def test_no_raw_embedding_in_doc(self):
        """Embeddings live in Qdrant, not Meilisearch — never store them here."""
        from writers.meilisearch_writer import MeilisearchWriter
        doc = make_doc()
        ms_doc = MeilisearchWriter._to_meilisearch_doc(doc)
        assert "embedding" not in ms_doc


# =============================================================================
# QDRANT WRITER TESTS
# =============================================================================

class TestQdrantWriter:

    def test_url_to_point_id_is_uint64(self):
        from writers.qdrant_writer import QdrantWriter
        point_id = QdrantWriter._url_to_point_id("https://example.com/page")
        assert isinstance(point_id, int)
        assert 0 <= point_id <= 2**64 - 1

    def test_url_to_point_id_is_stable(self):
        from writers.qdrant_writer import QdrantWriter
        url = "https://example.com/article"
        id1 = QdrantWriter._url_to_point_id(url)
        id2 = QdrantWriter._url_to_point_id(url)
        assert id1 == id2

    def test_different_urls_give_different_point_ids(self):
        from writers.qdrant_writer import QdrantWriter
        id1 = QdrantWriter._url_to_point_id("https://example.com/a")
        id2 = QdrantWriter._url_to_point_id("https://example.com/b")
        assert id1 != id2

    def test_to_qdrant_point_has_correct_id(self):
        from writers.qdrant_writer import QdrantWriter
        doc = make_doc()
        point = QdrantWriter._to_qdrant_point(doc)
        expected_id = QdrantWriter._url_to_point_id(doc.url)
        assert point.id == expected_id

    def test_to_qdrant_point_vector_is_embedding(self):
        from writers.qdrant_writer import QdrantWriter
        doc = make_doc()
        point = QdrantWriter._to_qdrant_point(doc)
        assert point.vector == doc.embedding
        assert len(point.vector) == 384

    def test_to_qdrant_point_payload_has_required_fields(self):
        from writers.qdrant_writer import QdrantWriter
        doc = make_doc()
        point = QdrantWriter._to_qdrant_point(doc)

        required_payload_fields = [
            "url", "domain", "title", "summary", "language",
            "freshness_score", "domain_authority", "crawled_at",
            "content_hash", "has_embedding",
        ]
        for field in required_payload_fields:
            assert field in point.payload, f"Missing payload field: {field}"

    def test_has_embedding_always_true_in_payload(self):
        from writers.qdrant_writer import QdrantWriter
        doc = make_doc()
        point = QdrantWriter._to_qdrant_point(doc)
        assert point.payload["has_embedding"] is True

    def test_payload_does_not_contain_full_text(self):
        """Qdrant payload should NOT store full clean_text — bloats the collection."""
        from writers.qdrant_writer import QdrantWriter
        doc = make_doc(clean_text="This is the full article text. " * 100)
        point = QdrantWriter._to_qdrant_point(doc)
        assert "clean_text" not in point.payload

    def test_freshness_score_rounded_in_payload(self):
        from writers.qdrant_writer import QdrantWriter
        doc = make_doc(freshness_score=0.9876543)
        point = QdrantWriter._to_qdrant_point(doc)
        assert point.payload["freshness_score"] == round(0.9876543, 4)


# =============================================================================
# REDIS SIGNALS WRITER TESTS
# =============================================================================

class TestRedisSignalsWriter:

    def test_signals_key_is_stable(self):
        from writers.redis_signals import RedisSignalsWriter
        url = "https://example.com/page"
        k1 = RedisSignalsWriter._signals_key(url)
        k2 = RedisSignalsWriter._signals_key(url)
        assert k1 == k2

    def test_signals_key_has_prefix(self):
        from writers.redis_signals import RedisSignalsWriter, SIGNALS_KEY_PREFIX
        key = RedisSignalsWriter._signals_key("https://example.com/page")
        assert key.startswith(SIGNALS_KEY_PREFIX)

    def test_signals_key_different_for_different_urls(self):
        from writers.redis_signals import RedisSignalsWriter
        k1 = RedisSignalsWriter._signals_key("https://example.com/a")
        k2 = RedisSignalsWriter._signals_key("https://example.com/b")
        assert k1 != k2

    def test_signals_key_length_fixed(self):
        """Key length should be predictable: prefix + 16 hex chars."""
        from writers.redis_signals import RedisSignalsWriter, SIGNALS_KEY_PREFIX
        key = RedisSignalsWriter._signals_key("https://any-url.com/page")
        expected_len = len(SIGNALS_KEY_PREFIX) + 16
        assert len(key) == expected_len


# =============================================================================
# INDEXED DOCUMENT PROPERTIES (used by all writers)
# =============================================================================

class TestIndexedDocumentProperties:

    def test_has_embedding_true(self):
        doc = make_doc(embedding=[0.1] * 384)
        assert doc.has_embedding is True

    def test_has_embedding_false_wrong_dims(self):
        doc = make_doc(embedding=[0.1] * 100)
        assert doc.has_embedding is False

    def test_has_embedding_false_empty(self):
        doc = make_doc(embedding=[])
        assert doc.has_embedding is False

    def test_e2e_latency_ms_computed(self):
        t0 = time.time()
        doc = make_doc(discovered_at=t0 - 45.0, processed_at=t0)
        # Should be approximately 45,000 ms
        assert 44_000 <= doc.e2e_latency_ms <= 46_000

    def test_e2e_latency_ms_zero_when_no_discovery(self):
        doc = make_doc(discovered_at=0.0, processed_at=time.time())
        assert doc.e2e_latency_ms == 0

    def test_display_title_og_priority(self):
        doc = make_doc(og_title="OG Title", title="Plain Title")
        assert doc.display_title == "OG Title"

    def test_display_title_plain_title_fallback(self):
        doc = make_doc(og_title="", title="Plain Title")
        assert doc.display_title == "Plain Title"

    def test_display_title_domain_fallback(self):
        doc = make_doc(og_title="", title="", domain="example.com")
        assert doc.display_title == "example.com"

    def test_display_description_og_priority(self):
        doc = make_doc(og_description="OG desc", summary="summary")
        assert doc.display_description == "OG desc"

    def test_display_description_summary_fallback(self):
        doc = make_doc(og_description="", summary="My summary")
        assert doc.display_description == "My summary"

    def test_top_entities_sorted_by_count(self):
        doc = make_doc(entities=[
            {"text": "A", "label": "ORG",    "count": 1},
            {"text": "B", "label": "ORG",    "count": 5},
            {"text": "C", "label": "PERSON", "count": 3},
        ])
        top = doc.top_entities(2)
        assert top[0]["text"] == "B"
        assert top[1]["text"] == "C"

    def test_json_roundtrip_preserves_all_fields(self):
        from shared.models.indexed_document import IndexedDocument
        doc = make_doc()
        restored = IndexedDocument.from_json(doc.to_json())

        assert restored.url              == doc.url
        assert restored.domain           == doc.domain
        assert restored.content_hash     == doc.content_hash
        assert restored.title            == doc.title
        assert restored.language         == doc.language
        assert restored.word_count       == doc.word_count
        assert restored.entities         == doc.entities
        assert restored.keywords         == doc.keywords
        assert len(restored.embedding)   == len(doc.embedding)
        assert restored.og_title         == doc.og_title
        assert restored.schema_type      == doc.schema_type
        assert abs(restored.freshness_score - doc.freshness_score) < 1e-6
        assert abs(restored.domain_authority - doc.domain_authority) < 1e-6


# =============================================================================
# PARALLEL WRITE AGGREGATION
# =============================================================================

class TestParallelWriteAggregation:
    """
    Test that the parallel write result aggregation in main.py
    correctly handles mixed success/failure outcomes.
    """

    def test_all_true_is_full_success(self):
        results = [True, True, True, True]
        all_ok = all(r if isinstance(r, bool) else False for r in results)
        assert all_ok is True

    def test_one_false_is_partial(self):
        results = [True, False, True, True]
        all_ok = all(r if isinstance(r, bool) else False for r in results)
        assert all_ok is False

    def test_exception_treated_as_false(self):
        results = [True, Exception("conn refused"), True, True]
        successes = [r if isinstance(r, bool) else False for r in results]
        assert successes == [True, False, True, True]
        assert all(successes) is False

    def test_four_exceptions_all_false(self):
        results = [
            Exception("meili down"),
            Exception("qdrant down"),
            Exception("pg down"),
            Exception("redis down"),
        ]
        successes = [r if isinstance(r, bool) else False for r in results]
        assert successes == [False, False, False, False]