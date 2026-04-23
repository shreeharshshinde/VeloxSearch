"""
services/processor/pipeline/cleaner.py
========================================
HTML cleaner — strips boilerplate and extracts the main article content.

WHY TRAFILATURA?
----------------
A raw HTML page contains far more than its article:
  - Navigation menus, header, footer
  - Sidebars, ads, cookie banners
  - "Related articles" widgets, comment sections
  - Pagination, breadcrumbs

If we index all of this, our search results become polluted with nav text
and ad copy. We need ONLY the main content.

Trafilatura is the best open-source content extractor available (2024).
Benchmarks on the CLEF CLEANEVAL dataset show it outperforms:
  - newspaper3k    (older, less maintained)
  - readability    (Mozilla's algorithm)
  - justext        (good precision, lower recall)
  - goose3         (slower, less accurate)

HOW IT WORKS (simplified):
1. Parse the HTML DOM tree
2. Score each block of text using heuristics:
   - Block position in page (main content is usually central)
   - Text density (ad blocks have low text-to-tag ratio)
   - Link density (nav menus are mostly links)
   - Block length (short blocks are usually UI elements)
3. Keep high-scoring blocks → discard the rest
4. Return clean text + metadata (title, author, date, etc.)

WHAT WE EXTRACT:
  title       → from <title> or <h1>, cleaned
  text        → main article body, cleaned
  author      → byline if detectable
  date        → publication date if in metadata
  language    → from html lang= attribute or content detection

EDGE CASES:
  - Paywalled content: extracts visible text only (teaser)
  - Login walls: returns None → we skip these pages
  - Very short pages (<50 words): flagged, processed but low-scored
  - Non-English: detected and passed through (NLP handles multi-lingual)
"""

import json
import time
from typing import Optional

import trafilatura
from trafilatura.settings import use_config

from shared.logging import get_logger

log = get_logger("processor.cleaner")

# ── Trafilatura configuration ─────────────────────────────────────────────────
# Custom config for our use case: keep tables (useful for data pages),
# skip comments (usually noise), include structured metadata.
_trafilatura_config = use_config()
_trafilatura_config.set("DEFAULT", "EXTRACTION_TIMEOUT", "10")


class CleanResult:
    """Result of HTML cleaning operation."""
    __slots__ = [
        "title", "text", "author", "date",
        "language", "word_count", "success",
        "clean_ms", "error"
    ]

    def __init__(self):
        self.title:      str  = ""
        self.text:       str  = ""
        self.author:     str  = ""
        self.date:       str  = ""
        self.language:   str  = ""
        self.word_count: int  = 0
        self.success:    bool = False
        self.clean_ms:   int  = 0
        self.error:      str  = ""

    @property
    def summary(self) -> str:
        """First 300 characters of clean text — used as search snippet."""
        if not self.text:
            return ""
        # Find a clean sentence boundary near 300 chars
        if len(self.text) <= 300:
            return self.text
        truncated = self.text[:300]
        # Try to end at a sentence boundary
        last_period = max(
            truncated.rfind(". "),
            truncated.rfind("! "),
            truncated.rfind("? "),
        )
        if last_period > 150:
            return truncated[:last_period + 1]
        return truncated + "..."


def clean_html(raw_html: str, url: str = "") -> CleanResult:
    """
    Extract main content from raw HTML using Trafilatura.

    Args:
        raw_html: Raw HTML string from the crawler
        url:      Source URL (used for relative URL resolution and metadata)

    Returns:
        CleanResult with extracted text, title, author, date, language.
        result.success is False if extraction failed (empty/login wall/etc.)

    Performance:
        Typically 50–200ms per page depending on HTML complexity.
        Pages with no extractable content return success=False in ~10ms.
    """
    result = CleanResult()
    start = time.monotonic()

    if not raw_html or len(raw_html) < 100:
        result.error = f"HTML too short: {len(raw_html)} chars"
        result.clean_ms = int((time.monotonic() - start) * 1000)
        return result

    try:
        # Primary extraction — returns JSON with all metadata
        extracted_json = trafilatura.extract(
            raw_html,
            url=url or None,
            include_comments=False,    # comments are noise
            include_tables=True,       # tables have useful structured data
            include_images=False,      # we don't need image alt texts
            no_fallback=False,         # use fallback extractors if primary fails
            output_format="json",      # get structured output
            with_metadata=True,        # extract title, author, date
            config=_trafilatura_config,
        )

        if not extracted_json:
            result.error = "Trafilatura returned None (likely login wall or empty page)"
            result.clean_ms = int((time.monotonic() - start) * 1000)
            return result

        data = json.loads(extracted_json)

        # ── Extract fields ────────────────────────────────────────────────────
        result.text     = _clean_str(data.get("text", ""))
        result.title    = _clean_str(data.get("title", ""))
        result.author   = _clean_str(data.get("author", ""))
        result.date     = _clean_str(data.get("date", ""))
        result.language = _clean_str(data.get("language", ""))

        # ── Validate minimum content ──────────────────────────────────────────
        words = result.text.split()
        result.word_count = len(words)

        if result.word_count < 20:
            result.error = f"Extracted text too short: {result.word_count} words"
            result.clean_ms = int((time.monotonic() - start) * 1000)
            return result

        result.success = True

    except json.JSONDecodeError as e:
        result.error = f"JSON decode error: {e}"
    except Exception as e:
        result.error = f"Unexpected error: {e}"
        log.error("Trafilatura extraction failed", url=url, error=str(e))

    result.clean_ms = int((time.monotonic() - start) * 1000)

    if result.success:
        log.debug(
            "HTML cleaned",
            url=url,
            words=result.word_count,
            title=result.title[:60],
            language=result.language,
            clean_ms=result.clean_ms,
        )
    else:
        log.debug("Clean failed", url=url, error=result.error)

    return result


def _clean_str(value) -> str:
    """Safely convert a value to a stripped string."""
    if value is None:
        return ""
    if isinstance(value, list):
        # Author can be a list in some metadata formats
        return "; ".join(str(v).strip() for v in value if v)
    return str(value).strip()