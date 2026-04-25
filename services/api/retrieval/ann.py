"""
services/api/retrieval/ann.py
================================
ANN (Approximate Nearest Neighbour) retrieval from Qdrant.

HOW SEMANTIC RETRIEVAL WORKS:
  1. Query text → sentence-transformers encoder → 384d query vector
  2. Qdrant HNSW search: find the 50 most similar document vectors
  3. Optional payload filter applied BEFORE search (language, domain, etc.)
  4. Returns ranked points sorted by cosine similarity

WHY 50 CANDIDATES (not just top-10)?
  We fetch 50 candidates from both BM25 and ANN, then fuse them with RRF.
  More candidates = better fusion quality. The fused top-10 is returned
  to the user. The extra 40 candidates are used for better ranking only.

PAYLOAD FILTERING:
  Qdrant filters run INSIDE the HNSW search (pre-filtering).
  This means we only search the vector subspace matching the filter,
  not all vectors. E.g., filtering to language="en" searches only
  English documents, making the search both faster and more relevant.

QUERY VECTOR CACHING:
  Embedding the same query twice is wasteful (50ms each time).
  We cache query vectors in Redis with a 60-second TTL.
  Cache key: sha256("embed:" + query)[:16]
"""

import hashlib
import json
import time
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from shared.config import settings
from shared.logging import get_logger

log = get_logger("api.ann")

CANDIDATE_COUNT = 50
QUERY_VECTOR_CACHE_TTL = 60   # seconds


class ANNRetriever:
    """Retrieves semantically similar documents from Qdrant."""

    def __init__(self, redis_client=None):
        self._client = QdrantClient(url=settings.qdrant_url)
        self._redis = redis_client    # optional — for query vector caching

    def search(
        self,
        query: str,
        lang:        Optional[str] = None,
        domain:      Optional[str] = None,
        schema_type: Optional[str] = None,
        since:       Optional[float] = None,
        limit:       int = CANDIDATE_COUNT,
    ) -> dict:
        """
        Execute an ANN search against Qdrant.

        Steps:
          1. Encode query → 384d vector (cached in Redis if available)
          2. Build payload filter from lang/domain/schema_type/since
          3. Search Qdrant HNSW index
          4. Return ranked hits with cosine similarity scores

        Args:
            query:       Search query string
            lang:        Language filter (e.g. "en")
            domain:      Domain filter
            schema_type: Schema.org type filter
            since:       Unix timestamp lower bound on crawled_at
            limit:       Number of candidates to retrieve

        Returns:
            dict with keys: hits, total, took_ms
        """
        start = time.monotonic()

        # ── Step 1: Embed the query ───────────────────────────────────────────
        query_vector = self._get_query_vector(query)
        if not query_vector:
            log.warning("Query embedding failed — returning empty ANN results", query=query[:50])
            return {"hits": [], "total": 0, "took_ms": 0}

        # ── Step 2: Build payload filter ──────────────────────────────────────
        must_conditions = []

        if lang:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="language",
                    match=qdrant_models.MatchValue(value=lang),
                )
            )
        if domain:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="domain",
                    match=qdrant_models.MatchValue(value=domain),
                )
            )
        if schema_type:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="schema_type",
                    match=qdrant_models.MatchValue(value=schema_type),
                )
            )
        if since:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="crawled_at",
                    range=qdrant_models.Range(gte=since),
                )
            )

        payload_filter = (
            qdrant_models.Filter(must=must_conditions)
            if must_conditions else None
        )

        # ── Step 3: HNSW search ───────────────────────────────────────────────
        try:
            results = self._client.search(
                collection_name=settings.qdrant_collection,
                query_vector=query_vector,
                query_filter=payload_filter,
                limit=limit,
                with_payload=True,
                score_threshold=0.3,    # ignore results with < 30% similarity
            )

            hits = []
            for point in results:
                payload = point.payload or {}
                hits.append({
                    "url":              payload.get("url", ""),
                    "domain":           payload.get("domain", ""),
                    "title":            payload.get("og_title") or payload.get("title", ""),
                    "summary":          payload.get("summary", ""),
                    "og_image":         payload.get("og_image", ""),
                    "author":           payload.get("author", ""),
                    "published_at":     payload.get("published_at", ""),
                    "language":         payload.get("language", ""),
                    "schema_type":      payload.get("schema_type", ""),
                    "freshness_score":  payload.get("freshness_score", 0.5),
                    "domain_authority": payload.get("domain_authority", 0.5),
                    "crawled_at":       payload.get("crawled_at"),
                    "indexed_at":       payload.get("indexed_at"),
                    "_semantic_score":  point.score,   # cosine similarity
                })

            took_ms = int((time.monotonic() - start) * 1000)
            log.debug(
                "ANN search complete",
                query=query[:50],
                hits=len(hits),
                took_ms=took_ms,
            )

            return {
                "hits":    hits,
                "total":   len(hits),
                "took_ms": took_ms,
            }

        except Exception as e:
            log.error("Qdrant search error", query=query[:50], error=str(e))
            return {"hits": [], "total": 0, "took_ms": 0}

    def _get_query_vector(self, query: str) -> list[float]:
        """
        Get or compute a query embedding.
        Uses Redis cache to avoid re-embedding the same query.
        """
        # Check Redis cache first
        if self._redis:
            cache_key = "QVEC:" + hashlib.sha256(query.encode()).hexdigest()[:16]
            try:
                cached = self._redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        # Generate embedding (50ms on CPU)
        from services.processor.pipeline.embedder import embed_query
        vector = embed_query(query)

        # Cache it
        if vector and self._redis:
            try:
                self._redis.set(
                    cache_key,
                    json.dumps(vector),
                    ex=QUERY_VECTOR_CACHE_TTL,
                )
            except Exception:
                pass

        return vector