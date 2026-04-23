"""
services/processor/pipeline/nlp.py
=====================================
NLP enrichment pipeline — Named Entity Recognition, keyword extraction,
and sentence segmentation using spaCy.

WHY NLP ENRICHMENT?
-------------------
Plain keyword search (BM25) matches exact words. NLP enrichment lets us:
  1. Search by entity type: "find articles about Apple (company)" vs
     "find articles about apple (fruit)"
  2. Filter by entities: "only results mentioning Elon Musk"
  3. Improve ranking: pages with many relevant entities rank higher
  4. Power knowledge graph connections between related documents

WHAT THIS MODULE DOES:
  1. Named Entity Recognition (NER)
     - Identifies people, organisations, locations, products, dates, etc.
     - Counts occurrences (entity mentioned 5× is more important than 1×)
     - Deduplicates case-insensitively ("Apple" and "apple" → same entity)

  2. Keyword extraction (TF-IDF on noun chunks)
     - Extracts noun phrases ("machine learning", "climate change")
     - Ranks by frequency × inverse document frequency heuristic
     - Returns top 20 keywords per document

  3. Sentence count (structural signal for ranking)

SPACY MODEL:
  en_core_web_sm — small English model (12MB)
  Components used: tok2vec, tagger, parser, ner, senter
  Accuracy: good for news/blog content, less good for scientific text

  For production: consider en_core_web_lg (large, 587MB) for better NER
  accuracy, or a domain-specific model fine-tuned on web content.

PERFORMANCE:
  en_core_web_sm processes ~50 pages/second on CPU.
  We use nlp.pipe() for batching when processing multiple documents.
  Single-document path (used here): ~20ms per page.

THREAD SAFETY:
  spaCy models are thread-safe — the same model instance can be used
  from multiple threads/coroutines concurrently after loading.
"""

import time
from collections import Counter
from typing import Optional

from shared.logging import get_logger

log = get_logger("processor.nlp")

# ── Lazy-loaded spaCy model ───────────────────────────────────────────────────
# Loaded once per process — spaCy models are heavy (~12MB), loading takes ~1s.
_nlp = None

# Entity labels we care about (subset of spaCy's full label set)
RELEVANT_ENTITY_LABELS = {
    "PERSON",   "ORG",     "GPE",     "LOC",
    "PRODUCT",  "EVENT",   "WORK_OF_ART",
    "DATE",     "MONEY",   "PERCENT", "CARDINAL",
    "NORP",     "FAC",     "LAW",     "LANGUAGE",
}

# Stop words for keyword extraction (extends spaCy's built-in list)
KEYWORD_STOP_WORDS = {
    "said", "says", "according", "new", "one", "two", "three",
    "year", "years", "time", "day", "days", "week", "month",
    "also", "including", "like", "many", "much", "such", "well",
}

# Max text length to process through spaCy (to limit memory)
MAX_NLP_CHARS = 100_000   # ~15,000 words — plenty for any article


class NLPResult:
    """Result of the NLP enrichment step."""
    __slots__ = [
        "entities", "keywords", "sentence_count",
        "word_count", "nlp_ms", "error"
    ]

    def __init__(self):
        self.entities:       list[dict] = []   # list of Entity.to_dict()
        self.keywords:       list[str]  = []   # top-N keyword phrases
        self.sentence_count: int        = 0
        self.word_count:     int        = 0    # non-stopword tokens
        self.nlp_ms:         int        = 0
        self.error:          str        = ""


def enrich(text: str, title: str = "") -> NLPResult:
    """
    Run the NLP enrichment pipeline on cleaned text.

    Args:
        text:  Cleaned article text (from cleaner.py)
        title: Page title (prepended to text for entity extraction)

    Returns:
        NLPResult with entities, keywords, sentence count, word count.

    Performance:
        ~20–80ms per document depending on text length.
        Uses en_core_web_sm (12MB model, loaded once per process).
    """
    result = NLPResult()
    start = time.monotonic()

    if not text or len(text.strip()) < 10:
        result.error = "Text too short for NLP"
        return result

    # Combine title + text for richer NER (title often has key entities)
    full_text = f"{title}. {text}" if title else text

    # Truncate to avoid memory issues on very long pages
    if len(full_text) > MAX_NLP_CHARS:
        full_text = full_text[:MAX_NLP_CHARS]
        log.debug("Text truncated for NLP", original_len=len(text), truncated_to=MAX_NLP_CHARS)

    try:
        nlp = _get_nlp()
        doc = nlp(full_text)

        # ── Named Entity Recognition ──────────────────────────────────────────
        result.entities = _extract_entities(doc)

        # ── Keyword extraction ────────────────────────────────────────────────
        result.keywords = _extract_keywords(doc)

        # ── Structural signals ────────────────────────────────────────────────
        result.sentence_count = len(list(doc.sents))
        result.word_count = sum(
            1 for token in doc
            if not token.is_stop and not token.is_punct and not token.is_space
            and len(token.text) > 2
        )

    except Exception as e:
        result.error = f"spaCy error: {e}"
        log.error("NLP enrichment failed", error=str(e))

    result.nlp_ms = int((time.monotonic() - start) * 1000)
    log.debug(
        "NLP complete",
        entities=len(result.entities),
        keywords=len(result.keywords),
        sentences=result.sentence_count,
        words=result.word_count,
        nlp_ms=result.nlp_ms,
    )
    return result


