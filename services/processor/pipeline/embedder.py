"""
services/processor/pipeline/embedder.py
=========================================
Semantic embedding generator using sentence-transformers.

WHY EMBEDDINGS?
---------------
Keyword search (BM25) has a fundamental limitation: it only matches EXACT
words. Search for "machine learning" won't find a document that only says
"neural networks" or "deep learning" — even though they're closely related.

Embeddings solve this. They map text into a high-dimensional vector space
where semantically similar texts are geometrically close. We can then find
documents that are semantically relevant to a query even with no word overlap.

COMBINED WITH BM25:
In Phase 5 (API), we run BOTH BM25 (exact keyword) and ANN (semantic)
retrieval in parallel, then fuse results with Reciprocal Rank Fusion.
This gives us the best of both worlds: precision of keywords +
recall of semantic similarity.

MODEL: all-MiniLM-L6-v2
--------------------------
Why this model specifically:
  - 384 dimensions (vs 768 for BERT-base) → half storage, 2× faster
  - ~50ms per document on CPU → fits in our 60s SLO budget
  - Strong performance on semantic similarity benchmarks (SBERT leaderboard)
  - Pre-trained on 1B+ sentence pairs → no fine-tuning needed
  - 22MB model file → fast download, fits in Docker image easily

Alternative models we considered:
  - all-mpnet-base-v2:  better accuracy, 768d, ~100ms → too slow
  - BERT-base-uncased:  768d, ~200ms on CPU → too slow
  - OpenAI text-embedding-3-small: excellent, but external API dependency

WHAT WE EMBED:
  title + first 2000 chars of clean text
  Concatenating title boosts its weight in the vector space.
  2000 char limit prevents slow processing on very long articles.

L2 NORMALISATION:
  Embeddings are L2-normalised before storage. This means:
  - Cosine similarity = dot product (faster to compute)
  - All vectors lie on the unit hypersphere
  - Distance is purely angular (direction matters, not magnitude)

SINGLETON PATTERN:
  The model is loaded once per process (~500ms to initialise).
  Subsequent calls use the cached model in memory.
  Thread-safe — can be called from multiple coroutines concurrently.
"""

import time
from typing import Optional

from shared.logging import get_logger

log = get_logger("processor.embedder")

# ── Lazy-loaded model ─────────────────────────────────────────────────────────
_model = None

# Input limits
MAX_EMBED_CHARS = 2000      # chars of text to embed (beyond this, quality gain is minimal)
MAX_TITLE_CHARS = 200       # max title chars to prepend


class EmbedResult:
    """Result of the embedding operation."""
    __slots__ = ["embedding", "embed_ms", "model_name", "dimensions", "error"]

    def __init__(self):
        self.embedding:   list[float] = []
        self.embed_ms:    int         = 0
        self.model_name:  str         = ""
        self.dimensions:  int         = 0
        self.error:       str         = ""

    @property
    def success(self) -> bool:
        return len(self.embedding) > 0


def embed(title: str, text: str) -> EmbedResult:
    """
    Generate a 384-dimensional semantic embedding for a document.

    The embedding represents the semantic meaning of the page content.
    Pages with similar meaning will have similar (close) embeddings,
    even if they use completely different words.

    Args:
        title: Page title (prepended to text for encoding)
        text:  Cleaned article text (from cleaner.py)

    Returns:
        EmbedResult with 384-float embedding vector (L2-normalised).
        result.success is False if encoding failed.

    Performance:
        ~50ms on CPU for typical article length.
        GPU would reduce this to ~5ms.
    """
    result = EmbedResult()
    start = time.monotonic()

    if not text and not title:
        result.error = "No text to embed"
        return result

    try:
        model = _get_model()
        result.model_name = model.model_name_or_path
        result.dimensions = model.get_sentence_embedding_dimension()

        # ── Build input text ──────────────────────────────────────────────────
        # Format: "{title}. {text[:2000]}"
        # The period after title creates a natural sentence boundary.
        input_parts = []
        if title:
            input_parts.append(title[:MAX_TITLE_CHARS].strip())
        if text:
            input_parts.append(text[:MAX_EMBED_CHARS].strip())

        input_text = ". ".join(input_parts)

        if not input_text.strip():
            result.error = "Empty input after truncation"
            return result

        # ── Generate embedding ────────────────────────────────────────────────
        # normalize_embeddings=True → L2 normalisation (cosine sim = dot product)
        embedding = model.encode(
            input_text,
            normalize_embeddings=True,   # L2-normalise for cosine similarity
            show_progress_bar=False,
        )

        result.embedding = embedding.tolist()

    except Exception as e:
        result.error = f"Embedding error: {e}"
        log.error("Embedding generation failed", error=str(e))

    result.embed_ms = int((time.monotonic() - start) * 1000)

    if result.success:
        log.debug(
            "Embedding generated",
            dims=result.dimensions,
            embed_ms=result.embed_ms,
            model=result.model_name,
        )
    else:
        log.warning("Embedding failed", error=result.error)

    return result


def embed_query(query_text: str) -> list[float]:
    """
    Embed a search query for ANN retrieval.

    Same model as document embedding — ensures query and document
    vectors live in the same semantic space.

    Args:
        query_text: Raw search query string

    Returns:
        384-float L2-normalised embedding, or empty list on failure.

    Used by the API service (Phase 5) for semantic search.
    """
    if not query_text or not query_text.strip():
        return []
    try:
        model = _get_model()
        embedding = model.encode(
            query_text.strip(),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embedding.tolist()
    except Exception as e:
        log.error("Query embedding failed", query=query_text[:100], error=str(e))
        return []


def _get_model():
    """
    Lazy-load the sentence-transformers model.
    Thread-safe singleton — loaded once per process, cached in memory.
    """
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        from shared.config import settings

        model_name = settings.embedding_model
        log.info("Loading embedding model", model=model_name)
        load_start = time.monotonic()

        _model = SentenceTransformer(model_name)
        # Warm up the model with a dummy sentence (JIT compilation)
        _model.encode("warmup", show_progress_bar=False)

        load_ms = int((time.monotonic() - load_start) * 1000)
        dims = _model.get_sentence_embedding_dimension()
        log.info(
            "Embedding model ready",
            model=model_name,
            dimensions=dims,
            load_ms=load_ms,
        )
    return _model