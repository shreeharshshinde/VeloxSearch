"""
services/processor/main.py
============================
Processor Service — Celery worker orchestrator.

This is the brain of Phase 3. It:
  1. Consumes crawl_complete events from Redis Stream STREAM:crawl_complete
  2. Fetches raw HTML from MinIO
  3. Runs the full NLP pipeline:
       clean_html()   → Trafilatura boilerplate removal
       detect_language() → fastText language identification
       enrich()       → spaCy NER + keyword extraction
       embed()        → sentence-transformers 384d vector
       extract_metadata() → extruct OpenGraph + Schema.org
       compute_signals()  → freshness, quality, authority scores
  4. Assembles all outputs into an IndexedDocument
  5. Publishes to Redis Stream STREAM:docs_ready → Phase 4 (Indexer)
  6. Publishes raw HTML + link events → Phase 1 (link extractor)

WORKER MODEL:
  We use Celery with a Redis broker. Each Celery worker process handles
  one document at a time (CPU-bound NLP work — no benefit to async I/O here).
  Scale by increasing PROCESSOR_WORKERS (number of parallel processes).

  For MVP: 2 workers (fits on a laptop)
  For production: 1 worker per CPU core, GPU for embeddings

TIMING:
  clean_html:       ~50–200ms
  detect_language:  ~1–5ms
  enrich (spaCy):   ~20–80ms
  embed:            ~50ms (CPU)
  extract_metadata: ~5–30ms
  signals:          ~1ms
  Total:            ~130–370ms → fits comfortably within Phase 3 budget

STREAM CONSUMPTION:
  We use Redis XREADGROUP (consumer group) so multiple processor workers
  can share the stream without duplicate processing.
  Each message is ACK'd after successful processing.
  Failed messages are retried up to 3 times before being moved to a
  dead-letter set (STREAM:dlq).
"""

import asyncio
import json
import os
import sys
import time
import signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import redis.asyncio as aioredis
from minio import Minio

from shared.config import settings
from shared.logging import get_logger
from shared.metrics import METRICS, start_metrics_server
from shared.models.crawled_page import CrawlStatus
from shared.models.indexed_document import IndexedDocument

from pipeline.cleaner  import clean_html
from pipeline.language import detect_language, extract_html_lang
from pipeline.nlp      import enrich
from pipeline.embedder import embed
from pipeline.metadata import extract_metadata
from pipeline.signals  import compute_signals

log = get_logger("processor")

# ── Redis Stream config ────────────────────────────────────────────────────────
CRAWL_COMPLETE_STREAM = "STREAM:crawl_complete"
DOCS_READY_STREAM     = "STREAM:docs_ready"
DLQ_KEY               = "STREAM:dlq"
CONSUMER_GROUP        = "processor_group"
CONSUMER_NAME         = f"processor_{os.getpid()}"   # unique per worker process
MAX_RETRIES           = 3
BATCH_SIZE            = 5      # process up to 5 docs at once per poll

# ── MinIO client (module-level singleton) ─────────────────────────────────────
_minio_client: Minio | None = None


def get_minio() -> Minio:
    global _minio_client
    if _minio_client is None:
        _minio_client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _minio_client


# ── Main service ───────────────────────────────────────────────────────────────

