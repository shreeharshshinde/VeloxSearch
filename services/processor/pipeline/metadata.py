"""
services/processor/pipeline/metadata.py
=========================================
Structured metadata extraction from HTML.

WHAT IS STRUCTURED METADATA?
-----------------------------
Many web pages embed structured data in their HTML that describes the
content in a machine-readable way. Three major formats exist:

  1. OpenGraph (og:)
     Developed by Facebook. Used by every major social network.
     Embedded as <meta property="og:*"> tags.
     Fields: og:title, og:description, og:image, og:url, og:type

  2. Schema.org / JSON-LD
     Developed by Google/Bing/Yahoo. The richest format.
     Embedded as <script type="application/ld+json"> blocks.
     Hundreds of types: Article, Product, Event, Recipe, JobPosting, etc.
     Fields: @type, name, description, datePublished, author, etc.

  3. Twitter Card (twitter:)
     Similar to OpenGraph. Used for Twitter/X link previews.
     Fields: twitter:title, twitter:description, twitter:image

WHY EXTRACT THIS?
  1. Rich results: Show article date, author, image in search results
  2. Better ranking signals: Schema.org datePublished → freshness score
  3. Entity disambiguation: Author name from structured data is reliable
  4. Type-aware indexing: Article vs Product vs Event → different ranking

LIBRARY: extruct
  extruct extracts all structured formats from HTML in one call.
  Supports: json-ld, opengraph, microdata, rdfa, microformat2, dublincore
  Very fast: regex-based, no DOM parsing needed for og: tags.

SAFETY:
  All extraction is wrapped in try/except — malformed metadata
  should never crash the pipeline. We gracefully fall back to empty strings.
"""

import time
from typing import Optional

from shared.logging import get_logger

log = get_logger("processor.metadata")


class MetadataResult:
    """Extracted structured metadata from a web page."""
    __slots__ = [
        "og_title", "og_description", "og_image", "og_type",
        "canonical_url", "schema_type", "schema_description",
        "date_published", "date_modified", "author",
        "meta_ms", "error",
    ]

    def __init__(self):
        self.og_title:          str = ""
        self.og_description:    str = ""
        self.og_image:          str = ""
        self.og_type:           str = ""
        self.canonical_url:     str = ""
        self.schema_type:       str = ""
        self.schema_description:str = ""
        self.date_published:    str = ""
        self.date_modified:     str = ""
        self.author:            str = ""
        self.meta_ms:           int = 0
        self.error:             str = ""


def extract_metadata(html: str, url: str = "") -> MetadataResult:
    """
    Extract all structured metadata from a page's HTML.

    Processes in order:
      1. <link rel="canonical">        → canonical URL
      2. <meta property="og:*">        → OpenGraph
      3. <meta name="twitter:*">       → Twitter Card
      4. <script type="application/ld+json"> → Schema.org JSON-LD

    Args:
        html: Raw HTML string
        url:  Base URL for resolving relative URLs

    Returns:
        MetadataResult populated with all found metadata.
        All fields default to "" if not found.

    Performance:
        Typically 5–30ms per page.
    """
    result = MetadataResult()
    start = time.monotonic()

    if not html:
        result.meta_ms = 0
        return result

    try:
        import extruct

        # extruct.extract() is the main entry point
        # uniform=True normalises the output format
        raw = extruct.extract(
            html,
            base_url=url or "https://example.com",
            syntaxes=["json-ld", "opengraph", "microdata"],
            uniform=True,       # normalise all formats to consistent structure
        )

        # ── OpenGraph ─────────────────────────────────────────────────────────
        _parse_opengraph(raw.get("opengraph", []), result)

        # ── Schema.org JSON-LD ────────────────────────────────────────────────
        _parse_jsonld(raw.get("json-ld", []), result)

        # ── Microdata (fallback) ──────────────────────────────────────────────
        if not result.schema_type:
            _parse_microdata(raw.get("microdata", []), result)

    except ImportError:
        result.error = "extruct not installed"
        log.warning("extruct not available — metadata extraction skipped")
    except Exception as e:
        result.error = f"extruct error: {e}"
        log.warning("Metadata extraction error", url=url, error=str(e))

    # ── Canonical URL (regex fallback, faster than full parse) ────────────────
    if not result.canonical_url:
        result.canonical_url = _extract_canonical(html)

    result.meta_ms = int((time.monotonic() - start) * 1000)
    log.debug(
        "Metadata extracted",
        og_title=bool(result.og_title),
        schema_type=result.schema_type or "none",
        date=result.date_published or "none",
        meta_ms=result.meta_ms,
    )
    return result


