# Phase 3 — Processing Pipeline
## PRN: 22510103 | Real-Time Web Indexing System

> **Goal of Phase 3:** Transform raw HTML into a fully enriched, search-ready
> `IndexedDocument` — running HTML cleaning, language detection, NLP enrichment,
> semantic embedding, metadata extraction, and ranking signal computation —
> all within the **25–45 second** window from original discovery.

---

## Table of Contents

1. [What Phase 3 Does](#1-what-phase-3-does)
2. [Architecture of the Processing Layer](#2-architecture-of-the-processing-layer)
3. [Pipeline Stages — Deep Dive](#3-pipeline-stages--deep-dive)
   - 3.1 [HTML Cleaner — Trafilatura](#31-html-cleaner--trafilatura)
   - 3.2 [Language Detection](#32-language-detection)
   - 3.3 [NLP Enrichment — spaCy](#33-nlp-enrichment--spacy)
   - 3.4 [Semantic Embedding — sentence-transformers](#34-semantic-embedding--sentence-transformers)
   - 3.5 [Metadata Extraction — extruct](#35-metadata-extraction--extruct)
   - 3.6 [Ranking Signals](#36-ranking-signals)
4. [Data Models](#4-data-models)
5. [Configuration Reference](#5-configuration-reference)
6. [File Reference](#6-file-reference)
7. [How to Run Phase 3](#7-how-to-run-phase-3)
8. [How to Test Phase 3](#8-how-to-test-phase-3)
9. [Metrics & Observability](#9-metrics--observability)
10. [Common Issues & Fixes](#10-common-issues--fixes)
11. [What Phase 4 Receives](#11-what-phase-4-receives)

---

## 1. What Phase 3 Does

Phase 3 is the intelligence layer. It takes the raw HTML from Phase 2
and transforms it into a fully enriched, search-ready document.

**Input:**
- Redis Stream `STREAM:crawl_complete` — crawl events published by Phase 2
- MinIO `raw-pages` bucket — raw HTML retrieved via `minio_key` from the event

**Output:**
- `IndexedDocument` published to Redis Stream `STREAM:docs_ready`
- Phase 4 (Indexer) reads this stream and writes to Meilisearch + Qdrant + PostgreSQL

**Time budget:** 25 to 45 seconds (crawl complete → document ready)

### Why six pipeline stages?

Each stage adds a different dimension of information:

| Stage | Library | Time | Adds |
|---|---|---|---|
| HTML cleaning | Trafilatura | ~100ms | Clean text, title, author, date |
| Language detection | fastText + regex | ~2ms | Language code (en/fr/de/…) |
| NLP enrichment | spaCy | ~40ms | Named entities, keywords, word count |
| Semantic embedding | sentence-transformers | ~50ms | 384d meaning vector |
| Metadata extraction | extruct | ~15ms | og:title, Schema.org, canonical URL |
| Ranking signals | pure Python | ~1ms | Freshness, quality, authority scores |
| **Total** | | **~210ms** | **Complete IndexedDocument** |

At 210ms per document, one processor worker handles ~5 docs/second.
With 2 workers: 10 docs/second = 600 docs/minute — plenty for MVP.

---

## 2. Architecture of the Processing Layer

```
Redis STREAM:crawl_complete
         │
   XREADGROUP (consumer group: processor_group)
         │
    ┌────┴────────────────────────────────────────────────────┐
    │                 PROCESSOR SERVICE                        │
    │              (asyncio event loop)                        │
    │                                                          │
    │  ┌─────────────────────────────────────────────────┐   │
    │  │  _process_message()                              │   │
    │  │                                                  │   │
    │  │  1. Parse CrawlEvent JSON                        │   │
    │  │  2. Skip non-success / duplicate events          │   │
    │  │  3. Fetch HTML from MinIO (async)                │   │
    │  │  4. run_in_executor → _run_pipeline()            │   │
    │  │                         (thread pool)            │   │
    │  │                              │                   │   │
    │  └──────────────────────────────┼───────────────────┘   │
    │                                 │                        │
    │    ┌────────────────────────────▼──────────────────┐    │
    │    │           _run_pipeline() — thread pool        │    │
    │    │                                                │    │
    │    │  ①  clean_html(html, url)                     │    │
    │    │         └─ Trafilatura → CleanResult            │    │
    │    │                                                │    │
    │    │  ②  detect_language(text, html_lang, header)  │    │
    │    │         └─ fastText / HTML attr → "en"         │    │
    │    │                                                │    │
    │    │  ③  enrich(text, title)                       │    │
    │    │         └─ spaCy → entities, keywords          │    │
    │    │                                                │    │
    │    │  ④  embed(title, text)                        │    │
    │    │         └─ SentBERT → [f32 × 384]              │    │
    │    │                                                │    │
    │    │  ⑤  extract_metadata(html, url)               │    │
    │    │         └─ extruct → og:, Schema.org           │    │
    │    │                                                │    │
    │    │  ⑥  compute_signals(…)                        │    │
    │    │         └─ math → freshness, quality            │    │
    │    │                                                │    │
    │    │  ⑦  Assemble IndexedDocument                  │    │
    │    └────────────────────────────────────────────────┘    │
    │                        │                                  │
    │  XADD STREAM:docs_ready  ← IndexedDocument.to_json()    │
    │  XACK crawl_complete msg_id                              │
    └──────────────────────────────────────────────────────────┘
                             │
            Redis STREAM:docs_ready → Phase 4 (Indexer)
```

### Why `run_in_executor` for the pipeline?

The pipeline is CPU-bound (NLP, matrix operations). Running it directly in the
asyncio event loop would block all other coroutines, including the Redis XREADGROUP
consumer and XADD publisher. By using `run_in_executor`, the pipeline runs in a
Python thread pool, leaving the event loop free for I/O.

---

## 3. Pipeline Stages — Deep Dive

### 3.1 HTML Cleaner — Trafilatura

**File:** `services/processor/pipeline/cleaner.py`

#### What it does

Takes the raw HTML fetched by the crawler and extracts only the meaningful
article content — discarding nav menus, ads, footers, sidebars, and other
boilerplate that would pollute search results.

#### How Trafilatura works

Trafilatura scores every block of text in the HTML using four signals:

```
Text density    = text_chars / total_chars_in_block
                  High density = actual content (not a nav menu)

Link density    = link_chars / text_chars
                  Low link density = article text (not a list of links)

Block position  = distance from top of page
                  Main content tends to be in the middle of the DOM tree

Block length    = number of words in the block
                  Short blocks (<10 words) are usually UI elements
```

Blocks above a threshold are kept; below are discarded. Remaining blocks
are joined into the final clean text.

#### Output fields

```python
result.title      # "How ML is Transforming Web Search"
result.text       # Full clean article text (no boilerplate)
result.summary    # First ~300 chars (smart sentence boundary)
result.author     # "Jane Smith" (if byline detected)
result.date       # "2024-01-15" (if date found in content)
result.language   # "en" (from Trafilatura's detection)
result.word_count # 487
result.success    # True
result.clean_ms   # 87
```

#### When it fails

```python
result.success = False  when:
  - HTML is < 100 chars (not a real page)
  - Extracted text < 20 words (login wall, empty SPA shell)
  - Trafilatura returns None (some paywalled pages)
```

When `success=False`, the pipeline returns `None` and the document is
**not** indexed. The crawl event is ACK'd to prevent reprocessing.

---

### 3.2 Language Detection

**File:** `services/processor/pipeline/language.py`

#### Three-layer strategy

```
Priority 1: HTML lang= attribute          → most reliable
  <html lang="fr-CA">  → "fr"
  <html lang="en-US">  → "en"
  BCP 47 tag → take primary subtag, normalise to 2-char ISO 639-1

Priority 2: Content-Language HTTP header  → fairly reliable
  Content-Language: de → "de"

Priority 3: fastText LID-176 model        → text classification fallback
  Sample first 500 chars of clean text
  Returns __label__en → "en"
  Confidence < 0.5 → don't guess

Default: "en"  → safe fallback for English-majority web
```

#### fastText LID model

- **Model:** `lid.176.ftz` (Facebook Research, 917KB compressed)
- **Languages:** 176 supported
- **Accuracy:** ~98% on CommonCrawl
- **Speed:** < 1ms per document (tiny model, runs on CPU)
- **Downloaded at:** Docker build time into `/app/models/lid.176.ftz`

The model is a lazy singleton — loaded on first use, cached thereafter.

---

### 3.3 NLP Enrichment — spaCy

**File:** `services/processor/pipeline/nlp.py`

#### Named Entity Recognition (NER)

spaCy's `en_core_web_sm` model identifies 18 entity types. We keep:

| Label | Meaning | Example |
|---|---|---|
| PERSON | People | "Sundar Pichai", "Jane Smith" |
| ORG | Companies, agencies | "Google", "OpenAI", "NASA" |
| GPE | Countries, cities | "San Francisco", "Germany" |
| PRODUCT | Products | "GPT-4", "iPhone 15" |
| EVENT | Named events | "World Cup 2024" |
| DATE | Dates | "January 2024", "last year" |
| MONEY | Monetary values | "$1.2 billion" |
| NORP | Nationalities, groups | "European", "Republicans" |

#### Entity deduplication

The same entity mentioned multiple times is deduplicated by `(text, label)` key.
A counter tracks how many times each entity appeared:

```python
# Input mentions: "Google" × 4, "Alphabet" × 2, "Sundar Pichai" × 1
# Output:
[
  {"text": "Google",        "label": "ORG",    "count": 4},
  {"text": "Alphabet",      "label": "ORG",    "count": 2},
  {"text": "Sundar Pichai", "label": "PERSON", "count": 1},
]
```

This count is used as a ranking signal: an entity mentioned 10 times is more
central to the document than one mentioned once.

#### Keyword extraction

Keywords are extracted from spaCy noun chunks:

```
"machine learning algorithms" → keyword (3-word noun phrase)
"neural network"              → keyword (compound noun)
"the model"                   → SKIPPED (starts with "the", article stripped)
"it"                          → SKIPPED (pronoun, too short)
```

Top 20 keywords returned, sorted by frequency.

#### Model loading strategy

```python
# Disabled components (not needed — saves memory and time):
disable=["attribute_ruler", "lemmatizer"]

# spaCy pipeline we use:
# tok2vec → tagger → parser → senter → ner
```

Model loaded once per process at warmup (~1s), cached in `_nlp` global.
Thread-safe — multiple coroutines can call `enrich()` concurrently.

---

### 3.4 Semantic Embedding — sentence-transformers

**File:** `services/processor/pipeline/embedder.py`

#### What embeddings are

An embedding maps text to a point in a 384-dimensional vector space.
Semantically similar texts end up close together; unrelated texts are far apart.

```
"machine learning"     → [0.12, -0.34, 0.87, ...]  (384 floats)
"deep learning"        → [0.14, -0.31, 0.85, ...]  (very similar direction)
"French cuisine"       → [-0.67, 0.23, -0.12, ...] (very different direction)
```

Cosine similarity between two L2-normalised vectors = their dot product:
```python
similarity = sum(a * b for a, b in zip(vec1, vec2))
# 1.0 = identical meaning, 0.0 = unrelated, -1.0 = opposite meaning
```

#### Why all-MiniLM-L6-v2

```
Model size:    22MB
Dimensions:    384 (vs 768 for BERT-base)
CPU speed:     ~50ms per document
GPU speed:     ~5ms per document
SBERT rank:    Top-5 for speed/quality tradeoff on MTEB benchmark
Training data: 1B+ sentence pairs (MS MARCO, NLI, Reddit, etc.)
```

#### Input construction

```python
input_text = f"{title[:200]}. {text[:2000]}"
```

Prepending the title boosts it in the vector (titles are the most
representative text for what a page is about). We cap at 2000 chars
because the model has a 256-token context window — additional text
beyond ~2000 chars doesn't improve the embedding quality meaningfully.

#### L2 normalisation

```python
model.encode(text, normalize_embeddings=True)
```

With `normalize_embeddings=True`, every vector has L2 norm = 1.0.
This means:
- Cosine similarity = dot product (faster, simpler)
- All vectors lie on a unit hypersphere
- Distance is purely semantic (angle), not magnitude

Qdrant stores these normalised vectors and uses cosine distance for ANN search.

---

### 3.5 Metadata Extraction — extruct

**File:** `services/processor/pipeline/metadata.py`

#### Three metadata formats

**OpenGraph (`<meta property="og:*">`):**
```html
<meta property="og:title"       content="Article Title">
<meta property="og:description" content="Short description">
<meta property="og:image"       content="https://example.com/img.jpg">
<meta property="og:type"        content="article">
```

**Schema.org JSON-LD (`<script type="application/ld+json">`):**
```json
{
  "@type": "Article",
  "headline": "Article Title",
  "author": {"@type": "Person", "name": "Jane Smith"},
  "datePublished": "2024-01-15T10:00:00Z",
  "description": "..."
}
```

**Canonical URL (`<link rel="canonical">`):**
```html
<link rel="canonical" href="https://example.com/articles/article-1">
```

#### Priority and fallback

```
og_title:         og:title → schema.org name/headline → ""
og_description:   og:description → schema.org description → ""
og_image:         og:image → schema.org image → ""
author:           schema.org author.name → og:author → trafilatura author → ""
date_published:   schema.org datePublished → trafilatura date → ""
schema_type:      schema.org @type → microdata @type → ""
canonical_url:    <link rel="canonical"> → og:url → ""
```

#### Why this matters for search

- `date_published` → freshness score (newer content ranked higher)
- `author` → entity for NER, displayed in search results
- `og_title` / `og_description` → richer search result snippets
- `schema_type` → type-aware indexing (Article vs Product vs Event)
- `canonical_url` → prevents duplicate indexing of same content at different URLs

---

### 3.6 Ranking Signals

**File:** `services/processor/pipeline/signals.py`

#### Freshness score

```python
# Exponential decay: score = exp(-decay * age_days)
# Half-life = 30 days (score halves every ~30 days)

DECAY = ln(2) / 30    # ≈ 0.0231 per day

freshness = exp(-DECAY * age_days)
```

| Age | Freshness Score |
|---|---|
| Just indexed | 1.00 |
| 1 day | 0.98 |
| 7 days | 0.86 |
| 30 days | 0.50 |
| 60 days | 0.25 |
| 120 days | 0.06 |

Uses `date_published` from Schema.org when available (most accurate).
Falls back to `crawled_at` timestamp.

#### Content quality score

```
+0.40  word_count signal (log-scaled: 2000 words → max)
+0.15  title present and meaningful
+0.20  structured data (og: or schema.org)
+0.10  author name extracted
+0.10  publication date extracted
+0.05  language detected confidently
───────
1.00   maximum possible score
```

#### How signals are used in Phase 5 (API)

```python
final_score = (
    0.40 * bm25_score          # keyword relevance
  + 0.40 * vector_similarity   # semantic relevance
  + 0.10 * freshness_score     # how recent is this?
  + 0.10 * content_quality     # how good is the content?
)
```

The 40/40/10/10 weights are configurable. In production, a learning-to-rank
model (LightGBM) would learn optimal weights from click data.

---

## 4. Data Models

### IndexedDocument

The complete enriched document, produced by Phase 3 and consumed by Phase 4.

```python
@dataclass
class IndexedDocument:
    # ── Identity ──────────────────────────────────────────────────────────────
    url:             str         # "https://example.com/article/1"
    domain:          str         # "example.com"
    content_hash:    str         # FNV-64a hex (from Phase 2)
    minio_key:       str         # MinIO object path (for reprocessing)

    # ── Extracted content ─────────────────────────────────────────────────────
    title:           str         # "How ML is Transforming Web Search"
    clean_text:      str         # full boilerplate-stripped article text
    summary:         str         # first ~300 chars (sentence-boundary aware)
    language:        str         # "en"
    author:          str         # "Jane Smith"
    published_at:    str         # "2024-01-15T10:00:00Z"

    # ── NLP enrichment ────────────────────────────────────────────────────────
    entities:        list[dict]  # [{"text":"Google","label":"ORG","count":4}]
    keywords:        list[str]   # ["machine learning", "neural network", ...]
    word_count:      int         # 487

    # ── Semantic search ───────────────────────────────────────────────────────
    embedding:       list[float] # 384-dim L2-normalised vector

    # ── Structured metadata ───────────────────────────────────────────────────
    og_title:        str         # OpenGraph og:title
    og_description:  str         # OpenGraph og:description
    og_image:        str         # OpenGraph og:image URL
    schema_type:     str         # Schema.org @type: "Article"
    canonical_url:   str         # <link rel="canonical"> URL

    # ── Ranking signals ───────────────────────────────────────────────────────
    domain_authority: float      # 0.0–1.0 (from domain registry)
    freshness_score:  float      # 0.0–1.0 (1.0 = just published)

    # ── Timing (SLO tracking) ─────────────────────────────────────────────────
    discovered_at:   float       # Unix ts — Phase 1 discovery
    crawled_at:      float       # Unix ts — Phase 2 fetch complete
    processed_at:    float       # Unix ts — Phase 3 pipeline complete
    indexed_at:      float       # Unix ts — Phase 4 write complete (set by indexer)

    # ── Provenance ────────────────────────────────────────────────────────────
    discovery_source: str        # "websub" | "sitemap" | "ct_log" | "link"
    crawl_depth:      int        # link hops from seed (0 = seed)
    fetch_ms:         int        # Phase 2 HTTP fetch time
    process_ms:       int        # Phase 3 pipeline time
```

### Computed properties

```python
doc.e2e_latency_ms    # discovered_at → processed_at in milliseconds
doc.display_title     # og_title → title → domain (best available)
doc.display_description # og_description → summary
doc.has_embedding     # len(embedding) == 384
doc.top_entities(n)   # top N entities sorted by count
```

---

## 5. Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model name |
| `SPACY_MODEL` | `en_core_web_sm` | spaCy model for NLP |
| `PROCESSOR_WORKERS` | `4` | Number of worker processes (MVP: 2) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO server address |
| `MINIO_BUCKET` | `raw-pages` | Bucket containing raw HTML |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

### Tuning for the 60s SLO

The processor is the most CPU-intensive stage. If latency is too high:

| Adjustment | Effect | Trade-off |
|---|---|---|
| Increase `PROCESSOR_WORKERS` | More parallel docs | More RAM, more CPU |
| Use GPU for embeddings | 10× faster embed | Requires CUDA setup |
| Switch to `en_core_web_sm` | Faster NLP | Lower NER accuracy |
| Reduce `MAX_EMBED_CHARS` (embedder.py) | Faster embed | Lower semantic accuracy |
| Disable fastText (language.py) | Faster language detect | Relies on HTML attr only |

For MVP, 2 workers on a laptop comfortably processes 10+ docs/second —
more than sufficient for the demo.

---

## 6. File Reference

```
services/processor/
├── __init__.py
├── Dockerfile                          Python 3.11 + PyTorch + spaCy + Playwright
├── requirements.txt                    All Python dependencies with pinned versions
├── main.py                             ★ Service orchestrator — Redis stream consumer,
│                                         MinIO fetcher, pipeline runner, doc publisher
│
├── pipeline/
│   ├── __init__.py
│   ├── cleaner.py                      ★ Trafilatura HTML → clean text
│   ├── language.py                     ★ fastText + HTML attr → ISO 639-1 code
│   ├── nlp.py                          ★ spaCy → entities + keywords
│   ├── embedder.py                     ★ sentence-transformers → 384d vector
│   ├── metadata.py                     ★ extruct → og:, Schema.org, canonical
│   └── signals.py                      ★ freshness + quality score computation
│
└── tests/
    ├── __init__.py
    ├── test_pipeline.py                ★ 50+ unit tests across all pipeline stages
    └── fixtures/
        └── sample_pages/
            ├── article.html            Rich article with Schema.org + og: tags
            ├── spa_shell.html          Empty React SPA shell (should fail gracefully)
            └── minimal.html            Short French page for language detection

shared/models/
├── indexed_document.py                 ★ IndexedDocument dataclass — Phase 3 output
└── crawled_page.py                     CrawledPage — Phase 3 input (from Phase 2)

★ = files you must understand to work on Phase 3
```

---

## 7. How to Run Phase 3

### Prerequisites

```bash
# Start all required infrastructure
docker compose up -d redis minio postgres

# Phase 2 (crawler) must be running to produce crawl events
docker compose up -d crawler
```

### Start the processor

```bash
# Docker (recommended — models pre-downloaded in image)
docker compose up processor

# Local Python (for development, after pip install -r requirements.txt)
source .venv/bin/activate
cd services/processor
LOG_FORMAT=text python main.py
```

### Expected startup log

```
INFO  processor  | Processor service starting pid=1
INFO  processor  | Redis connected
INFO  processor  | Consumer group created group=processor_group
INFO  processor  | Warming up ML models...
INFO  processor.nlp      | Loading spaCy model model=en_core_web_sm
INFO  processor.nlp      | spaCy model loaded load_ms=1203
INFO  processor.embedder | Loading embedding model model=all-MiniLM-L6-v2
INFO  processor.embedder | Embedding model ready dimensions=384 load_ms=487
INFO  processor  | ML models ready
INFO  processor  | Processor worker running stream=STREAM:crawl_complete
```

### Verify documents flow through

```bash
# Watch the output stream grow
watch -n2 "docker exec indexer_redis redis-cli XLEN STREAM:docs_ready"

# Read the latest processed document
docker exec indexer_redis redis-cli XREVRANGE STREAM:docs_ready + - COUNT 1
```

---

## 8. How to Test Phase 3

### Run all tests (no external services needed)

```bash
# Activate virtual env with dependencies installed
source .venv/bin/activate

# Run all pipeline tests
pytest services/processor/tests/test_pipeline.py -v

# Run specific stage tests
pytest services/processor/tests/test_pipeline.py -v -k "cleaner"
pytest services/processor/tests/test_pipeline.py -v -k "embedder"
pytest services/processor/tests/test_pipeline.py -v -k "signals"
```

### Expected output

```
PASSED test_pipeline.py::TestCleaner::test_extracts_article_text
PASSED test_pipeline.py::TestCleaner::test_extracts_title
PASSED test_pipeline.py::TestCleaner::test_strips_navigation_boilerplate
PASSED test_pipeline.py::TestCleaner::test_word_count_positive
PASSED test_pipeline.py::TestCleaner::test_spa_shell_fails_gracefully
PASSED test_pipeline.py::TestLanguageDetection::test_detects_english_from_html_attr
PASSED test_pipeline.py::TestLanguageDetection::test_strips_region_from_lang_tag
PASSED test_pipeline.py::TestNLPEnrichment::test_finds_org_entities
PASSED test_pipeline.py::TestNLPEnrichment::test_entities_sorted_by_count
PASSED test_pipeline.py::TestEmbedder::test_embedding_has_384_dimensions
PASSED test_pipeline.py::TestEmbedder::test_embedding_is_l2_normalised
PASSED test_pipeline.py::TestEmbedder::test_similar_texts_have_similar_embeddings
PASSED test_pipeline.py::TestMetadataExtraction::test_extracts_og_title
PASSED test_pipeline.py::TestMetadataExtraction::test_extracts_schema_type
PASSED test_pipeline.py::TestMetadataExtraction::test_extracts_author
PASSED test_pipeline.py::TestRankingSignals::test_freshness_just_indexed_is_near_1
PASSED test_pipeline.py::TestRankingSignals::test_freshness_old_page_is_low
PASSED test_pipeline.py::TestRankingSignals::test_quality_high_for_rich_content
PASSED test_pipeline.py::TestIndexedDocument::test_json_roundtrip
PASSED test_pipeline.py::TestIndexedDocument::test_e2e_latency_ms

50 passed in 4.32s
```

---

## 9. Metrics & Observability

Prometheus metrics exposed on **port 9103**:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `indexer_docs_processed_total` | Counter | — | Documents completing full pipeline |
| `indexer_process_duration_seconds` | Histogram | — | Pipeline wall-clock time |
| `indexer_embed_duration_seconds` | Histogram | — | Embedding generation time |
| `indexer_process_errors_total` | Counter | `stage` | Failures per pipeline stage |
| `indexer_e2e_latency_seconds` | Histogram | — | Discovery → processed (SLO metric) |
| `indexer_slo_breached_total` | Counter | — | Docs taking > 60s end-to-end |

### Key Grafana queries

```promql
# Processing throughput (docs/min)
rate(indexer_docs_processed_total[1m]) * 60

# P95 pipeline latency
histogram_quantile(0.95, rate(indexer_process_duration_seconds_bucket[5m]))

# SLO pass rate (% of docs indexed within 60s)
1 - (
  rate(indexer_slo_breached_total[5m]) /
  rate(indexer_docs_processed_total[5m])
)

# E2E latency P50 / P95
histogram_quantile(0.50, rate(indexer_e2e_latency_seconds_bucket[5m]))
histogram_quantile(0.95, rate(indexer_e2e_latency_seconds_bucket[5m]))
```

---

## 10. Common Issues & Fixes

### "spaCy model not found: en_core_web_sm"

```bash
# Fix: download the model
python -m spacy download en_core_web_sm

# Or in Docker:
docker compose build processor   # Dockerfile includes model download
```

### "sentence-transformers model download on every restart"

**Cause:** Model cache directory not persisted.
**Fix:** Mount a Docker volume for the model cache:
```yaml
# In docker-compose.yml, under processor:
volumes:
  - ./models_cache:/app/models
```

Or ensure `SENTENCE_TRANSFORMERS_HOME=/app/models` is set and the Dockerfile
pre-downloads the model (already done in our Dockerfile).

### "No messages in STREAM:crawl_complete"

**Cause:** Phase 2 (crawler) not running, or queue empty.
```bash
# Check crawler is running
docker compose ps crawler

# Check stream length
docker exec indexer_redis redis-cli XLEN STREAM:crawl_complete

# Seed URLs and start crawler
python scripts/seed_urls.py --count 10
docker compose up -d crawler
```

### "Pipeline returns None for all documents"

**Cause A:** Trafilatura can't extract content (all pages are SPA shells).
- Verify Phase 2 is using Playwright for JS pages
- Check: `docker compose logs crawler | grep playwright`

**Cause B:** All pages are very short (< 20 words after cleaning).
```bash
# Check logs for "Clean failed" messages
docker compose logs processor | grep "Clean failed"
```

### "Embedding model is slow (> 500ms per doc)"

**Cause:** Running on CPU without optimisation.
```python
# In embedder.py, the model already uses float32 — this is optimal for CPU.
# For faster CPU inference, install the optimised ONNX runtime:
pip install optimum onnxruntime
# Then export model: optimum-cli export onnx --model all-MiniLM-L6-v2 ./onnx
```

For the demo, 50ms per embedding is fine. Production uses GPU (5ms).

---

## 11. What Phase 4 Receives

When Phase 3 completes, an `IndexedDocument` is published to
`STREAM:docs_ready`. Here's exactly what Phase 4 (Indexer) receives:

```python
doc = IndexedDocument(
    url          = "https://techcrunch.com/2024/01/15/ai-search",
    domain       = "techcrunch.com",
    content_hash = "a3f9c2d8e1b74f56",

    title        = "How AI is Reshaping Web Search",
    clean_text   = "The landscape of web search has changed dramatically...",
    summary      = "The landscape of web search has changed dramatically over the past decade.",
    language     = "en",
    author       = "Jane Smith",
    published_at = "2024-01-15T10:00:00Z",

    entities     = [
        {"text": "Google",  "label": "ORG",    "count": 4},
        {"text": "OpenAI",  "label": "ORG",    "count": 3},
        {"text": "GPT-4",   "label": "PRODUCT","count": 2},
    ],
    keywords     = ["machine learning", "semantic search", "neural network", ...],
    word_count   = 487,

    embedding    = [0.0823, -0.1204, 0.3401, ...],  # 384 floats

    og_title        = "How AI is Reshaping Web Search",
    og_description  = "A deep dive into modern search technology",
    og_image        = "https://techcrunch.com/img/ai-search.jpg",
    schema_type     = "Article",
    canonical_url   = "https://techcrunch.com/2024/01/15/ai-search",

    domain_authority = 0.92,
    freshness_score  = 0.98,

    discovered_at = 1705316400.0,
    crawled_at    = 1705316408.3,
    processed_at  = 1705316408.6,   # e2e_latency_ms ≈ 8,600ms so far

    discovery_source = "websub",
    crawl_depth      = 0,
    fetch_ms         = 342,
    process_ms       = 287,
)
```

**Phase 3 guarantees to Phase 4:**
- `clean_text` is non-empty and >= 20 words (no SPA shells, no login walls)
- `embedding` is a 384-dimensional L2-normalised vector (or empty on failure)
- `language` is a valid ISO 639-1 code
- `freshness_score` and `domain_authority` are in [0.0, 1.0]
- `discovered_at` is preserved from Phase 1 for end-to-end SLO tracking
- `entities` list is sorted by count descending, deduplicated by (text, label)

---

*Phase 3 documentation | PRN: 22510103 | Version 1.0*