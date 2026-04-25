"""
services/api/retrieval/fusion.py
==================================
Reciprocal Rank Fusion (RRF) — merges BM25 and ANN result lists
into a single, better-ranked combined result.

WHY RRF INSTEAD OF SIMPLE SCORE AVERAGING?
-------------------------------------------
Naive score averaging fails because BM25 scores and cosine similarity
scores live on completely different scales:
  BM25 score:       0 → 25+   (unbounded, depends on term frequency)
  Cosine similarity: 0.3 → 1.0 (bounded, L2-normalised)

Simply averaging "BM25=18.4 + cosine=0.72" = 19.12 / 2 = 9.56
The BM25 score dominates completely. Semantic search is ignored.

RRF fixes this by using RANK not score. Rank is position-based and
independent of the underlying score scale:
  BM25 rank:    1, 2, 3, ... (position in sorted BM25 results)
  ANN rank:     1, 2, 3, ... (position in sorted ANN results)
  RRF combines: 1/(k + rank) for each result list

RRF FORMULA:
  For a document d appearing in result lists L1, L2, ...:
    RRF(d) = Σᵢ 1 / (k + rankᵢ(d))

  Where:
    k = 60  (constant that smooths rank differences; standard value)
    rankᵢ(d) = position of d in list i (1-indexed)

WORKED EXAMPLE:
  Query: "machine learning"

  BM25 results:       ANN results:
  1. example.com/a    1. example.com/b  (semantically closest)
  2. example.com/b    2. example.com/a
  3. example.com/c    3. example.com/d

  RRF scores (k=60):
    example.com/a: 1/(60+1) + 1/(60+2) = 0.01639 + 0.01613 = 0.03252
    example.com/b: 1/(60+2) + 1/(60+1) = 0.01613 + 0.01639 = 0.03252  ← tie
    example.com/c: 1/(60+3) + 0         = 0.01587
    example.com/d: 0        + 1/(60+3)  = 0.01587

  Final ranking: a ≈ b > c ≈ d

COMPOSITE RERANKING:
  After RRF fusion, we apply a lightweight composite reranker that
  adjusts scores using freshness and domain authority signals from Redis:

    final_score = 0.80 × rrf_score
                + 0.10 × freshness_score
                + 0.10 × domain_authority

  This ensures fresh, high-authority content ranks higher than stale
  content with equal RRF score.
"""

from __future__ import annotations

import time
from typing import Optional

from shared.logging import get_logger

log = get_logger("api.fusion")

# Standard RRF k constant (60 is well-established in literature)
RRF_K = 60

# Composite score weights
W_RRF       = 0.80
W_FRESHNESS = 0.10
W_AUTHORITY = 0.10


