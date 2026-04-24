"""
services/indexer/main.py
==========================
Indexer Service — reads enriched documents from Redis Stream and writes
them to all three search indexes simultaneously.

WHAT THIS SERVICE DOES:
  1. Consumes IndexedDocument JSON from Redis Stream STREAM:docs_ready
     (produced by Phase 3 Processor)
  2. Writes each document to FOUR backends in PARALLEL:
       a. Meilisearch   → full-text BM25 inverted index
       b. Qdrant        → 384d ANN vector index (HNSW)
       c. PostgreSQL    → metadata store + crawl audit log
       d. Redis Signals → ranking signals cache + cache invalidation
  3. Records success/failure per backend with partial-success support
     (a Meilisearch failure doesn't prevent Qdrant from writing)
  4. Updates the doc's indexed_at timestamp and publishes final metrics

PARALLEL WRITE STRATEGY:
  asyncio.gather() runs all four writes concurrently.
  Total time ≈ max(individual write times), not sum.

  Typical timing:
    Meilisearch: ~10ms
    Qdrant:       ~5ms
    PostgreSQL:  ~10ms
    Redis:        ~2ms
    ─────────────────
    Total:       ~12ms  (parallel, not sequential)

  vs sequential: 10 + 5 + 10 + 2 = 27ms

PARTIAL SUCCESS:
  If one backend fails, the others are not rolled back.
  The document remains searchable via the successful backends.
  A retry mechanism (dead-letter queue) handles persistent failures.

CONSUMER GROUP:
  Uses Redis consumer group "indexer_group" on STREAM:docs_ready.
  Supports multiple indexer replicas without duplicate writes.
  Messages are ACK'd only after all writes complete.

DEAD LETTER QUEUE:
  Messages that fail after MAX_RETRIES are moved to STREAM:dlq
  for manual inspection and replay.
"""

import asyncio
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger
from shared.metrics import METRICS, start_metrics_server
from shared.models.indexed_document import IndexedDocument

from writers.meilisearch_writer import MeilisearchWriter
from writers.qdrant_writer import QdrantWriter
from writers.postgres_writer import PostgresWriter
from writers.redis_signals import RedisSignalsWriter

log = get_logger("indexer")

# ── Stream config ─────────────────────────────────────────────────────────────
DOCS_READY_STREAM = "STREAM:docs_ready"
DLQ_STREAM        = "STREAM:dlq"
CONSUMER_GROUP    = "indexer_group"
CONSUMER_NAME     = f"indexer_{os.getpid()}"
BATCH_SIZE        = 5       # consume up to 5 docs per Redis read
MAX_RETRIES       = 3       # before moving to DLQ