class ProcessorService:
    """
    Consumes crawl events from Redis Stream and runs the NLP pipeline.
    """

    def __init__(self):
        self._redis: aioredis.Redis | None = None
        self._running = False

    async def start(self) -> None:
        log.info("Processor service starting", pid=os.getpid())

        # ── Redis ─────────────────────────────────────────────────────────────
        self._redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,
        )
        await self._redis.ping()
        log.info("Redis connected")

        # ── Consumer group ────────────────────────────────────────────────────
        await self._ensure_consumer_group()

        # ── Prometheus metrics ────────────────────────────────────────────────
        start_metrics_server("processor")

        # ── Warm up ML models (load into memory before first request) ─────────
        log.info("Warming up ML models...")
        await asyncio.get_event_loop().run_in_executor(None, _warmup_models)
        log.info("ML models ready")

        self._running = True
        log.info(
            "Processor worker running",
            stream=CRAWL_COMPLETE_STREAM,
            group=CONSUMER_GROUP,
            consumer=CONSUMER_NAME,
        )

        # ── Main processing loop ──────────────────────────────────────────────
        await self._consume_loop()

    async def stop(self) -> None:
        self._running = False
        if self._redis:
            await self._redis.aclose()
        log.info("Processor stopped")

    async def _ensure_consumer_group(self) -> None:
        """Create the Redis consumer group if it doesn't exist."""
        try:
            await self._redis.xgroup_create(
                CRAWL_COMPLETE_STREAM, CONSUMER_GROUP, id="0", mkstream=True
            )
            log.info("Consumer group created", group=CONSUMER_GROUP)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                log.debug("Consumer group already exists")
            else:
                raise

    async def _consume_loop(self) -> None:
        """
        Main loop: read crawl events, process documents, publish to indexer.
        """
        while self._running:
            try:
                # Read up to BATCH_SIZE pending messages
                messages = await self._redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={CRAWL_COMPLETE_STREAM: ">"},
                    count=BATCH_SIZE,
                    block=5000,    # block 5s if no messages (saves CPU)
                )

                if not messages:
                    continue

                for _stream_name, records in messages:
                    for msg_id, fields in records:
                        await self._process_message(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Consume loop error", error=str(e))
                await asyncio.sleep(1)

    async def _process_message(self, msg_id: str, fields: dict) -> None:
        """
        Process a single crawl_complete event.

        1. Parse the CrawlEvent JSON
        2. Skip non-success events (robots_block, http_error, duplicate)
        3. Fetch HTML from MinIO
        4. Run NLP pipeline in thread pool (CPU-bound)
        5. Publish IndexedDocument to STREAM:docs_ready
        6. ACK message in consumer group
        """
        start_time = time.monotonic()

        try:
            # ── Parse event ───────────────────────────────────────────────────
            raw_data = fields.get("data", "{}")
            event = json.loads(raw_data)

            url    = event.get("url", "")
            status = event.get("status", "")
            domain = event.get("domain", "")

            log.debug("Processing event", url=url, status=status, msg_id=msg_id)

            # ── Skip non-success crawls ────────────────────────────────────────
            if status != CrawlStatus.SUCCESS.value:
                log.debug("Skipping non-success event", url=url, status=status)
                await self._ack(msg_id)
                return

            # ── Fetch HTML from MinIO ─────────────────────────────────────────
            minio_key = event.get("minio_key", "")
            if not minio_key:
                log.warning("No minio_key in event", url=url)
                await self._ack(msg_id)
                return

            html = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_from_minio, minio_key
            )
            if not html:
                log.warning("Empty HTML from MinIO", url=url, key=minio_key)
                await self._ack(msg_id)
                return

            # ── Run NLP pipeline in thread pool ───────────────────────────────
            # All pipeline stages are CPU-bound — run in executor to avoid
            # blocking the event loop for other coroutines.
            doc = await asyncio.get_event_loop().run_in_executor(
                None,
                _run_pipeline,
                url, domain, html, event,
            )

            if doc is None:
                log.warning("Pipeline returned None", url=url)
                await self._ack(msg_id)
                return

            # ── Publish to indexer stream ──────────────────────────────────────
            await self._publish_document(doc)

            # ── Update metrics ─────────────────────────────────────────────────
            process_ms = int((time.monotonic() - start_time) * 1000)
            METRICS.docs_processed.inc()
            METRICS.process_duration.observe(process_ms / 1000)

            # E2E SLO tracking
            e2e_ms = doc.e2e_latency_ms
            METRICS.e2e_latency.observe(e2e_ms / 1000)
            if e2e_ms > 60_000:
                METRICS.slo_breached.inc()

            log.info(
                "Document processed",
                url=url,
                language=doc.language,
                words=doc.word_count,
                entities=len(doc.entities),
                has_embedding=doc.has_embedding,
                process_ms=process_ms,
                e2e_ms=e2e_ms,
            )

        except Exception as e:
            log.error("Message processing error", msg_id=msg_id, error=str(e))
            METRICS.process_errors.labels(stage="pipeline").inc()
            # Don't ACK — message will be retried
            return

        # ACK only after successful processing
        await self._ack(msg_id)

    async def _publish_document(self, doc: IndexedDocument) -> None:
        """Publish the enriched document to STREAM:docs_ready for the Indexer."""
        await self._redis.xadd(
            DOCS_READY_STREAM,
            {"data": doc.to_json()},
            maxlen=50_000,
            approximate=True,
        )

    async def _ack(self, msg_id: str) -> None:
        """Acknowledge a message in the consumer group."""
        await self._redis.xack(CRAWL_COMPLETE_STREAM, CONSUMER_GROUP, msg_id)


# ── Pipeline functions (run in thread pool) ───────────────────────────────────

def _warmup_models() -> None:
    """
    Load all ML models into memory before processing starts.
    Called once at startup — prevents slow first-request latency.
    """
    from pipeline.nlp      import _get_nlp
    from pipeline.embedder import _get_model

    _get_nlp()      # loads spaCy model (~1s)
    _get_model()    # loads sentence-transformers (~500ms)
    log.info("Model warmup complete")


