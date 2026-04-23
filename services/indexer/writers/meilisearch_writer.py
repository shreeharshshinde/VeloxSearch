"""
services/indexer/writers/meilisearch_writer.py
================================================
Writes IndexedDocuments to Meilisearch for full-text BM25 search.

WHY MEILISEARCH FOR MVP?
-------------------------
Meilisearch gives us near-real-time (NRT) search with zero configuration:
  - Documents appear in search results within ~200ms of being added
  - BM25 ranking out of the box — no schema tweaking needed
  - REST API — easy to query from FastAPI
  - Runs in a single Docker container — no cluster management
  - Handles millions of documents on modest hardware

HOW NRT WORKS IN MEILISEARCH:
  Meilisearch batches document updates internally and re-builds its
  inverted index every ~200ms (configurable). This is called a "task".
  After add_documents() returns, the document is queued but not yet
  searchable. It becomes searchable after the task completes (~200ms).

  For our 60s SLO this is fine: even at worst case, documents are
  searchable 200ms after the indexer write — well inside our budget.

DOCUMENT SCHEMA (what Meilisearch stores):
  We store a flat dict with all fields needed for:
    1. Full-text search (searchable_attributes)
    2. Filtering (filterable_attributes)
    3. Sorting (sortable_attributes)
    4. Display in search results (all fields)

RANKING RULES:
  We use Meilisearch's built-in ranking rules PLUS custom ranking scores
  (freshness_score, domain_authority) injected as sortable attributes.
  The Phase 5 API sorts by a computed composite score.
"""

import asyncio
import hashlib
import time
from typing import Optional

import meilisearch
from meilisearch.errors import MeilisearchError

from shared.config import settings
from shared.logging import get_logger
from shared.models.indexed_document import IndexedDocument

log = get_logger("indexer.meilisearch")

# ── Index configuration ───────────────────────────────────────────────────────
# Fields included in BM25 full-text search (in priority order).
# Meilisearch weights earlier fields more heavily.
SEARCHABLE_ATTRIBUTES = [
    "title",          # highest weight — title match = strong relevance
    "og_title",       # OpenGraph title (often same as title, sometimes richer)
    "keywords",       # extracted keyword phrases
    "clean_text",     # full article body (lower weight, needed for coverage)
    "og_description", # meta description
    "author",         # useful for author name searches
    "domain",         # allow searching by domain name
]

# Fields that can be used in filter expressions:
#   GET /search?q=...&filter=language=en AND domain=techcrunch.com
FILTERABLE_ATTRIBUTES = [
    "domain",
    "language",
    "schema_type",
    "discovery_source",
    "crawl_depth",
    "has_embedding",
]

# Fields that can be used in sort expressions:
#   GET /search?q=...&sort=freshness_score:desc
SORTABLE_ATTRIBUTES = [
    "crawled_at",
    "freshness_score",
    "domain_authority",
    "word_count",
    "indexed_at",
]

# Ranking rules (applied in order — first rule breaks ties with second, etc.)
RANKING_RULES = [
    "words",        # number of matching query words
    "typo",         # fewer typos = higher rank
    "proximity",    # matching words closer together = higher rank
    "attribute",    # matches in earlier searchableAttributes = higher rank
    "sort",         # sort by freshness_score:desc (applied when requested)
    "exactness",    # exact word match > prefix match
]