class IndexerService:
    """
    Reads enriched documents and writes them to all search backends in parallel.
    """

    def __init__(self):
        self._redis: aioredis.Redis | None       = None
        self._meili:  MeilisearchWriter | None   = None
        self._qdrant: QdrantWriter | None        = None
        self._pg:     PostgresWriter | None      = None
        self._rsig:   RedisSignalsWriter | None  = None
        self._running = False

        # Track retry counts per message ID
        self._retry_counts: dict[str, int] = {}

    async def start(self) -> None:
        log.info("Indexer service starting", pid=os.getpid())

        # ── Redis (consumer + signals) ─────────────────────────────────────────
        self._redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,
        )
        await self._redis.ping()
        log.info("Redis connected")

        # ── Connect all writers ────────────────────────────────────────────────
        self._meili  = MeilisearchWriter()
        self._qdrant = QdrantWriter()
        self._pg     = PostgresWriter()
        self._rsig   = RedisSignalsWriter()

        # Connect all in parallel
        await asyncio.gather(
            self._meili.connect(),
            self._qdrant.connect(),
            self._pg.connect(),
            self._rsig.connect(),
        )
        log.info("All backends connected: Meilisearch, Qdrant, PostgreSQL, Redis")

        # ── Consumer group ────────────────────────────────────────────────────
        await self._ensure_consumer_group()

        # ── Metrics server ────────────────────────────────────────────────────
        start_metrics_server("indexer")

        self._running = True
        log.info(
            "Indexer service running",
            stream=DOCS_READY_STREAM,
            group=CONSUMER_GROUP,
            consumer=CONSUMER_NAME,
        )

        await self._consume_loop()

    async def stop(self) -> None:
        self._running = False
        if self._pg:
            await self._pg.close()
        if self._rsig:
            await self._rsig.close()
        if self._redis:
            await self._redis.aclose()
        log.info("Indexer service stopped")

    # ── Consumer group setup ──────────────────────────────────────────────────

    async def _ensure_consumer_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                DOCS_READY_STREAM, CONSUMER_GROUP, id="0", mkstream=True
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    # ── Main consumption loop ─────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """
        Continuously read from STREAM:docs_ready and index each document.
        Blocks up to 5 seconds when the stream is empty (efficient idle).
        """
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={DOCS_READY_STREAM: ">"},
                    count=BATCH_SIZE,
                    block=5000,
                )

                if not messages:
                    continue

                # Process all messages concurrently within the batch
                tasks = []
                for _stream, records in messages:
                    for msg_id, fields in records:
                        tasks.append(self._index_message(msg_id, fields))

                await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Consume loop error", error=str(e))
                await asyncio.sleep(1)

    # ── Document indexing ─────────────────────────────────────────────────────

    async def _index_message(self, msg_id: str, fields: dict) -> None:
        """
        Parse an IndexedDocument from the stream message and write to all backends.
        ACKs on success, retries on failure, DLQs after MAX_RETRIES.
        """
        start_time = time.monotonic()

        try:
            # ── Deserialise ───────────────────────────────────────────────────
            raw = fields.get("data", "{}")
            doc = IndexedDocument.from_json(raw)

            log.info(
                "Indexing document",
                url=doc.url,
                language=doc.language,
                words=doc.word_count,
                has_embedding=doc.has_embedding,
            )

            # ── Parallel writes to all backends ───────────────────────────────
            results = await asyncio.gather(
                self._meili.write(doc),
                self._qdrant.write(doc),
                self._pg.write(doc),
                self._rsig.write(doc),
                return_exceptions=True,
            )

            meili_ok, qdrant_ok, pg_ok, redis_ok = [
                r if isinstance(r, bool) else False
                for r in results
            ]

            # Log any exceptions (don't crash the service)
            for backend, result in zip(
                ["meilisearch", "qdrant", "postgres", "redis"], results
            ):
                if isinstance(result, Exception):
                    log.error(f"{backend} write exception", url=doc.url, error=str(result))
                    METRICS.index_errors.labels(backend=backend).inc()

            # ── Update metrics ────────────────────────────────────────────────
            index_ms = int((time.monotonic() - start_time) * 1000)
            all_succeeded = all([meili_ok, qdrant_ok, pg_ok, redis_ok])

            if all_succeeded:
                METRICS.docs_indexed.inc()
                METRICS.index_duration.observe(index_ms / 1000)
                log.info(
                    "Document indexed ✓",
                    url=doc.url,
                    index_ms=index_ms,
                    e2e_ms=doc.e2e_latency_ms + index_ms,
                    backends="meili+qdrant+pg+redis",
                )
            else:
                log.warning(
                    "Partial index success",
                    url=doc.url,
                    meili=meili_ok,
                    qdrant=qdrant_ok,
                    pg=pg_ok,
                    redis=redis_ok,
                )

            # ACK even on partial success — at least some indexes have it
            await self._ack(msg_id)
            self._retry_counts.pop(msg_id, None)

        except json.JSONDecodeError as e:
            log.error("Malformed document JSON", msg_id=msg_id, error=str(e))
            await self._ack(msg_id)   # bad data — no point retrying

        except Exception as e:
            log.error("Indexing failed", msg_id=msg_id, error=str(e))
            await self._handle_failure(msg_id, fields, str(e))

    async def _ack(self, msg_id: str) -> None:
        """Acknowledge the message — removes it from the pending list."""
        try:
            await self._redis.xack(DOCS_READY_STREAM, CONSUMER_GROUP, msg_id)
        except Exception as e:
            log.error("ACK failed", msg_id=msg_id, error=str(e))

    async def _handle_failure(self, msg_id: str, fields: dict, error: str) -> None:
        """
        Handle a failed indexing attempt.
        Increments retry counter — after MAX_RETRIES, moves to DLQ and ACKs.
        """
        retries = self._retry_counts.get(msg_id, 0) + 1
        self._retry_counts[msg_id] = retries

        if retries >= MAX_RETRIES:
            log.error(
                "Max retries exceeded — moving to DLQ",
                msg_id=msg_id,
                retries=retries,
                error=error,
            )
            # Publish to DLQ for manual inspection
            await self._redis.xadd(
                DLQ_STREAM,
                {**fields, "original_msg_id": msg_id, "error": error},
                maxlen=10_000,
            )
            await self._ack(msg_id)   # remove from pending
            self._retry_counts.pop(msg_id, None)
        else:
            log.warning(
                "Indexing failed — will retry",
                msg_id=msg_id,
                attempt=retries,
                max_retries=MAX_RETRIES,
            )
            # Don't ACK — Redis will redeliver on next XREADGROUP call
            await asyncio.sleep(1 * retries)   # backoff: 1s, 2s, 3s


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main():
    service = IndexerService()

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        log.info("Shutdown signal received", signal=sig.name)
        loop.create_task(service.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    await service.start()


if __name__ == "__main__":
    asyncio.run(main())