def _fetch_from_minio(minio_key: str) -> str:
    """
    Fetch and decompress raw HTML from MinIO.
    Returns empty string on failure.
    """
    import gzip
    try:
        mc = get_minio()
        response = mc.get_object(settings.minio_bucket, minio_key)
        compressed = response.read()
        response.close()
        response.release_conn()
        # Decompress gzip
        return gzip.decompress(compressed).decode("utf-8", errors="replace")
    except Exception as e:
        log.error("MinIO fetch failed", key=minio_key, error=str(e))
        return ""


def _run_pipeline(url: str, domain: str, html: str, event: dict) -> IndexedDocument | None:
    """
    Run all pipeline stages sequentially and return an IndexedDocument.

    This function is CPU-bound and runs in a thread pool executor.
    All pipeline stages are called synchronously here.

    Stages:
        1. clean_html     → CleanResult
        2. detect_language → str (ISO 639-1)
        3. enrich         → NLPResult (entities, keywords)
        4. embed          → EmbedResult (384d vector)
        5. extract_metadata → MetadataResult
        6. compute_signals  → RankingSignals
        7. Assemble IndexedDocument
    """
    pipeline_start = time.monotonic()

    # ── Stage 1: HTML Cleaning ────────────────────────────────────────────────
    clean_result = clean_html(html, url=url)
    if not clean_result.success:
        log.debug("Clean failed — skipping document", url=url, error=clean_result.error)
        return None

    # ── Stage 2: Language Detection ───────────────────────────────────────────
    html_lang    = extract_html_lang(html)
    content_lang = event.get("response_headers", {}).get("Content-Language", "")
    language     = detect_language(
        text=clean_result.text,
        html_lang=html_lang,
        content_language_header=content_lang,
    )

    # ── Stage 3: NLP Enrichment ───────────────────────────────────────────────
    nlp_result = enrich(text=clean_result.text, title=clean_result.title)

    # ── Stage 4: Semantic Embedding ───────────────────────────────────────────
    embed_result = embed(title=clean_result.title, text=clean_result.text)

    # ── Stage 5: Structured Metadata ──────────────────────────────────────────
    meta_result = extract_metadata(html=html, url=url)

    # ── Stage 6: Ranking Signals ──────────────────────────────────────────────
    domain_authority = float(event.get("domain_authority", 0.5))
    signals = compute_signals(
        word_count       = nlp_result.word_count or clean_result.word_count,
        language         = language,
        title            = clean_result.title,
        has_og_title     = bool(meta_result.og_title),
        has_schema_type  = bool(meta_result.schema_type),
        has_author       = bool(meta_result.author or clean_result.author),
        has_date         = bool(meta_result.date_published or clean_result.date),
        date_published   = meta_result.date_published or clean_result.date,
        crawled_at       = float(event.get("crawled_at", time.time())),
        domain_authority = domain_authority,
    )

    process_ms = int((time.monotonic() - pipeline_start) * 1000)

    # ── Stage 7: Assemble IndexedDocument ─────────────────────────────────────
    doc = IndexedDocument(
        # Identity
        url          = url,
        domain       = domain,
        content_hash = event.get("content_hash", ""),
        minio_key    = event.get("minio_key", ""),

        # Extracted content
        title        = clean_result.title,
        clean_text   = clean_result.text,
        summary      = clean_result.summary,
        language     = language,
        author       = meta_result.author or clean_result.author,
        published_at = meta_result.date_published or clean_result.date,

        # NLP
        entities     = nlp_result.entities,
        keywords     = nlp_result.keywords,
        word_count   = nlp_result.word_count or clean_result.word_count,

        # Embedding
        embedding    = embed_result.embedding,

        # Structured metadata
        og_title        = meta_result.og_title,
        og_description  = meta_result.og_description,
        og_image        = meta_result.og_image,
        schema_type     = meta_result.schema_type,
        canonical_url   = meta_result.canonical_url or url,

        # Ranking signals
        domain_authority = signals.domain_authority,
        freshness_score  = signals.freshness_score,

        # Timing
        discovered_at = float(event.get("discovered_at", 0)),
        crawled_at    = float(event.get("crawled_at", 0)),
        processed_at  = time.time(),

        # Provenance
        discovery_source = event.get("discovery_source", ""),
        crawl_depth      = int(event.get("depth", 0)),
        fetch_ms         = int(event.get("fetch_ms", 0)),
        process_ms       = process_ms,
    )

    return doc


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    service = ProcessorService()

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        log.info("Shutdown signal", signal=sig.name)
        loop.create_task(service.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    await service.start()


if __name__ == "__main__":
    asyncio.run(main())