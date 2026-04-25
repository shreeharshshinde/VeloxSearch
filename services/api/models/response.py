"""
services/api/models/response.py
=================================
Pydantic response models for the search API.

These define exactly what JSON shape the API returns.
FastAPI serialises the response and validates its structure.
"""

from typing import Optional
from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """A single search result document."""
    url:          str   = Field(description="Canonical page URL")
    domain:       str   = Field(description="Domain of the page")
    title:        str   = Field(description="Best available title (og:title > title > domain)")
    summary:      str   = Field(description="Short content snippet (~300 chars)")
    og_image:     str   = Field(default="", description="OpenGraph image URL")
    author:       str   = Field(default="", description="Author name if extracted")
    published_at: str   = Field(default="", description="Publication date (ISO 8601)")
    language:     str   = Field(default="en", description="ISO 639-1 language code")
    schema_type:  str   = Field(default="", description="Schema.org @type")

    # Scores
    score:            float = Field(description="Composite relevance score (0–1)")
    bm25_score:       float = Field(default=0.0, description="BM25 component score")
    semantic_score:   float = Field(default=0.0, description="Semantic similarity component")
    freshness_score:  float = Field(default=0.5, description="Content freshness (0–1)")
    domain_authority: float = Field(default=0.5, description="Domain authority (0–1)")

    # Timing
    indexed_at:  Optional[float] = Field(default=None, description="Unix timestamp when indexed")
    crawled_at:  Optional[float] = Field(default=None, description="Unix timestamp when crawled")

    # Highlights (from Meilisearch)
    highlights: dict = Field(
        default_factory=dict,
        description="Field highlights with <mark> tags around matching terms",
    )


class SearchResponse(BaseModel):
    """Response envelope for search queries."""
    query:           str           = Field(description="Original query string")
    mode:            str           = Field(description="Retrieval mode used")
    total:           int           = Field(description="Total matching documents (approximate)")
    took_ms:         int           = Field(description="Query execution time in milliseconds")
    index_freshness: str           = Field(description="ISO 8601 timestamp of most recent index commit")
    from_cache:      bool          = Field(description="True if result served from Redis cache")
    results:         list[SearchResult] = Field(description="Ranked search results")


class URLSubmitResponse(BaseModel):
    """Response after submitting a URL for indexing."""
    task_id:              str   = Field(description="Unique task identifier for status polling")
    url:                  str   = Field(description="URL submitted for indexing")
    queued_at:            str   = Field(description="ISO 8601 timestamp when URL was queued")
    estimated_index_time: str   = Field(description="Estimated time until searchable")
    priority:             str   = Field(description="Queue priority assigned")


class TaskStatusResponse(BaseModel):
    """Indexing pipeline status for a submitted URL."""
    task_id:    str  = Field(description="Task identifier")
    url:        str  = Field(description="URL being indexed")
    status:     str  = Field(description="Current pipeline stage")
    progress:   dict = Field(description="Per-stage timestamps and elapsed time")
    elapsed_ms: int  = Field(description="Total elapsed milliseconds since submission")


class PipelineStats(BaseModel):
    """Overall pipeline health and performance statistics."""
    # Throughput
    urls_indexed_last_60s:    int   = Field(description="Pages indexed in last 60 seconds")
    urls_indexed_last_hour:   int   = Field(description="Pages indexed in last hour")
    total_pages_indexed:      int   = Field(description="Total pages in the index")

    # SLO metrics
    slo_pass_rate_pct:  float = Field(description="% of pages indexed within 60s SLO target")
    p50_latency_ms:     float = Field(description="Median discovery-to-indexed latency (ms)")
    p95_latency_ms:     float = Field(description="P95 discovery-to-indexed latency (ms)")

    # Queue
    queue_depth:        int   = Field(description="URLs currently waiting to be crawled")

    # Index sizes
    meilisearch_docs:   int   = Field(description="Documents in Meilisearch full-text index")
    qdrant_vectors:     int   = Field(description="Vectors in Qdrant ANN index")

    # System
    uptime_seconds:     int   = Field(description="API service uptime in seconds")
    index_freshness:    str   = Field(description="Timestamp of most recent index commit")


class HealthResponse(BaseModel):
    """Service health check response."""
    status:   str  = Field(description="'healthy' or 'degraded'")
    version:  str  = Field(description="API version string")
    backends: dict = Field(description="Per-backend health status")