def _parse_opengraph(og_data: list, result: MetadataResult) -> None:
    """Parse OpenGraph meta tags."""
    if not og_data:
        return

    og = og_data[0] if isinstance(og_data, list) else og_data
    if not isinstance(og, dict):
        return

    result.og_title       = _safe_str(og.get("og:title"))
    result.og_description = _safe_str(og.get("og:description"))
    result.og_image       = _safe_str(og.get("og:image"))
    result.og_type        = _safe_str(og.get("og:type"))

    # og:url is more reliable than canonical for some sites
    og_url = _safe_str(og.get("og:url"))
    if og_url and not result.canonical_url:
        result.canonical_url = og_url

    # Twitter card fields (same structure, different prefix)
    if not result.og_title:
        result.og_title = _safe_str(og.get("twitter:title"))
    if not result.og_description:
        result.og_description = _safe_str(og.get("twitter:description"))
    if not result.og_image:
        result.og_image = _safe_str(og.get("twitter:image"))


def _parse_jsonld(jsonld_data: list, result: MetadataResult) -> None:
    """
    Parse Schema.org JSON-LD structured data.

    JSON-LD can contain multiple objects — we take the first Article/NewsArticle
    if present, otherwise the first object.

    Common @type values:
        Article, NewsArticle, BlogPosting
        Product, Event, Recipe, JobPosting
        WebPage, WebSite, Organization, Person
    """
    if not jsonld_data:
        return

    # Find the most relevant object — prefer Article types
    target = None
    article_types = {"Article", "NewsArticle", "BlogPosting", "TechArticle"}

    for item in jsonld_data:
        if not isinstance(item, dict):
            continue
        item_type = _safe_str(item.get("@type"))
        if item_type in article_types:
            target = item
            break

    if target is None and jsonld_data:
        target = jsonld_data[0]

    if not target or not isinstance(target, dict):
        return

    # ── Extract fields ────────────────────────────────────────────────────────
    result.schema_type        = _safe_str(target.get("@type"))
    result.schema_description = _safe_str(target.get("description"))

    # Date fields (multiple possible keys)
    result.date_published = (
        _safe_str(target.get("datePublished"))
        or _safe_str(target.get("publishedDate"))
    )
    result.date_modified = _safe_str(target.get("dateModified"))

    # Author (can be a string or {name: ...} object)
    author_raw = target.get("author")
    if isinstance(author_raw, str):
        result.author = author_raw.strip()
    elif isinstance(author_raw, dict):
        result.author = _safe_str(author_raw.get("name"))
    elif isinstance(author_raw, list) and author_raw:
        # Multiple authors — join names
        names = []
        for a in author_raw[:3]:
            if isinstance(a, dict):
                names.append(_safe_str(a.get("name")))
            elif isinstance(a, str):
                names.append(a.strip())
        result.author = ", ".join(filter(None, names))

    # Override og_title with Schema.org name if og title is missing
    if not result.og_title:
        result.og_title = _safe_str(target.get("name") or target.get("headline"))

    # Override og_image with Schema.org image
    if not result.og_image:
        image_raw = target.get("image")
        if isinstance(image_raw, str):
            result.og_image = image_raw
        elif isinstance(image_raw, dict):
            result.og_image = _safe_str(image_raw.get("url"))
        elif isinstance(image_raw, list) and image_raw:
            first = image_raw[0]
            result.og_image = first if isinstance(first, str) else _safe_str(first.get("url", ""))


def _parse_microdata(microdata: list, result: MetadataResult) -> None:
    """Parse Microdata (schema.org embedded in HTML attributes). Fallback only."""
    if not microdata:
        return
    for item in microdata:
        if not isinstance(item, dict):
            continue
        item_type = _safe_str(item.get("type", ""))
        if "schema.org" in item_type and not result.schema_type:
            schema = item_type.split("/")[-1]
            result.schema_type = schema
            props = item.get("properties", {})
            if isinstance(props, dict):
                if not result.date_published:
                    result.date_published = _safe_str(props.get("datePublished", ""))
                if not result.author:
                    author_items = props.get("author", [])
                    if author_items:
                        result.author = _safe_str(author_items[0])
            break


def _extract_canonical(html: str) -> str:
    """
    Extract <link rel="canonical" href="..."> using regex.
    Faster than full DOM parse for this single attribute.
    """
    import re
    match = re.search(
        r'<link[^>]*\srel=["\']canonical["\'][^>]*\shref=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ) or re.search(
        r'<link[^>]*\shref=["\']([^"\']+)["\'][^>]*\srel=["\']canonical["\']',
        html,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _safe_str(value) -> str:
    """Safely convert any value to a stripped string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return _safe_str(value[0]) if value else ""
    return str(value).strip()[:500]   # cap at 500 chars