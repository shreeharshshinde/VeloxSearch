"""
services/indexer/writers/qdrant_writer.py
==========================================
Writes IndexedDocuments to Qdrant for ANN (approximate nearest-neighbour)
semantic vector search.

WHY QDRANT?
-----------
Qdrant is a purpose-built vector database with:
  - HNSW index: sub-millisecond ANN search at millions of vectors
  - Payload filtering: filter by domain/language BEFORE vector search
    (avoids fetching irrelevant docs and reranking them)
  - gRPC + REST: flexible, both supported
  - Native Python SDK with async support
  - Runs in a single Docker container for MVP

HNSW (Hierarchical Navigable Small World):
  HNSW builds a multi-layer graph where each node (document vector)
  is connected to its nearest neighbours. Search traverses from the
  top layer (coarse) down to the bottom layer (fine-grained), pruning
  branches that are geometrically far from the query vector.

  Result: O(log N) approximate search vs O(N) exact search.
  At 1M documents: ~1ms ANN vs ~1000ms exact — 1000× faster.

COLLECTION SCHEMA:
  Each point (document) in Qdrant has:
    - id:      uint64 (derived from URL hash)
    - vector:  float32[384] (L2-normalised embedding from Phase 3)
    - payload: dict with all metadata needed for display + filtering

UPSERT SEMANTICS:
  Qdrant UPSERT = insert if new, replace if ID exists.
  Same URL → same ID → always updates to the latest version.

PAYLOAD FILTERING:
  Qdrant filters run BEFORE the ANN search, drastically reducing
  the search space:
    filter = language == "en" AND domain == "techcrunch.com"
  This is more efficient than post-filtering returned results.
"""

import asyncio
import hashlib
import time
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

from shared.config import settings
from shared.logging import get_logger
from shared.models.indexed_document import IndexedDocument

log = get_logger("indexer.qdrant")

# ── Collection configuration ──────────────────────────────────────────────────
VECTOR_SIZE   = 384    # all-MiniLM-L6-v2 output dimension
VECTOR_DIST   = qdrant_models.Distance.COSINE   # L2-normalised → cosine = dot product

# HNSW parameters (higher = better quality, slower build)
HNSW_M           = 16    # number of bi-directional links per node (8–64)
HNSW_EF_CONSTRUCT = 100  # candidates during index build (quality vs speed)

# Quantisation: compress vectors to int8 to reduce RAM 4× at slight quality cost
# For MVP, disable (simpler). Enable in production when RAM is constrained.
USE_QUANTISATION = False