class MeilisearchWriter:
    """
    Writes IndexedDocuments to Meilisearch.
    Handles index creation, settings, and document upsert.
    """

    def __init__(self):
        self._client: Optional[meilisearch.Client] = None
        self._index = None
        self._ready = False

    async def connect(self) -> None:
        """Connect to Meilisearch and ensure the index is configured."""
        self._client = meilisearch.Client(
            settings.meilisearch_url,
            settings.meilisearch_api_key,
        )
        await asyncio.get_event_loop().run_in_executor(
            None, self._setup_index
        )
        self._ready = True
        log.info(
            "Meilisearch writer ready",
            url=settings.meilisearch_url,
            index=settings.meilisearch_index,
        )

    def _setup_index(self) -> None:
        """
        Create the index if it doesn't exist and apply all settings.
        Safe to call on every startup — Meilisearch is idempotent for settings.
        """
        # Create index (idempotent)
        try:
            self._client.create_index(
                settings.meilisearch_index,
                {"primaryKey": "id"},
            )
            log.info("Meilisearch index created", index=settings.meilisearch_index)
        except MeilisearchError as e:
            if "index_already_exists" in str(e).lower():
                log.debug("Meilisearch index already exists")
            else:
                raise

        self._index = self._client.index(settings.meilisearch_index)

        # Apply settings (waits for task to complete)
        task = self._index.update_settings({
            "searchableAttributes":  SEARCHABLE_ATTRIBUTES,
            "filterableAttributes":  FILTERABLE_ATTRIBUTES,
            "sortableAttributes":    SORTABLE_ATTRIBUTES,
            "rankingRules":          RANKING_RULES,
            "highlightPreTag":       "<mark>",
            "highlightPostTag":      "</mark>",
            "pagination":            {"maxTotalHits": 1000},
            "typoTolerance": {
                "enabled": True,
                "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9},
            },
        })
        # Wait for settings task (one-time on startup)
        self._client.wait_for_task(task.task_uid, timeout_in_ms=10_000)
        log.info("Meilisearch index settings applied")

    async def write(self, doc: IndexedDocument) -> bool:
        """
        Write a single IndexedDocument to Meilisearch.

        Converts IndexedDocument → flat dict → Meilisearch document.
        Uses UPSERT semantics — if the document already exists (same URL),
        it is replaced with the new version.

        Args:
            doc: Fully enriched IndexedDocument from Phase 3

        Returns:
            True on success, False on failure.

        Performance:
            ~5–20ms per document (async, non-blocking).
        """
        if not self._ready:
            log.error("Writer not connected — call connect() first")
            return False

        try:
            ms_doc = self._to_meilisearch_doc(doc)
            task = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._index.add_documents([ms_doc]),
            )
            log.debug(
                "Meilisearch write queued",
                url=doc.url,
                task_uid=task.task_uid,
                doc_id=ms_doc["id"],
            )
            return True

        except MeilisearchError as e:
            log.error("Meilisearch write failed", url=doc.url, error=str(e))
            return False
        except Exception as e:
            log.error("Unexpected Meilisearch error", url=doc.url, error=str(e))
            return False

    async def write_batch(self, docs: list[IndexedDocument]) -> int:
        """
        Write multiple documents in a single Meilisearch API call.
        More efficient than calling write() in a loop.

        Returns:
            Number of documents successfully queued.
        """
        if not docs:
            return 0
        try:
            ms_docs = [self._to_meilisearch_doc(d) for d in docs]
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._index.add_documents(ms_docs),
            )
            log.debug("Meilisearch batch write", count=len(ms_docs))
            return len(ms_docs)
        except Exception as e:
            log.error("Meilisearch batch write failed", count=len(docs), error=str(e))
            return 0

    async def delete(self, url: str) -> bool:
        """Remove a document from the index by URL."""
        doc_id = self._url_to_id(url)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._index.delete_document(doc_id),
            )
            log.info("Meilisearch document deleted", url=url)
            return True
        except Exception as e:
            log.error("Meilisearch delete failed", url=url, error=str(e))
            return False

    async def get_stats(self) -> dict:
        """Return index statistics (document count, index size)."""
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, self._index.get_stats
            )
            return {
                "document_count": stats.number_of_documents,
                "is_indexing": stats.is_indexing,
            }
        except Exception:
            return {}

    @staticmethod
    def _url_to_id(url: str) -> str:
        """
        Convert a URL to a Meilisearch document ID.

        Meilisearch requires alphanumeric IDs without special chars.
        We use a SHA-256 hash truncated to 32 hex chars.
        This is stable — same URL always produces same ID (upsert works).
        """
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    @staticmethod
    def _to_meilisearch_doc(doc: IndexedDocument) -> dict:
        """
        Convert IndexedDocument to a flat Meilisearch document dict.

        All searchable, filterable, sortable, and display fields must
        be included here.

        Note: We do NOT store the raw embedding in Meilisearch —
        that lives in Qdrant. We do NOT store clean_text in full either —
        we store a truncated version (first 10,000 chars) to keep
        the Meilisearch index size manageable.
        """
        return {
            # Primary key (stable, derived from URL)
            "id":               MeilisearchWriter._url_to_id(doc.url),

            # Identity
            "url":              doc.url,
            "domain":           doc.domain,
            "canonical_url":    doc.canonical_url or doc.url,

            # Searchable content (title-weight → body-weight order)
            "title":            doc.title,
            "og_title":         doc.og_title,
            "keywords":         doc.keywords,               # list[str]
            "clean_text":       doc.clean_text[:10_000],    # truncated
            "og_description":   doc.og_description,
            "author":           doc.author,

            # Display fields (for search result rendering)
            "summary":          doc.summary,
            "og_image":         doc.og_image,
            "language":         doc.language,
            "published_at":     doc.published_at,
            "schema_type":      doc.schema_type,

            # Filter fields
            "discovery_source": doc.discovery_source,
            "crawl_depth":      doc.crawl_depth,
            "has_embedding":    doc.has_embedding,

            # Sort/ranking fields (numeric — used in composite score)
            "freshness_score":    round(doc.freshness_score, 4),
            "domain_authority":   round(doc.domain_authority, 4),
            "word_count":         doc.word_count,
            "crawled_at":         int(doc.crawled_at),
            "indexed_at":         int(time.time()),

            # Entity data (for faceting and rich display)
            "entity_names":     [e["text"]  for e in doc.entities[:20]],
            "entity_types":     list(set(e["label"] for e in doc.entities[:20])),
        }