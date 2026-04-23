"""
playwright_crawler/page_classifier.py
========================================
Classifies a URL/response to decide whether it needs Playwright (JS rendering)
or can be handled by the lightweight Go HTTP fetcher.

WHY THIS EXISTS:
Playwright (headless Chromium) is 10–20× slower than a plain HTTP request.
We only use it when absolutely necessary — when a page requires JavaScript
to render its content. Using it for static pages wastes time and resources.

CLASSIFICATION SIGNALS (in order of reliability):

1. KNOWN JS FRAMEWORKS (highest confidence)
   - <meta name="next-head-count"> → Next.js
   - <div id="__NEXT_DATA__">      → Next.js
   - <div id="root">               → Create React App
   - <div id="app">                → Vue / Nuxt
   - window.__NUXT__               → Nuxt.js
   - angular                       → Angular
   - gatsby                        → Gatsby

2. EMPTY CONTENT HEURISTIC
   - Body text < 200 chars but page is not an error page
     → likely a loading shell that needs JS to render content

3. RESPONSE HEADERS
   - X-Powered-By: Next.js         → definitely Next.js
   - Content-Type: application/json → not an HTML page at all

4. URL PATTERNS
   - Sites known to require JS (configurable list)
   - URLs ending in /app, /dashboard, /portal

DECISION TREE:
  is_json_api(url, response)   → return False (not a page at all)
  is_known_spa_framework(html) → return True  (needs Playwright)
  is_empty_shell(html)         → return True  (needs Playwright)
  else                         → return False (Go fetcher is fine)
"""

import re
from dataclasses import dataclass
from typing import Optional


# ── SPA framework fingerprints ─────────────────────────────────────────────────
# These patterns in the HTML strongly indicate JS rendering is required.
SPA_PATTERNS = [
    # Next.js
    re.compile(r'id="__NEXT_DATA__"',        re.IGNORECASE),
    re.compile(r'name="next-head-count"',    re.IGNORECASE),
    re.compile(r'__NEXT_ROUTER_BASEPATH__',  re.IGNORECASE),

    # React (Create React App)
    re.compile(r'<div\s+id=["\']root["\']>\s*</div>', re.IGNORECASE),

    # Vue / Nuxt
    re.compile(r'window\.__NUXT__',          re.IGNORECASE),
    re.compile(r'<div\s+id=["\']app["\']>\s*</div>', re.IGNORECASE),

    # Angular
    re.compile(r'ng-version=',               re.IGNORECASE),
    re.compile(r'<app-root>',                re.IGNORECASE),

    # Gatsby
    re.compile(r'gatsby-focus-wrapper',      re.IGNORECASE),
    re.compile(r'___gatsby',                 re.IGNORECASE),

    # Generic SPA shell (almost no text content)
    re.compile(r'<body[^>]*>\s*<div[^>]*>\s*</div>\s*</body>', re.IGNORECASE),
]

# Domains known to always need JS rendering
ALWAYS_JS_DOMAINS = {
    "twitter.com", "x.com",           # React SPA
    "notion.so",                       # Next.js
    "figma.com",                       # React SPA
    "linear.app",                      # React SPA
    "vercel.app",                      # often Next.js
}

# Content-type values that should NEVER be sent to Playwright
SKIP_CONTENT_TYPES = {
    "application/json",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/pdf",
    "image/",
    "video/",
    "audio/",
}


@dataclass
class ClassificationResult:
    needs_playwright: bool
    reason: str              # Human-readable reason for the decision
    confidence: float        # 0.0–1.0


def classify(
    url: str,
    html: str,
    content_type: str = "text/html",
    response_headers: Optional[dict] = None,
) -> ClassificationResult:
    """
    Classify a URL/response to determine if Playwright is needed.

    Args:
        url:              The URL being classified
        html:             Raw HTML content of the page
        content_type:     Content-Type from the HTTP response
        response_headers: Full response headers dict

    Returns:
        ClassificationResult with needs_playwright flag and reason
    """
    response_headers = response_headers or {}

    # ── 1. Non-HTML content → never use Playwright ────────────────────────────
    for skip_ct in SKIP_CONTENT_TYPES:
        if skip_ct in content_type.lower():
            return ClassificationResult(
                needs_playwright=False,
                reason=f"non-HTML content-type: {content_type}",
                confidence=1.0,
            )

    # ── 2. Known JS-heavy domains ─────────────────────────────────────────────
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if domain in ALWAYS_JS_DOMAINS:
        return ClassificationResult(
            needs_playwright=True,
            reason=f"domain {domain} is known to require JS",
            confidence=1.0,
        )

    # ── 3. Response header signals ────────────────────────────────────────────
    powered_by = response_headers.get("X-Powered-By", "").lower()
    if "next.js" in powered_by:
        return ClassificationResult(
            needs_playwright=True,
            reason="X-Powered-By: Next.js header",
            confidence=0.95,
        )

    if not html:
        return ClassificationResult(
            needs_playwright=False,
            reason="empty HTML body",
            confidence=0.9,
        )

    # ── 4. SPA framework fingerprints in HTML ─────────────────────────────────
    for pattern in SPA_PATTERNS:
        if pattern.search(html):
            return ClassificationResult(
                needs_playwright=True,
                reason=f"SPA pattern detected: {pattern.pattern[:50]}",
                confidence=0.90,
            )

    # ── 5. Empty shell heuristic ─────────────────────────────────────────────
    # Strip all HTML tags and count remaining text
    text_only = re.sub(r'<[^>]+>', '', html).strip()
    text_length = len(text_only)

    # If the page has very little visible text but is not tiny (404 page etc.)
    html_length = len(html)
    if html_length > 2000 and text_length < 200:
        return ClassificationResult(
            needs_playwright=True,
            reason=f"empty shell: {html_length} bytes HTML but only {text_length} chars visible text",
            confidence=0.75,
        )

    # ── Default: static page, Go fetcher is sufficient ────────────────────────
    return ClassificationResult(
        needs_playwright=False,
        reason="no JS rendering signals detected",
        confidence=0.85,
    )


def needs_playwright(url: str, html: str, **kwargs) -> bool:
    """Convenience wrapper — returns just the boolean."""
    return classify(url, html, **kwargs).needs_playwright