class QdrantWriter:
    """
    Async Qdrant writer — upserts document vectors into the HNSW index.
    """

    def __init__(self):
        self._client: Optional[AsyncQdrantClient] = None
        self._ready = False

    async def connect(self) -> None:
        """Connect to Qdrant and ensure the collection exists."""
        self._client = AsyncQdrantClient(url=settings.qdrant_url)
        await self._ensure_collection()
        self._ready = True
        log.info(
            "Qdrant writer ready",
            url=settings.qdrant_url,
            collection=settings.qdrant_collection,
            vector_size=VECTOR_SIZE,
        )

    async def _ensure_collection(self) -> None:
        """
        Create the Qdrant collection if it doesn't exist.
        If it exists with the correct config, does nothing.
        Safe to call on every startup.
        """
        try:
            info = await self._client.get_collection(settings.qdrant_collection)
            existing_size = info.config.params.vectors.size
            if existing_size != VECTOR_SIZE:
                log.error(
                    "Collection exists with wrong vector size",
                    existing=existing_size,
                    expected=VECTOR_SIZE,
                )
            else:
                log.debug("Qdrant collection already exists", collection=settings.qdrant_collection)
            return
        except (UnexpectedResponse, Exception) as e:
            if "404" not in str(e) and "not found" not in str(e).lower():
                raise

        # Create collection with HNSW index
        vectors_config = qdrant_models.VectorParams(
            size=VECTOR_SIZE,
            distance=VECTOR_DIST,
            hnsw_config=qdrant_models.HnswConfigDiff(
                m=HNSW_M,
                ef_construct=HNSW_EF_CONSTRUCT,
                full_scan_threshold=10_000,  # use exact search below this count
            ),
            # Optional: quantise to int8 for 4× memory reduction
            quantization_config=(
                qdrant_models.ScalarQuantization(
                    scalar=qdrant_models.ScalarQuantizationConfig(
                        type=qdrant_models.ScalarType.INT8,
                        quantile=0.99,
                        always_ram=True,
                    )
                ) if USE_QUANTISATION else None
            ),
        )

        optimisers_config = qdrant_models.OptimizersConfigDiff(
            # Build HNSW index when segment reaches 20k vectors
            indexing_threshold=20_000,
            # Use 2 segments during initial bulk load
            default_segment_number=2,
        )

        await self._client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=vectors_config,
            optimizers_config=optimisers_config,
        )

        # Create payload indexes for efficient filtering
        await self._create_payload_indexes()
        log.info("Qdrant collection created", collection=settings.qdrant_collection)

    async def _create_payload_indexes(self) -> None:
        """
        Create keyword indexes on payload fields used in filter expressions.

        Without these indexes, Qdrant scans ALL points to apply a filter.
        With indexes, it uses an inverted index — O(log N) instead of O(N).

        Critical for:
            filter = language == "en"              (millions of docs)
            filter = domain == "techcrunch.com"   (thousands of docs)
        """
        payload_indexes = [
            ("domain",           qdrant_models.PayloadSchemaType.KEYWORD),
            ("language",         qdrant_models.PayloadSchemaType.KEYWORD),
            ("schema_type",      qdrant_models.PayloadSchemaType.KEYWORD),
            ("discovery_source", qdrant_models.PayloadSchemaType.KEYWORD),
            ("crawled_at",       qdrant_models.PayloadSchemaType.FLOAT),
            ("freshness_score",  qdrant_models.PayloadSchemaType.FLOAT),
            ("domain_authority", qdrant_models.PayloadSchemaType.FLOAT),
        ]

        for field_name, schema_type in payload_indexes:
            try:
                await self._client.create_payload_index(
                    collection_name=settings.qdrant_collection,
                    field_name=field_name,
                    field_schema=schema_type,
                )
            except Exception as e:
                if "already exists" in str(e).lower():
                    pass
                else:
                    log.warning("Payload index creation failed", field=field_name, error=str(e))

    async def write(self, doc: IndexedDocument) -> bool:
        """
        Upsert a single document vector into Qdrant.

        Skips documents without a valid embedding (e.g., embedding
        generation failed in Phase 3). Full-text search still works
        for these documents via Meilisearch.

        Args:
            doc: Fully enriched IndexedDocument with 384-dim embedding

        Returns:
            True on success, False on failure or missing embedding.

        Performance:
            ~3–10ms per document (network + HNSW insert).
        """
        if not self._ready:
            log.error("Writer not connected")
            return False

        if not doc.has_embedding:
            log.debug("Skipping Qdrant write — no embedding", url=doc.url)
            return True   # not a failure — embedding may have been skipped

        try:
            point = self._to_qdrant_point(doc)
            await self._client.upsert(
                collection_name=settings.qdrant_collection,
                points=[point],
                wait=True,   # wait for confirmation (ensures durability)
            )
            log.debug("Qdrant upsert success", url=doc.url, point_id=point.id)
            return True

        except Exception as e:
            log.error("Qdrant write failed", url=doc.url, error=str(e))
            return False

    async def write_batch(self, docs: list[IndexedDocument]) -> int:
        """
        Batch upsert multiple document vectors in a single gRPC call.
        More efficient than individual writes — use for bulk indexing.

        Returns:
            Count of documents successfully upserted.
        """
        if not docs:
            return 0

        embeddable = [d for d in docs if d.has_embedding]
        if not embeddable:
            return 0

        try:
            points = [self._to_qdrant_point(d) for d in embeddable]
            await self._client.upsert(
                collection_name=settings.qdrant_collection,
                points=points,
                wait=True,
            )
            log.debug("Qdrant batch upsert", count=len(points))
            return len(points)

        except Exception as e:
            log.error("Qdrant batch write failed", count=len(embeddable), error=str(e))
            return 0

    async def delete(self, url: str) -> bool:
        """Remove a point from the collection by URL."""
        point_id = self._url_to_point_id(url)
        try:
            await self._client.delete(
                collection_name=settings.qdrant_collection,
                points_selector=qdrant_models.PointIdsList(points=[point_id]),
            )
            log.info("Qdrant point deleted", url=url, point_id=point_id)
            return True
        except Exception as e:
            log.error("Qdrant delete failed", url=url, error=str(e))
            return False

    async def get_stats(self) -> dict:
        """Return collection statistics."""
        try:
            info = await self._client.get_collection(settings.qdrant_collection)
            return {
                "vectors_count":    info.vectors_count,
                "indexed_vectors":  info.indexed_vectors_count,
                "points_count":     info.points_count,
                "status":           info.status.value,
            }
        except Exception:
            return {}

    @staticmethod
    def _url_to_point_id(url: str) -> int:
        """
        Convert a URL to a Qdrant point ID (uint64).

        Qdrant uses integer IDs for points. We derive a stable uint64
        from the URL's SHA-256 hash (first 8 bytes → uint64).
        Same URL → same ID → upsert replaces the old vector.
        """
        h = hashlib.sha256(url.encode()).digest()
        # Take first 8 bytes, interpret as big-endian uint64
        return int.from_bytes(h[:8], byteorder="big")

    @staticmethod
    def _to_qdrant_point(doc: IndexedDocument) -> qdrant_models.PointStruct:
        """
        Convert an IndexedDocument to a Qdrant PointStruct.

        The payload stores all metadata needed for:
          1. Filtering before ANN search (domain, language, etc.)
          2. Displaying results without fetching from PostgreSQL
          3. Reranking by freshness/authority in Phase 5

        We do NOT store the full clean_text in Qdrant payload —
        that would bloat the collection. Only what's needed for search.
        """
        return qdrant_models.PointStruct(
            id=QdrantWriter._url_to_point_id(doc.url),
            vector=doc.embedding,           # 384-dim float32 list
            payload={
                # Identity (for dedup and display)
                "url":              doc.url,
                "domain":           doc.domain,
                "canonical_url":    doc.canonical_url or doc.url,

                # Display fields (shown in search results)
                "title":            doc.title,
                "og_title":         doc.og_title,
                "summary":          doc.summary,
                "og_description":   doc.og_description,
                "og_image":         doc.og_image,
                "author":           doc.author,
                "published_at":     doc.published_at,

                # Filter fields (indexed in Qdrant payload index)
                "language":         doc.language,
                "schema_type":      doc.schema_type,
                "discovery_source": doc.discovery_source,

                # Ranking signals (used for hybrid reranking in Phase 5)
                "freshness_score":  round(doc.freshness_score, 4),
                "domain_authority": round(doc.domain_authority, 4),
                "word_count":       doc.word_count,

                # Timing
                "crawled_at":       doc.crawled_at,
                "indexed_at":       time.time(),

                # Content signals
                "content_hash":     doc.content_hash,
                "has_embedding":    True,
            },
        )