class HybridFusion:
    """
    Merges BM25 and ANN result lists using Reciprocal Rank Fusion,
    then reranks using freshness and domain authority signals.
    """

    def fuse(
        self,
        bm25_hits:     list[dict],
        ann_hits:      list[dict],
        limit:         int = 10,
        offset:        int = 0,
    ) -> list[dict]:
        """
        Fuse two ranked result lists into a single ranked list.

        Args:
            bm25_hits:  Results from Meilisearch (ordered by BM25 score)
            ann_hits:   Results from Qdrant (ordered by cosine similarity)
            limit:      Number of results to return
            offset:     Pagination offset

        Returns:
            List of merged, reranked result dicts.
        """
        start = time.monotonic()

        # ── Step 1: Build URL → document mapping ──────────────────────────────
        # Merge payload from both result sets (BM25 has highlights, ANN has scores)
        docs: dict[str, dict] = {}

        for rank, hit in enumerate(bm25_hits, start=1):
            url = hit.get("url", "")
            if not url:
                continue
            docs[url] = {
                **hit,
                "_bm25_rank":  rank,
                "_bm25_score": hit.get("_rankingScore", 0.0),
                "_ann_rank":   None,
                "_ann_score":  0.0,
            }

        for rank, hit in enumerate(ann_hits, start=1):
            url = hit.get("url", "")
            if not url:
                continue
            if url in docs:
                # Merge — ANN adds semantic score and may have newer payload
                docs[url]["_ann_rank"]  = rank
                docs[url]["_ann_score"] = hit.get("_semantic_score", 0.0)
            else:
                docs[url] = {
                    **hit,
                    "_bm25_rank":  None,
                    "_bm25_score": 0.0,
                    "_ann_rank":   rank,
                    "_ann_score":  hit.get("_semantic_score", 0.0),
                }

        # ── Step 2: Compute RRF score for each document ───────────────────────
        for url, doc in docs.items():
            rrf = 0.0
            if doc["_bm25_rank"] is not None:
                rrf += 1.0 / (RRF_K + doc["_bm25_rank"])
            if doc["_ann_rank"] is not None:
                rrf += 1.0 / (RRF_K + doc["_ann_rank"])
            doc["_rrf_score"] = rrf

        # ── Step 3: Composite reranking ───────────────────────────────────────
        for doc in docs.values():
            freshness = float(doc.get("freshness_score", 0.5))
            authority = float(doc.get("domain_authority", 0.5))
            rrf       = doc["_rrf_score"]

            # Normalise RRF to [0, 1]: max possible RRF = 1/(60+1) + 1/(60+1) ≈ 0.0328
            rrf_normalised = min(1.0, rrf / 0.033)

            doc["_composite_score"] = (
                W_RRF       * rrf_normalised +
                W_FRESHNESS * freshness +
                W_AUTHORITY * authority
            )

        # ── Step 4: Sort by composite score, paginate ─────────────────────────
        ranked = sorted(docs.values(), key=lambda d: d["_composite_score"], reverse=True)
        page   = ranked[offset : offset + limit]

        took_ms = int((time.monotonic() - start) * 1000)
        log.debug(
            "RRF fusion complete",
            bm25_candidates=len(bm25_hits),
            ann_candidates=len(ann_hits),
            unique_docs=len(docs),
            returned=len(page),
            took_ms=took_ms,
        )

        return page

    def bm25_only(
        self,
        bm25_hits: list[dict],
        limit:     int = 10,
        offset:    int = 0,
    ) -> list[dict]:
        """Return BM25 results with composite scoring (no ANN)."""
        for rank, doc in enumerate(bm25_hits, start=1):
            rrf = 1.0 / (RRF_K + rank)
            rrf_n = min(1.0, rrf / 0.033)
            doc["_composite_score"] = (
                W_RRF       * rrf_n +
                W_FRESHNESS * float(doc.get("freshness_score", 0.5)) +
                W_AUTHORITY * float(doc.get("domain_authority", 0.5))
            )
            doc["_bm25_rank"]  = rank
            doc["_bm25_score"] = doc.get("_rankingScore", 0.0)
            doc["_ann_rank"]   = None
            doc["_ann_score"]  = 0.0
        return bm25_hits[offset : offset + limit]

    def ann_only(
        self,
        ann_hits: list[dict],
        limit:    int = 10,
        offset:   int = 0,
    ) -> list[dict]:
        """Return ANN results with composite scoring (no BM25)."""
        for rank, doc in enumerate(ann_hits, start=1):
            rrf = 1.0 / (RRF_K + rank)
            rrf_n = min(1.0, rrf / 0.033)
            doc["_composite_score"] = (
                W_RRF       * rrf_n +
                W_FRESHNESS * float(doc.get("freshness_score", 0.5)) +
                W_AUTHORITY * float(doc.get("domain_authority", 0.5))
            )
            doc["_bm25_rank"]  = None
            doc["_bm25_score"] = 0.0
            doc["_ann_rank"]   = rank
            doc["_ann_score"]  = doc.get("_semantic_score", 0.0)
        return ann_hits[offset : offset + limit]