"""
services/processor/pipeline/language.py
=========================================
Language detection for crawled pages.

WHY LANGUAGE DETECTION?
------------------------
We crawl the global web — pages arrive in English, French, German, Chinese,
Arabic, Hindi, and dozens of other languages. We need to know the language
to:
  1. Route to the correct spaCy model (en_core_web_sm vs multilingual)
  2. Store language in the index for filtering (lang=en in search query)
  3. Apply correct tokenisation rules
  4. Display language-appropriate snippets in search results

DETECTION STRATEGY (three layers):
  1. HTML lang= attribute  → most reliable, explicitly set by site
     <html lang="en">  → "en"
     <html lang="fr-CA"> → "fr" (strip region subtag)

  2. HTTP Content-Language header  → fairly reliable
     Content-Language: de → "de"

  3. fastText text classifier  → fallback when HTML metadata is missing
     LID-176 model: 176 languages, ~98% accuracy, runs in <1ms per page
     (lid.176.ftz is the compressed version, only 917KB)

FASTTEXT MODEL:
  - Model: lid.176.ftz (Facebook Research, 917KB)
  - 176 languages supported
  - Identifies language from a text sample (first 500 chars)
  - Returns (language_code, confidence) pairs
  - We use it as fallback only since it requires downloading the model

OUTPUT:
  ISO 639-1 two-letter code ("en", "fr", "de", "zh", "ar", etc.)
  Returns "en" as default if detection fails (safe default for English web).
"""

import re
from typing import Optional

from shared.logging import get_logger

log = get_logger("processor.language")

# ── Lazy-loaded fastText model ────────────────────────────────────────────────
_fasttext_model = None
_FASTTEXT_MODEL_PATH = "/app/models/lid.176.ftz"   # downloaded in Dockerfile

# Map of common 2-char codes — fastText uses __label__xx format
_LANG_PREFIX = "__label__"


def detect_language(
    text: str,
    html_lang: str = "",
    content_language_header: str = "",
) -> str:
    """
    Detect the primary language of a page.

    Strategy (in order of priority):
      1. HTML lang= attribute
      2. Content-Language HTTP header
      3. fastText text classification
      4. Default to "en"

    Args:
        text:                    Cleaned text content (used for fastText)
        html_lang:               Value of html[lang] attribute
        content_language_header: Content-Language HTTP response header

    Returns:
        ISO 639-1 two-letter language code (lowercase).
        Examples: "en", "fr", "de", "zh", "ar", "hi", "ja", "ko"
    """

    # ── Layer 1: HTML lang= attribute ─────────────────────────────────────────
    if html_lang:
        code = _parse_lang_tag(html_lang)
        if code:
            log.debug("Language from HTML attr", lang=code, source="html_lang")
            return code

    # ── Layer 2: Content-Language header ─────────────────────────────────────
    if content_language_header:
        code = _parse_lang_tag(content_language_header)
        if code:
            log.debug("Language from header", lang=code, source="content_language")
            return code

    # ── Layer 3: fastText text classification ─────────────────────────────────
    if text and len(text) >= 20:
        code = _fasttext_detect(text)
        if code:
            log.debug("Language from fastText", lang=code)
            return code

    # ── Layer 4: Default ──────────────────────────────────────────────────────
    log.debug("Language detection failed, defaulting to en")
    return "en"


def _parse_lang_tag(tag: str) -> Optional[str]:
    """
    Parse a BCP 47 language tag and return the 2-letter ISO 639-1 code.

    Examples:
        "en"       → "en"
        "en-US"    → "en"
        "fr-CA"    → "fr"
        "zh-Hans"  → "zh"
        "invalid"  → None
    """
    if not tag:
        return None

    # Take the primary subtag (before first hyphen)
    primary = tag.strip().split("-")[0].split("_")[0].lower()

    # Must be 2 or 3 ASCII letters
    if re.match(r"^[a-z]{2,3}$", primary):
        return primary[:2]   # normalise to 2-char ISO 639-1

    return None


def _fasttext_detect(text: str) -> Optional[str]:
    """
    Use fastText LID model to detect language from text.

    Only loads the model once (lazy singleton pattern).
    Returns None if model not available or detection fails.
    """
    global _fasttext_model

    # Try to load fastText model (may not be installed in dev)
    if _fasttext_model is None:
        try:
            import fasttext
            import os
            if os.path.exists(_FASTTEXT_MODEL_PATH):
                _fasttext_model = fasttext.load_model(_FASTTEXT_MODEL_PATH)
                log.info("fastText language model loaded", path=_FASTTEXT_MODEL_PATH)
            else:
                _fasttext_model = False    # sentinel: model not available
                log.warning(
                    "fastText model not found — language detection limited",
                    path=_FASTTEXT_MODEL_PATH,
                )
        except ImportError:
            _fasttext_model = False
            log.warning("fastText not installed — using HTML attr / header only")

    if not _fasttext_model:
        return None

    try:
        # Use first 500 chars — enough for reliable detection
        sample = text[:500].replace("\n", " ").strip()
        if not sample:
            return None

        predictions = _fasttext_model.predict(sample, k=1)
        labels, confidences = predictions

        if not labels or confidences[0] < 0.5:
            return None    # low confidence → don't guess

        # fastText returns "__label__en" format
        raw_label = labels[0]
        code = raw_label.replace(_LANG_PREFIX, "").strip()
        return code[:2] if len(code) >= 2 else None

    except Exception as e:
        log.debug("fastText detection error", error=str(e))
        return None


def extract_html_lang(html: str) -> str:
    """
    Extract the lang= attribute from the <html> tag.

    Args:
        html: Raw HTML string

    Returns:
        Language tag string (e.g. "en", "fr-CA") or empty string.

    Fast regex-based extraction — no full DOM parse needed.
    """
    match = re.search(
        r"<html[^>]*\slang=['\"]([^'\"]+)['\"]",
        html,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""