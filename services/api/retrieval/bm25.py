"""
services/api/retrieval/bm25.py
================================
BM25 full-text retrieval from Meilisearch.

HOW BM25 RETRIEVAL WORKS IN PHASE 5:
  1. Client sends query string: "machine learning transformers"
  2. Meilisearch tokenises: ["machine", "learning", "transformers"]
  3. For each token, looks up its posting list (which docs contain it)
  4. Scores each doc using BM25 formula (term frequency × IDF)
  5. Returns top-N docs ranked by BM25 score

WHAT WE GET BACK:
  - Ranked document list with BM25 scores
  - Highlighted snippets with <mark> tags around matching terms
  - Total hit count (for pagination)

RETRIEVAL PARAMETERS:
  limit=50   — we fetch 50 candidates for hybrid fusion (not just top-10)
               More candidates = better RRF fusion quality
  attributesToHighlight — which fields get <mark> highlighting
  attributesToRetrieve  — which payload fields to return (subset for speed)

FILTER SYNTAX (Meilisearch):
  "language = 'en'"
  "domain = 'techcrunch.com' AND freshness_score > 0.5"
  "crawled_at > 1704067200"
"""

import time
from typing import Optional

import meilisearch
from meilisearch.errors import MeilisearchError

from shared.config import settings
from shared.logging import get_logger

log = get_logger("api.bm25")

# Fields to retrieve from Meilisearch (all needed for display + scoring)
RETRIEVE_FIELDS = [
    "url", "domain", "title", "og_title", "summary", "og_description",
    "og_image", "author", "published_at", "language", "schema_type",
    "freshness_score", "domain_authority", "word_count",
    "crawled_at", "indexed_at", "has_embedding",
]

# Fields to highlight (matching query terms wrapped in <mark>)
HIGHLIGHT_FIELDS = ["title", "og_title", "summary", "og_description"]

# Number of candidates to fetch for RRF fusion
CANDIDATE_COUNT = 50


class BM25Retriever:
    """Retrieves documents from Meilisearch using BM25 ranking."""

    def __init__(self):
        self._client = meilisearch.Client(
            settings.meilisearch_url,
            settings.meilisearch_api_key,
        )
        self._index = self._client.index(settings.meilisearch_index)

    def search(
        self,
        query: str,
        lang:        Optional[str] = None,
        domain:      Optional[str] = None,
        schema_type: Optional[str] = None,
        since:       Optional[float] = None,
        limit:       int = CANDIDATE_COUNT,
        offset:      int = 0,
    ) -> dict:
        """
        Execute a BM25 search against Meilisearch.

        Args:
            query:       Search query string
            lang:        ISO 639-1 language filter (e.g. "en")
            domain:      Domain filter (e.g. "techcrunch.com")
            schema_type: Schema.org type filter (e.g. "Article")
            since:       Unix timestamp — only docs indexed after this
            limit:       Maximum results to return
            offset:      Pagination offset

        Returns:
            dict with keys: hits, total, took_ms
            Each hit contains all RETRIEVE_FIELDS plus _rankingScore and _matchesPosition.
        """
        start = time.monotonic()

        # ── Build filter expression ───────────────────────────────────────────
        filters = []
        if lang:
            filters.append(f'language = "{lang}"')
        if domain:
            filters.append(f'domain = "{domain}"')
        if schema_type:
            filters.append(f'schema_type = "{schema_type}"')
        if since:
            filters.append(f"crawled_at > {int(since)}")

        filter_str = " AND ".join(filters) if filters else None

        try:
            result = self._index.search(
                query,
                opt_params={
                    "limit":                    limit,
                    "offset":                   offset,
                    "filter":                   filter_str,
                    "attributesToRetrieve":     RETRIEVE_FIELDS,
                    "attributesToHighlight":    HIGHLIGHT_FIELDS,
                    "highlightPreTag":          "<mark>",
                    "highlightPostTag":         "</mark>",
                    "showRankingScore":         True,    # include BM25 score
                    "matchingStrategy":         "last",  # match as many terms as possible
                },
            )

            hits = result.get("hits", [])
            total = result.get("estimatedTotalHits", len(hits))
            took_ms = int((time.monotonic() - start) * 1000)

            log.debug(
                "BM25 search complete",
                query=query[:50],
                hits=len(hits),
                total=total,
                took_ms=took_ms,
            )

            return {
                "hits":    hits,
                "total":   total,
                "took_ms": took_ms,
            }

        except MeilisearchError as e:
            log.error("Meilisearch search error", query=query[:50], error=str(e))
            return {"hits": [], "total": 0, "took_ms": 0}
        except Exception as e:
            log.error("BM25 search unexpected error", error=str(e))
            return {"hits": [], "total": 0, "took_ms": 0}

    def get_document(self, url: str) -> Optional[dict]:
        """Retrieve a single document by URL (for status checks)."""
        import hashlib
        doc_id = hashlib.sha256(url.encode()).hexdigest()[:32]
        try:
            return self._index.get_document(doc_id)
        except Exception:
            return None