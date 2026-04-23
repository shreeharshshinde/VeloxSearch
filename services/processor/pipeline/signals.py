"""
services/processor/pipeline/signals.py
=========================================
Ranking signal computation — produces numeric features that the
search ranking layer (Phase 5) uses to score and rerank results.

WHY RANKING SIGNALS?
--------------------
BM25 scores by keyword relevance. Vector search scores by semantic similarity.
But neither alone captures everything that makes a result "good":

  - A brand-new article about a query is more valuable than a 3-year-old one
  - A page from a highly authoritative domain is more trustworthy
  - A long, detailed article is usually more useful than a stub
  - A page with structured metadata is likely a higher-quality source

Ranking signals let us blend these factors into a final score:
    final_score = 0.4 * bm25_score
                + 0.4 * vector_similarity
                + 0.1 * freshness_score
                + 0.1 * domain_authority

SIGNALS WE COMPUTE:

  freshness_score    (0.0–1.0)
    1.0 = just published/indexed
    Decays exponentially with age
    Formula: exp(-age_days / 30)  → halves every ~21 days

  content_quality    (0.0–1.0)
    Based on: word count, title presence, structured metadata, language detected
    A 2000-word article with Schema.org and og:title = 0.9
    A 50-word stub with no metadata = 0.2

  domain_authority   (0.0–1.0)
    Loaded from PostgreSQL domains table
    Set manually or via Moz/Majestic API integration
    Defaults to 0.5 for unknown domains

These signals are stored alongside the document in the index.
The API reranker (Phase 5) uses them to adjust final rankings.
"""

import math
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RankingSignals:
    """Computed ranking signals for one document."""
    freshness_score:    float = 1.0     # 0.0–1.0 (1.0 = just indexed)
    content_quality:    float = 0.5     # 0.0–1.0
    domain_authority:   float = 0.5     # 0.0–1.0 (from domain registry)
    word_count_signal:  float = 0.5     # 0.0–1.0 (normalised word count)
    has_structured_data:bool  = False   # Schema.org or og: tags present
    has_author:         bool  = False   # author extracted
    has_date:           bool  = False   # date extracted
    language_confident: bool  = True    # language detection was confident


def compute_signals(
    word_count:       int,
    language:         str,
    title:            str,
    has_og_title:     bool,
    has_schema_type:  bool,
    has_author:       bool,
    has_date:         bool,
    date_published:   str,
    crawled_at:       float,
    domain_authority: float = 0.5,
) -> RankingSignals:
    """
    Compute all ranking signals for a processed document.

    Args:
        word_count:       Non-stop-word token count from NLP
        language:         Detected ISO 639-1 language code
        title:            Extracted page title
        has_og_title:     OpenGraph title present
        has_schema_type:  Schema.org @type present
        has_author:       Author name extracted
        has_date:         Publication date extracted
        date_published:   ISO 8601 date string (if known)
        crawled_at:       Unix timestamp of crawl
        domain_authority: 0.0–1.0 domain authority score

    Returns:
        RankingSignals dataclass with all computed signals.
    """
    signals = RankingSignals()
    signals.domain_authority = max(0.0, min(1.0, domain_authority))

    # ── Freshness score ───────────────────────────────────────────────────────
    signals.freshness_score = _compute_freshness(
        date_published=date_published,
        crawled_at=crawled_at,
    )

    # ── Word count signal (normalised) ────────────────────────────────────────
    # 0 words → 0.0, 100 words → 0.3, 500 words → 0.7, 2000+ words → 1.0
    signals.word_count_signal = min(1.0, math.log1p(word_count) / math.log1p(2000))

    # ── Structured data flags ─────────────────────────────────────────────────
    signals.has_structured_data = has_og_title or has_schema_type
    signals.has_author          = has_author
    signals.has_date            = has_date

    # ── Language confidence ───────────────────────────────────────────────────
    signals.language_confident = bool(language and len(language) == 2)

    # ── Content quality composite ─────────────────────────────────────────────
    signals.content_quality = _compute_quality(
        word_count=word_count,
        has_title=bool(title and len(title) > 5),
        has_structured_data=signals.has_structured_data,
        has_author=has_author,
        has_date=has_date,
        language_confident=signals.language_confident,
    )

    return signals


def _compute_freshness(date_published: str, crawled_at: float) -> float:
    """
    Compute freshness score using exponential decay.

    Formula: exp(-age_days / HALF_LIFE_DAYS)
    where HALF_LIFE_DAYS = 30 (score halves every ~30 days)

    Priority:
      1. Use date_published from structured data (most accurate)
      2. Fall back to crawled_at (time we fetched the page)

    Returns:
        1.0 = just published/indexed
        0.5 = 30 days old
        0.13 = 60 days old
        0.02 = 120 days old
    """
    HALF_LIFE_DAYS = 30.0
    DECAY_CONSTANT = math.log(2) / HALF_LIFE_DAYS   # ≈ 0.0231

    reference_time = crawled_at or time.time()

    # Try to parse date_published
    if date_published:
        try:
            from datetime import datetime, timezone
            # Try common ISO 8601 formats
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%d", "%Y/%m/%d", "%d %B %Y"):
                try:
                    dt = datetime.strptime(date_published[:25], fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    published_ts = dt.timestamp()
                    age_days = (time.time() - published_ts) / 86400
                    # Guard: don't penalise articles dated in the future
                    age_days = max(0.0, age_days)
                    return max(0.01, math.exp(-DECAY_CONSTANT * age_days))
                except ValueError:
                    continue
        except Exception:
            pass

    # Fall back to crawled_at
    age_days = (time.time() - reference_time) / 86400
    age_days = max(0.0, age_days)
    return max(0.01, math.exp(-DECAY_CONSTANT * age_days))


def _compute_quality(
    word_count:          int,
    has_title:           bool,
    has_structured_data: bool,
    has_author:          bool,
    has_date:            bool,
    language_confident:  bool,
) -> float:
    """
    Compute a content quality score from multiple signals.

    Each signal contributes a weighted amount to the final score.
    Score is clamped to [0.0, 1.0].
    """
    score = 0.0

    # Word count contributes up to 0.4
    # 0 words → 0.0, 500 words → 0.3, 2000 words → 0.4
    score += 0.4 * min(1.0, math.log1p(word_count) / math.log1p(2000))

    # Title present: +0.15
    if has_title:
        score += 0.15

    # Structured data (og: or schema:): +0.20
    if has_structured_data:
        score += 0.20

    # Author present: +0.10
    if has_author:
        score += 0.10

    # Date present: +0.10
    if has_date:
        score += 0.10

    # Language detected confidently: +0.05
    if language_confident:
        score += 0.05

    return round(min(1.0, max(0.0, score)), 3)