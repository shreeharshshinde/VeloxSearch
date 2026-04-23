"""
shared/models/indexed_document.py
===================================
IndexedDocument is the fully enriched document produced by the Processor
(Phase 3) and consumed by the Indexer (Phase 4).

Lifecycle:
    Processor runs NLP pipeline on raw HTML
        → produces IndexedDocument
        → publishes to STREAM:docs_ready
    Indexer consumes STREAM:docs_ready
        → writes to Meilisearch (BM25 full-text)
        → writes to Qdrant     (ANN vector search)
        → writes to PostgreSQL  (metadata + history)

This is the richest data model in the pipeline — it contains everything
needed for search ranking, semantic retrieval, and result display.

Design principle: IndexedDocument is IMMUTABLE after creation.
The Indexer reads it, the indexes store it. Nothing mutates it.
"""

import json
import time
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class Entity:
    """
    A named entity extracted from the page text.

    Examples:
        Entity(text="OpenAI", label="ORG")
        Entity(text="San Francisco", label="GPE")
        Entity(text="GPT-4", label="PRODUCT")

    spaCy entity labels:
        PERSON   → people (real or fictional)
        ORG      → companies, agencies, institutions
        GPE      → geopolitical entities (countries, cities)
        PRODUCT  → objects, vehicles, foods, etc.
        EVENT    → named hurricanes, battles, wars, sports events
        DATE     → absolute or relative dates
        MONEY    → monetary values, including unit
        PERCENT  → percentage
        CARDINAL → numerals that do not fall under another type
    """
    text:  str
    label: str
    count: int = 1    # how many times this entity appeared in the text

    def to_dict(self) -> dict:
        return {"text": self.text, "label": self.label, "count": self.count}

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(**d)


@dataclass
class IndexedDocument:
    """
    A fully enriched web page ready to be written to all search indexes.

    Attributes:
        # ── Identity ──────────────────────────────────────────────────────────
        url           : Canonical URL of the page
        domain        : Hostname (e.g. "techcrunch.com")
        content_hash  : FNV-64a hash of raw HTML — used for change detection
        minio_key     : Where raw HTML lives in MinIO (for reprocessing)

        # ── Extracted content ─────────────────────────────────────────────────
        title         : Page title (from <title> or <h1>)
        clean_text    : Boilerplate-stripped body text (Trafilatura output)
        summary       : First ~300 chars of clean_text (for search snippets)
        language      : ISO 639-1 language code ("en", "fr", "de", ...)
        author        : Extracted author name (if available)
        published_at  : Publication date (ISO 8601, if available)

        # ── NLP enrichment ────────────────────────────────────────────────────
        entities      : Named entities extracted by spaCy
        keywords      : Top-N keywords (TF-IDF on noun chunks)
        word_count    : Number of non-stop-word tokens

        # ── Semantic search ───────────────────────────────────────────────────
        embedding     : 384-dimensional sentence embedding (all-MiniLM-L6-v2)
                        L2-normalised float32 list — ready for cosine similarity

        # ── Structured metadata ───────────────────────────────────────────────
        og_title      : OpenGraph og:title
        og_description: OpenGraph og:description
        og_image      : OpenGraph og:image URL
        schema_type   : Schema.org @type (Article, Product, Event, etc.)
        canonical_url : <link rel="canonical"> URL

        # ── Ranking signals ───────────────────────────────────────────────────
        domain_authority : 0.0–1.0 score (from domain registry)
        freshness_score  : 0.0–1.0 score (1.0 = just published, decays daily)

        # ── Timing (for SLO tracking) ─────────────────────────────────────────
        discovered_at : Unix timestamp — when URL was first found (Phase 1)
        crawled_at    : Unix timestamp — when HTML was fetched (Phase 2)
        processed_at  : Unix timestamp — when NLP pipeline completed (Phase 3)
        indexed_at    : Unix timestamp — when written to indexes (Phase 4)

        # ── Provenance ────────────────────────────────────────────────────────
        discovery_source : "websub" | "sitemap" | "ct_log" | "link" | "manual"
        crawl_depth      : Link hops from seed URL (0 = seed)
        fetch_ms         : Time to fetch the page (from CrawledPage)
        process_ms       : Time to run NLP pipeline (Phase 3 duration)
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    url:           str
    domain:        str
    content_hash:  str
    minio_key:     str = ""

    # ── Extracted content ─────────────────────────────────────────────────────
    title:         str   = ""
    clean_text:    str   = ""
    summary:       str   = ""
    language:      str   = "en"
    author:        str   = ""
    published_at:  str   = ""         # ISO 8601 string or empty

    # ── NLP enrichment ────────────────────────────────────────────────────────
    entities:      list  = field(default_factory=list)   # list[Entity.to_dict()]
    keywords:      list  = field(default_factory=list)   # list[str]
    word_count:    int   = 0

    # ── Semantic search ───────────────────────────────────────────────────────
    embedding:     list  = field(default_factory=list)   # list[float], len=384

    # ── Structured metadata ───────────────────────────────────────────────────
    og_title:       str  = ""
    og_description: str  = ""
    og_image:       str  = ""
    schema_type:    str  = ""
    canonical_url:  str  = ""

    # ── Ranking signals ───────────────────────────────────────────────────────
    domain_authority: float = 0.5
    freshness_score:  float = 1.0    # starts at 1.0, decays over time

    # ── Timing ────────────────────────────────────────────────────────────────
    discovered_at: float = 0.0
    crawled_at:    float = 0.0
    processed_at:  float = field(default_factory=time.time)
    indexed_at:    float = 0.0       # set by Indexer when write completes

    # ── Provenance ────────────────────────────────────────────────────────────
    discovery_source: str = ""
    crawl_depth:      int = 0
    fetch_ms:         int = 0
    process_ms:       int = 0

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, data: str | bytes) -> "IndexedDocument":
        d = json.loads(data)
        return cls(**d)

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def e2e_latency_ms(self) -> int:
        """End-to-end latency: discovery → processed (ms). Used for SLO tracking."""
        if self.discovered_at > 0:
            return int((self.processed_at - self.discovered_at) * 1000)
        return 0

    @property
    def display_title(self) -> str:
        """Best available title: og:title > title > domain."""
        return self.og_title or self.title or self.domain

    @property
    def display_description(self) -> str:
        """Best available description: og:description > summary."""
        return self.og_description or self.summary

    @property
    def has_embedding(self) -> bool:
        return len(self.embedding) == 384

    def top_entities(self, n: int = 5) -> list[dict]:
        """Return top N entities sorted by occurrence count."""
        return sorted(self.entities, key=lambda e: e.get("count", 1), reverse=True)[:n]

    def __repr__(self) -> str:
        return (
            f"IndexedDocument(url={self.url!r}, "
            f"lang={self.language}, words={self.word_count}, "
            f"entities={len(self.entities)}, "
            f"has_embedding={self.has_embedding})"
        )