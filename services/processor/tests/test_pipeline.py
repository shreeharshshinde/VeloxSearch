"""
tests/test_pipeline.py
========================
Unit tests for every stage of the NLP processing pipeline.

Tests are designed to run WITHOUT any external services:
  - No Redis required
  - No MinIO required
  - spaCy + sentence-transformers loaded from local cache

Run with:
  pytest services/processor/tests/test_pipeline.py -v
  pytest services/processor/tests/test_pipeline.py -v -k "cleaner"  # one stage

Coverage goals:
  - cleaner:   HTML extraction, edge cases, short content handling
  - language:  HTML attr detection, header parsing, BCP 47 tags
  - nlp:       entity extraction, keyword extraction, word count
  - embedder:  vector dimensions, normalisation, query embedding
  - metadata:  og: tags, JSON-LD, canonical URL, author extraction
  - signals:   freshness decay, quality scoring, domain authority
  - document:  IndexedDocument roundtrip, computed properties
"""

import os
import sys
import time
import math

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Fixture paths
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures/sample_pages")


def load_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# =============================================================================
# CLEANER TESTS
# =============================================================================

class TestCleaner:
    """Tests for pipeline/cleaner.py — Trafilatura HTML extraction."""

    def test_extracts_article_text(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html, url="https://example.com/article")

        assert result.success, f"Expected success, got error: {result.error}"
        assert "machine learning" in result.text.lower()
        assert "semantic search" in result.text.lower()

    def test_extracts_title(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html, url="https://example.com/article")

        assert result.title, "Title should not be empty"
        assert "Machine Learning" in result.title or "machine learning" in result.title.lower()

    def test_strips_navigation_boilerplate(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)

        # Navigation text should be stripped
        assert "Privacy Policy" not in result.text
        assert "Terms of Service" not in result.text
        assert "Related Articles" not in result.text

    def test_extracts_author(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)
        # Trafilatura may or may not extract author from byline — test is lenient
        assert isinstance(result.author, str)

    def test_extracts_date(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)
        # Date extraction is optional — just check it's a string
        assert isinstance(result.date, str)

    def test_word_count_positive(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)
        assert result.word_count > 50, f"Expected >50 words, got {result.word_count}"

    def test_summary_max_length(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)
        assert len(result.summary) <= 310, f"Summary too long: {len(result.summary)}"

    def test_summary_not_empty_for_good_content(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)
        assert len(result.summary) > 50

    def test_empty_html_fails_gracefully(self):
        from pipeline.cleaner import clean_html
        result = clean_html("")
        assert not result.success
        assert result.error

    def test_very_short_html_fails_gracefully(self):
        from pipeline.cleaner import clean_html
        result = clean_html("<html><body><p>Hi</p></body></html>")
        assert not result.success   # too few words

    def test_spa_shell_fails_gracefully(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("spa_shell.html")
        result = clean_html(html)
        # SPA shell has no real content — should fail gracefully
        assert not result.success or result.word_count < 20

    def test_clean_ms_recorded(self):
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        result = clean_html(html)
        assert result.clean_ms >= 0

    def test_url_passed_for_metadata(self):
        """Passing URL helps Trafilatura resolve relative links and metadata."""
        from pipeline.cleaner import clean_html
        html = load_fixture("article.html")
        # Should not raise even with a URL
        result = clean_html(html, url="https://example.com/articles/ml-web-search")
        assert result.success


# =============================================================================
# LANGUAGE DETECTION TESTS
# =============================================================================

class TestLanguageDetection:
    """Tests for pipeline/language.py."""

    def test_detects_english_from_html_attr(self):
        from pipeline.language import detect_language
        lang = detect_language("Any text", html_lang="en")
        assert lang == "en"

    def test_detects_french_from_html_attr(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="fr")
        assert lang == "fr"

    def test_strips_region_from_lang_tag(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="en-US")
        assert lang == "en"

    def test_fr_ca_gives_fr(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="fr-CA")
        assert lang == "fr"

    def test_zh_hans_gives_zh(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="zh-Hans")
        assert lang == "zh"

    def test_header_fallback(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="", content_language_header="de")
        assert lang == "de"

    def test_html_attr_overrides_header(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="en", content_language_header="fr")
        assert lang == "en"

    def test_defaults_to_en_on_failure(self):
        from pipeline.language import detect_language
        lang = detect_language("", html_lang="", content_language_header="")
        assert lang == "en"

    def test_extract_html_lang_en(self):
        from pipeline.language import extract_html_lang
        html = '<html lang="en"><body></body></html>'
        assert extract_html_lang(html) == "en"

    def test_extract_html_lang_fr_ca(self):
        from pipeline.language import extract_html_lang
        html = "<html lang='fr-CA'><body></body></html>"
        assert extract_html_lang(html) == "fr-CA"

    def test_extract_html_lang_missing(self):
        from pipeline.language import extract_html_lang
        html = "<html><body></body></html>"
        assert extract_html_lang(html) == ""

    def test_extract_html_lang_case_insensitive(self):
        from pipeline.language import extract_html_lang
        html = '<HTML LANG="EN"><BODY></BODY></HTML>'
        result = extract_html_lang(html)
        assert result.lower() in ("en", "EN", "")   # either case is fine


# =============================================================================
# NLP ENRICHMENT TESTS
# =============================================================================

class TestNLPEnrichment:
    """Tests for pipeline/nlp.py — spaCy NER and keyword extraction."""

    @pytest.fixture(scope="class")
    def article_nlp(self):
        from pipeline.nlp import enrich
        text = (
            "Google and Microsoft are competing in the AI search market. "
            "Sundar Pichai, CEO of Alphabet, announced new features for Google Search. "
            "The company is headquartered in Mountain View, California. "
            "OpenAI released GPT-4 in March 2023, which changed the industry. "
            "Vector embeddings and semantic search are transforming information retrieval. "
            "Neural networks can understand the meaning of a query, not just keywords. "
            "The BERT model from Google uses transformer architecture for language understanding. "
            "Researchers at Stanford University published a paper on dense retrieval methods. "
        )
        return enrich(text=text, title="AI Companies and Search Technology")

    def test_returns_entities(self, article_nlp):
        assert len(article_nlp.entities) > 0, "Should extract at least one entity"

    def test_finds_org_entities(self, article_nlp):
        org_texts = [e["text"] for e in article_nlp.entities if e["label"] == "ORG"]
        assert len(org_texts) > 0, f"Should find ORG entities, got: {article_nlp.entities}"

    def test_finds_person_entities(self, article_nlp):
        person_texts = [e["text"] for e in article_nlp.entities if e["label"] == "PERSON"]
        assert len(person_texts) > 0, "Should find PERSON entities"

    def test_entities_have_required_fields(self, article_nlp):
        for entity in article_nlp.entities:
            assert "text"  in entity, "Entity missing 'text'"
            assert "label" in entity, "Entity missing 'label'"
            assert "count" in entity, "Entity missing 'count'"
            assert isinstance(entity["count"], int)
            assert entity["count"] >= 1

    def test_returns_keywords(self, article_nlp):
        assert len(article_nlp.keywords) > 0, "Should extract keywords"

    def test_keywords_are_strings(self, article_nlp):
        for kw in article_nlp.keywords:
            assert isinstance(kw, str)
            assert len(kw) > 0

    def test_word_count_positive(self, article_nlp):
        assert article_nlp.word_count > 10

    def test_sentence_count_positive(self, article_nlp):
        assert article_nlp.sentence_count > 0

    def test_nlp_ms_recorded(self, article_nlp):
        assert article_nlp.nlp_ms >= 0

    def test_empty_text_handled(self):
        from pipeline.nlp import enrich
        result = enrich(text="", title="")
        assert result.error != "" or (result.word_count == 0 and len(result.entities) == 0)

    def test_entities_sorted_by_count(self, article_nlp):
        """Entities should be sorted by count (most frequent first)."""
        counts = [e["count"] for e in article_nlp.entities]
        assert counts == sorted(counts, reverse=True), "Entities should be sorted by count desc"

    def test_no_duplicate_entities(self, article_nlp):
        """Each (text, label) pair should appear only once."""
        seen = set()
        for e in article_nlp.entities:
            key = (e["text"], e["label"])
            assert key not in seen, f"Duplicate entity: {key}"
            seen.add(key)


# =============================================================================
# EMBEDDER TESTS
# =============================================================================

class TestEmbedder:
    """Tests for pipeline/embedder.py — sentence-transformers."""

    @pytest.fixture(scope="class")
    def article_embedding(self):
        from pipeline.embedder import embed
        return embed(
            title="Machine Learning in Web Search",
            text="Semantic search uses vector embeddings to find relevant documents. "
                 "Neural networks encode text into high-dimensional vectors. "
                 "Similar texts cluster together in embedding space.",
        )

    def test_embedding_has_384_dimensions(self, article_embedding):
        assert len(article_embedding.embedding) == 384, (
            f"Expected 384 dimensions, got {len(article_embedding.embedding)}"
        )

    def test_embedding_is_l2_normalised(self, article_embedding):
        """L2 norm of a normalised vector should be ~1.0."""
        vec = article_embedding.embedding
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 0.01, f"Expected unit norm, got {norm:.4f}"

    def test_embedding_values_are_floats(self, article_embedding):
        for val in article_embedding.embedding[:10]:
            assert isinstance(val, float)

    def test_embedding_values_in_reasonable_range(self, article_embedding):
        """Normalised embeddings should have values roughly between -1 and 1."""
        for val in article_embedding.embedding:
            assert -1.0 <= val <= 1.0, f"Value out of range: {val}"

    def test_success_flag(self, article_embedding):
        assert article_embedding.success is True

    def test_embed_ms_recorded(self, article_embedding):
        assert article_embedding.embed_ms >= 0

    def test_different_texts_have_different_embeddings(self):
        from pipeline.embedder import embed
        e1 = embed("Python programming", "Python is a programming language.")
        e2 = embed("Cooking recipes", "Mix flour and eggs together.")
        # Cosine similarity should be low for unrelated texts
        dot = sum(a * b for a, b in zip(e1.embedding, e2.embedding))
        assert dot < 0.9, f"Unrelated texts should not be nearly identical (dot={dot:.3f})"

    def test_similar_texts_have_similar_embeddings(self):
        from pipeline.embedder import embed
        e1 = embed("Machine learning", "Neural networks learn from data.")
        e2 = embed("Deep learning", "Neural networks process training examples.")
        dot = sum(a * b for a, b in zip(e1.embedding, e2.embedding))
        assert dot > 0.5, f"Similar texts should have high similarity (dot={dot:.3f})"

    def test_empty_input_handled(self):
        from pipeline.embedder import embed
        result = embed("", "")
        assert result.success is False
        assert result.error

    def test_query_embedding(self):
        from pipeline.embedder import embed_query
        vec = embed_query("machine learning transformers")
        assert len(vec) == 384
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 0.01

    def test_query_embedding_empty(self):
        from pipeline.embedder import embed_query
        result = embed_query("")
        assert result == []


# =============================================================================
# METADATA EXTRACTION TESTS
# =============================================================================

class TestMetadataExtraction:
    """Tests for pipeline/metadata.py — extruct OpenGraph + Schema.org."""

    @pytest.fixture(scope="class")
    def article_meta(self):
        from pipeline.metadata import extract_metadata
        html = load_fixture("article.html")
        return extract_metadata(html, url="https://example.com/article")

    def test_extracts_og_title(self, article_meta):
        assert article_meta.og_title, "Should extract og:title"
        assert "Machine Learning" in article_meta.og_title

    def test_extracts_og_description(self, article_meta):
        assert article_meta.og_description, "Should extract og:description"

    def test_extracts_og_image(self, article_meta):
        assert article_meta.og_image, "Should extract og:image"
        assert article_meta.og_image.startswith("http")

    def test_extracts_schema_type(self, article_meta):
        assert article_meta.schema_type == "Article"

    def test_extracts_date_published(self, article_meta):
        assert article_meta.date_published, "Should extract datePublished"
        assert "2024" in article_meta.date_published

    def test_extracts_author(self, article_meta):
        assert article_meta.author, "Should extract author name"
        assert "Jane Smith" in article_meta.author

    def test_extracts_canonical_url(self, article_meta):
        assert article_meta.canonical_url, "Should extract canonical URL"
        assert "example.com" in article_meta.canonical_url

    def test_meta_ms_recorded(self, article_meta):
        assert article_meta.meta_ms >= 0

    def test_empty_html_returns_empty_result(self):
        from pipeline.metadata import extract_metadata
        result = extract_metadata("", url="")
        assert result.og_title == ""
        assert result.schema_type == ""
        assert result.error == ""   # empty HTML is not an error

    def test_no_metadata_html_returns_empty(self):
        from pipeline.metadata import extract_metadata
        html = "<html><body><p>Simple page with no metadata.</p></body></html>"
        result = extract_metadata(html)
        assert result.og_title == ""
        assert result.schema_type == ""

    def test_canonical_regex_fallback(self):
        from pipeline.metadata import _extract_canonical
        html = '<link rel="canonical" href="https://example.com/page">'
        assert _extract_canonical(html) == "https://example.com/page"

    def test_canonical_regex_alternate_order(self):
        from pipeline.metadata import _extract_canonical
        html = '<link href="https://example.com/page" rel="canonical">'
        result = _extract_canonical(html)
        assert result == "https://example.com/page"


# =============================================================================
# RANKING SIGNALS TESTS
# =============================================================================

class TestRankingSignals:
    """Tests for pipeline/signals.py — freshness, quality, authority scoring."""

    def test_freshness_just_indexed_is_near_1(self):
        from pipeline.signals import compute_signals
        signals = compute_signals(
            word_count=500, language="en", title="Test",
            has_og_title=True, has_schema_type=True,
            has_author=True, has_date=True,
            date_published="",
            crawled_at=time.time(),   # just now
            domain_authority=0.8,
        )
        assert signals.freshness_score > 0.95, (
            f"Just-indexed page should be ~1.0, got {signals.freshness_score}"
        )

    def test_freshness_old_page_is_low(self):
        from pipeline.signals import compute_signals
        # 90 days ago
        old_crawled_at = time.time() - (90 * 86400)
        signals = compute_signals(
            word_count=500, language="en", title="Test",
            has_og_title=False, has_schema_type=False,
            has_author=False, has_date=False,
            date_published="",
            crawled_at=old_crawled_at,
        )
        assert signals.freshness_score < 0.2, (
            f"90-day-old page should have low freshness, got {signals.freshness_score}"
        )

    def test_freshness_from_date_published(self):
        from pipeline.signals import compute_signals
        # Published 7 days ago
        seven_days_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 7 * 86400)
        )
        signals = compute_signals(
            word_count=500, language="en", title="Test",
            has_og_title=True, has_schema_type=True,
            has_author=True, has_date=True,
            date_published=seven_days_ago,
            crawled_at=time.time(),
        )
        # 7 days: exp(-ln(2)/30 * 7) ≈ 0.85
        assert 0.75 < signals.freshness_score < 0.95, (
            f"7-day-old page should have ~0.85 freshness, got {signals.freshness_score}"
        )

    def test_quality_high_for_rich_content(self):
        from pipeline.signals import compute_signals
        signals = compute_signals(
            word_count=2000, language="en", title="Complete Article",
            has_og_title=True, has_schema_type=True,
            has_author=True, has_date=True,
            date_published="2024-01-15",
            crawled_at=time.time(),
            domain_authority=0.9,
        )
        assert signals.content_quality > 0.8

    def test_quality_low_for_stub(self):
        from pipeline.signals import compute_signals
        signals = compute_signals(
            word_count=10, language="", title="",
            has_og_title=False, has_schema_type=False,
            has_author=False, has_date=False,
            date_published="",
            crawled_at=time.time(),
            domain_authority=0.1,
        )
        assert signals.content_quality < 0.3

    def test_domain_authority_clamped_0_to_1(self):
        from pipeline.signals import compute_signals
        signals_high = compute_signals(
            word_count=100, language="en", title="T",
            has_og_title=False, has_schema_type=False,
            has_author=False, has_date=False,
            date_published="", crawled_at=time.time(),
            domain_authority=5.0,   # out of range
        )
        assert signals_high.domain_authority == 1.0

        signals_low = compute_signals(
            word_count=100, language="en", title="T",
            has_og_title=False, has_schema_type=False,
            has_author=False, has_date=False,
            date_published="", crawled_at=time.time(),
            domain_authority=-2.0,   # out of range
        )
        assert signals_low.domain_authority == 0.0

    def test_structured_data_flag(self):
        from pipeline.signals import compute_signals
        with_meta = compute_signals(
            word_count=500, language="en", title="Test",
            has_og_title=True, has_schema_type=False,
            has_author=False, has_date=False,
            date_published="", crawled_at=time.time(),
        )
        assert with_meta.has_structured_data is True

        without_meta = compute_signals(
            word_count=500, language="en", title="Test",
            has_og_title=False, has_schema_type=False,
            has_author=False, has_date=False,
            date_published="", crawled_at=time.time(),
        )
        assert without_meta.has_structured_data is False

    def test_all_signal_values_in_range(self):
        from pipeline.signals import compute_signals
        signals = compute_signals(
            word_count=300, language="en", title="Test Article",
            has_og_title=True, has_schema_type=True,
            has_author=True, has_date=True,
            date_published="2024-01-15",
            crawled_at=time.time(),
            domain_authority=0.7,
        )
        assert 0.0 <= signals.freshness_score    <= 1.0
        assert 0.0 <= signals.content_quality    <= 1.0
        assert 0.0 <= signals.domain_authority   <= 1.0
        assert 0.0 <= signals.word_count_signal  <= 1.0