def _extract_entities(doc) -> list[dict]:
    """
    Extract and deduplicate named entities from a spaCy Doc.

    Algorithm:
      1. Collect all entity mentions from doc.ents
      2. Filter to relevant label types only
      3. Normalise text (title-case, strip whitespace)
      4. Count occurrences of each unique (text, label) pair
      5. Sort by count descending, return top 50

    Returns:
        List of dicts: [{"text": "OpenAI", "label": "ORG", "count": 3}, ...]
    """
    entity_counts: Counter = Counter()

    for ent in doc.ents:
        if ent.label_ not in RELEVANT_ENTITY_LABELS:
            continue

        # Normalise: strip whitespace, title-case for proper nouns
        text = ent.text.strip()
        if len(text) < 2 or len(text) > 100:
            continue

        # Skip pure numbers unless they're dates
        if text.isdigit() and ent.label_ not in ("DATE", "CARDINAL", "MONEY"):
            continue

        key = (text, ent.label_)
        entity_counts[key] += 1

    # Convert to list of dicts, sorted by count
    entities = [
        {"text": text, "label": label, "count": count}
        for (text, label), count in entity_counts.most_common(50)
    ]
    return entities


def _extract_keywords(doc, top_n: int = 20) -> list[str]:
    """
    Extract top keywords using noun chunk frequency.

    Algorithm:
      1. Extract all noun chunks from the document
      2. Filter out stop words, very short/long chunks
      3. Normalise to lowercase
      4. Count frequency
      5. Return top_n most frequent phrases

    This is a simplified TF (term frequency) approach.
    For production, add IDF (inverse document frequency) across the corpus.

    Returns:
        List of keyword strings, most frequent first.
        Example: ["machine learning", "neural network", "training data", ...]
    """
    chunk_counts: Counter = Counter()

    for chunk in doc.noun_chunks:
        # Get the root of the chunk (skip determiners like "the", "a")
        root_text = chunk.root.text.lower()

        # Skip if root is a stop word, very short, or a stop keyword
        if (chunk.root.is_stop
                or len(root_text) < 3
                or root_text in KEYWORD_STOP_WORDS):
            continue

        # Use the full chunk text (normalised)
        phrase = chunk.text.lower().strip()

        # Skip chunks that are too long (probably not a keyword)
        word_count = len(phrase.split())
        if word_count > 4 or len(phrase) > 50:
            continue

        # Skip chunks that start with a stop word
        first_word = phrase.split()[0]
        if first_word in {"the", "a", "an", "this", "that", "these", "those", "its"}:
            # Use the remainder without the article
            words = phrase.split()[1:]
            if words:
                phrase = " ".join(words)
            else:
                continue

        chunk_counts[phrase] += 1

    # Also add single important nouns that might have been missed
    for token in doc:
        if (token.pos_ in ("NOUN", "PROPN")
                and not token.is_stop
                and len(token.text) > 3
                and token.text.lower() not in KEYWORD_STOP_WORDS):
            chunk_counts[token.text.lower()] += 1

    return [kw for kw, _ in chunk_counts.most_common(top_n)]


def _get_nlp():
    """
    Lazy-load the spaCy model.
    Loads once per process; subsequent calls return the cached model.
    """
    global _nlp
    if _nlp is None:
        import spacy
        from shared.config import settings

        log.info("Loading spaCy model", model=settings.spacy_model)
        load_start = time.monotonic()
        _nlp = spacy.load(
            settings.spacy_model,
            # Disable unused pipeline components for speed
            disable=["attribute_ruler", "lemmatizer"],
        )
        load_ms = int((time.monotonic() - load_start) * 1000)
        log.info("spaCy model loaded", model=settings.spacy_model, load_ms=load_ms)
    return _nlp