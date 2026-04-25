"""
services/api/models/request.py
================================
Pydantic request models for the search API.

FastAPI uses these models for:
  1. Automatic request validation (wrong types → 422 Unprocessable Entity)
  2. OpenAPI documentation generation (visible at /docs)
  3. IDE autocompletion

Every field has a description — this appears in the Swagger UI at /docs,
making the API self-documenting for anyone using it.
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class SearchMode(str, Enum):
    """Which retrieval backends to use."""
    BM25     = "bm25"     # Meilisearch keyword search only
    SEMANTIC = "semantic"  # Qdrant ANN vector search only
    HYBRID   = "hybrid"   # Both combined via RRF (default, best results)


class SearchRequest(BaseModel):
    """
    POST /search request body.

    Also used internally — GET /search query params are mapped to this model.
    """
    q: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Search query string",
        examples=["machine learning transformers"],
    )
    mode: SearchMode = Field(
        default=SearchMode.HYBRID,
        description="Retrieval mode: bm25 (keyword), semantic (vector), or hybrid (both, recommended)",
    )
    lang: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=5,
        description="Filter by language ISO 639-1 code (e.g. 'en', 'fr', 'de')",
        examples=["en"],
    )
    domain: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Filter results to a specific domain (e.g. 'techcrunch.com')",
        examples=["techcrunch.com"],
    )
    schema_type: Optional[str] = Field(
        default=None,
        description="Filter by Schema.org type (e.g. 'Article', 'Product', 'Event')",
        examples=["Article"],
    )
    since: Optional[float] = Field(
        default=None,
        description="Return only pages indexed after this Unix timestamp",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Number of results to return (1–50)",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=950,
        description="Pagination offset (max 950 to keep total hits ≤ 1000)",
    )

    @field_validator("q")
    @classmethod
    def strip_query(cls, v: str) -> str:
        return v.strip()

    @field_validator("lang")
    @classmethod
    def normalise_lang(cls, v: Optional[str]) -> Optional[str]:
        return v.lower()[:2] if v else None


class URLSubmitRequest(BaseModel):
    """POST /urls/submit request body."""
    url: str = Field(
        ...,
        description="Fully qualified URL to index immediately",
        examples=["https://en.wikipedia.org/wiki/Web_indexing"],
    )
    priority: str = Field(
        default="normal",
        description="Crawl priority: 'high' (manual queue jump) or 'normal'",
        pattern="^(high|normal)$",
    )