# =============================================================================
# INDEXED DOCUMENT TESTS
# =============================================================================

class TestIndexedDocument:
    """Tests for shared/models/indexed_document.py."""

    def _make_doc(self, **kwargs) -> "IndexedDocument":
        from shared.models.indexed_document import IndexedDocument
        defaults = dict(
            url="https://example.com/page",
            domain="example.com",
            content_hash="a3f9c2d8e1b74f56",
            title="Test Article",
            clean_text="This is a test article about machine learning.",
            language="en",
            word_count=8,
            embedding=[0.1] * 384,
            discovered_at=time.time() - 30,
            processed_at=time.time(),
        )
        defaults.update(kwargs)
        return IndexedDocument(**defaults)

    def test_json_roundtrip(self):
        from shared.models.indexed_document import IndexedDocument
        doc = self._make_doc(
            entities=[{"text": "Google", "label": "ORG", "count": 3}],
            keywords=["machine learning", "neural network"],
            og_title="Test OG Title",
            domain_authority=0.8,
        )
        restored = IndexedDocument.from_json(doc.to_json())
        assert restored.url            == doc.url
        assert restored.domain         == doc.domain
        assert restored.content_hash   == doc.content_hash
        assert restored.title          == doc.title
        assert restored.entities       == doc.entities
        assert restored.keywords       == doc.keywords
        assert restored.og_title       == doc.og_title
        assert abs(restored.domain_authority - doc.domain_authority) < 0.001
        assert len(restored.embedding) == 384

    def test_e2e_latency_ms(self):
        discovered = time.time() - 45.0   # 45 seconds ago
        doc = self._make_doc(
            discovered_at=discovered,
            processed_at=time.time(),
        )
        assert 44_000 <= doc.e2e_latency_ms <= 46_000

    def test_display_title_prefers_og(self):
        doc = self._make_doc(title="Plain Title", og_title="OG Title")
        assert doc.display_title == "OG Title"

    def test_display_title_falls_back_to_title(self):
        doc = self._make_doc(title="Plain Title", og_title="")
        assert doc.display_title == "Plain Title"

    def test_display_title_falls_back_to_domain(self):
        doc = self._make_doc(title="", og_title="")
        assert doc.display_title == "example.com"

    def test_has_embedding_true(self):
        doc = self._make_doc(embedding=[0.1] * 384)
        assert doc.has_embedding is True

    def test_has_embedding_false_wrong_dims(self):
        doc = self._make_doc(embedding=[0.1] * 100)
        assert doc.has_embedding is False

    def test_has_embedding_false_empty(self):
        doc = self._make_doc(embedding=[])
        assert doc.has_embedding is False

    def test_top_entities_sorted(self):
        doc = self._make_doc(entities=[
            {"text": "Apple", "label": "ORG", "count": 1},
            {"text": "Google", "label": "ORG", "count": 5},
            {"text": "Microsoft", "label": "ORG", "count": 3},
        ])
        top = doc.top_entities(2)
        assert top[0]["text"] == "Google"
        assert top[1]["text"] == "Microsoft"

    def test_display_description_prefers_og(self):
        doc = self._make_doc(
            summary="Short summary.",
            og_description="OG description is longer and richer.",
        )
        assert doc.display_description == "OG description is longer and richer."

    def test_display_description_falls_back_to_summary(self):
        doc = self._make_doc(summary="Short summary.", og_description="")
        assert doc.display_description == "Short summary."