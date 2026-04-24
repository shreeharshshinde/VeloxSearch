"""
services/indexer/writers/postgres_writer.py
============================================
Writes IndexedDocument metadata to PostgreSQL.

WHY POSTGRESQL IN THE INDEXER?
-------------------------------
Meilisearch stores what we SEARCH. Qdrant stores what we FIND semantically.
PostgreSQL stores everything we need to MANAGE, MONITOR, and ANALYSE:

  1. Crawl history (append-only audit log)
     Every crawl attempt is recorded — success, failure, timing.
     Used for: SLO reporting, debugging, detecting stale pages.

  2. Page metadata (current state)
     One row per URL — always reflects the latest crawl.
     Used for: admin dashboard, domain statistics, re-crawl scheduling.

  3. Domain registry
     Per-domain authority scores, crawl rate limits, robots.txt cache.
     Used for: priority queue scoring (Phase 1), rate limiting (Phase 2).

  4. SLO reporting
     The slo_status() SQL function queries crawl_log for latency percentiles.
     The Grafana dashboard calls this every minute.

WRITE PATTERN: Upsert + Append
  pages table:    UPSERT (INSERT ... ON CONFLICT DO UPDATE)
  crawl_log table: INSERT always (append-only audit log, never updated)

ASYNC DRIVER: asyncpg
  asyncpg is the fastest PostgreSQL driver for Python.
  It uses binary protocol and connection pooling for low-latency writes.
  Pool size = 5 connections (adequate for MVP — writes are infrequent vs searches).
"""

import asyncio
import time
from typing import Optional

import asyncpg

from shared.config import settings
from shared.logging import get_logger
from shared.models.indexed_document import IndexedDocument

log = get_logger("indexer.postgres")


class PostgresWriter:
    """
    Writes IndexedDocument metadata to PostgreSQL.
    Uses a connection pool for efficient concurrent writes.
    """

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._ready = False

    async def connect(self) -> None:
        """Create the asyncpg connection pool."""
        self._pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=2,
            max_size=5,
            command_timeout=10.0,
        )
        # Verify connectivity
        async with self._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

        self._ready = True
        log.info("PostgreSQL writer ready", dsn=settings.database_url.split("@")[-1])

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def write(self, doc: IndexedDocument) -> bool:
        """
        Write document metadata to PostgreSQL.

        Executes two statements inside a single transaction:
          1. UPSERT into pages        (current state of the URL)
          2. INSERT into crawl_log    (immutable crawl audit record)

        Both succeed or both fail — atomicity guaranteed.

        Args:
            doc: Fully enriched IndexedDocument from Phase 3

        Returns:
            True on success, False on failure.

        Performance:
            ~5–15ms per document (single transaction, two statements).
        """
        if not self._ready:
            log.error("PostgreSQL writer not connected")
            return False

        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await self._upsert_page(conn, doc)
                    await self._upsert_domain(conn, doc)
                    await self._insert_crawl_log(conn, doc)

            log.debug("PostgreSQL write success", url=doc.url)
            return True

        except asyncpg.UniqueViolationError:
            # Race condition: two workers processing same URL simultaneously
            # The second write is a no-op (content_hash is the same)
            log.debug("PostgreSQL duplicate write skipped", url=doc.url)
            return True

        except Exception as e:
            log.error("PostgreSQL write failed", url=doc.url, error=str(e))
            return False

    async def _upsert_page(self, conn: asyncpg.Connection, doc: IndexedDocument) -> None:
        """
        Insert or update the page record.

        ON CONFLICT (url) DO UPDATE means:
          - First crawl: inserts a new row
          - Re-crawl: updates existing row with new content/metadata

        crawl_count is incremented on every re-crawl.
        discovery_latency_ms is only set on first crawl (DO NOT update on conflict).
        """
        await conn.execute(
            """
            INSERT INTO pages (
                url, domain, title, language, word_count, content_hash,
                crawled_at, indexed_at, http_status, fetch_bytes,
                discovery_source, discovery_latency_ms, crawl_depth,
                freshness_score, domain_authority, is_indexed, index_error
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                to_timestamp($7), to_timestamp($8), $9, $10,
                $11, $12, $13,
                $14, $15, TRUE, NULL
            )
            ON CONFLICT (url) DO UPDATE SET
                title            = EXCLUDED.title,
                language         = EXCLUDED.language,
                word_count       = EXCLUDED.word_count,
                content_hash     = EXCLUDED.content_hash,
                crawled_at       = EXCLUDED.crawled_at,
                indexed_at       = EXCLUDED.indexed_at,
                http_status      = EXCLUDED.http_status,
                fetch_bytes      = EXCLUDED.fetch_bytes,
                freshness_score  = EXCLUDED.freshness_score,
                domain_authority = EXCLUDED.domain_authority,
                is_indexed       = TRUE,
                index_error      = NULL,
                crawl_count      = pages.crawl_count + 1
            """,
            # Positional parameters ($1 … $15)
            doc.url,
            doc.domain,
            doc.title[:500] if doc.title else None,
            doc.language[:2] if doc.language else None,
            doc.word_count,
            doc.content_hash,
            doc.crawled_at,
            time.time(),                             # indexed_at = now
            200,                                     # http_status (not stored in doc)
            0,                                       # fetch_bytes (not stored in doc)
            doc.discovery_source[:20] if doc.discovery_source else None,
            doc.e2e_latency_ms,                      # discovery → indexed latency
            doc.crawl_depth,
            doc.freshness_score,
            doc.domain_authority,
        )

    async def _upsert_domain(self, conn: asyncpg.Connection, doc: IndexedDocument) -> None:
        """
        Ensure the domain exists in the domains table.
        Updates last_crawled_at and increments total_pages_indexed.

        Uses INSERT ... ON CONFLICT DO UPDATE so the first crawl from
        a new domain automatically creates a domain record.
        """
        await conn.execute(
            """
            INSERT INTO domains (domain, authority_score, last_crawled_at, total_pages_indexed)
            VALUES ($1, $2, NOW(), 1)
            ON CONFLICT (domain) DO UPDATE SET
                last_crawled_at     = NOW(),
                total_pages_indexed = domains.total_pages_indexed + 1,
                authority_score     = GREATEST(domains.authority_score, EXCLUDED.authority_score)
            """,
            doc.domain,
            doc.domain_authority,
        )

    async def _insert_crawl_log(self, conn: asyncpg.Connection, doc: IndexedDocument) -> None:
        """
        Append a record to the crawl_log table.

        crawl_log is APPEND-ONLY — rows are never updated or deleted.
        This gives us a full audit trail of every crawl attempt,
        enabling latency analysis and SLO reporting over time.

        The total_ms column is the most critical for SLO reporting:
            total_ms < 60,000 → SLO passed
            total_ms > 60,000 → SLO breached
        """
        now = time.time()
        await conn.execute(
            """
            INSERT INTO crawl_log (
                url, domain,
                queued_at, crawl_started_at, crawl_ended_at,
                process_started_at, process_ended_at, indexed_at,
                queue_wait_ms, fetch_ms, process_ms, index_ms, total_ms,
                http_status, content_hash, fetch_bytes, fetcher_type,
                error, success
            ) VALUES (
                $1,  $2,
                to_timestamp($3),  to_timestamp($4),  to_timestamp($5),
                to_timestamp($5),  to_timestamp($6),  to_timestamp($7),
                $8,  $9,  $10, $11, $12,
                200, $13, 0,   'http',
                NULL, TRUE
            )
            """,
            doc.url,
            doc.domain,
            doc.discovered_at,                           # queued_at ≈ discovered_at
            doc.crawled_at - (doc.fetch_ms / 1000),      # crawl_started_at
            doc.crawled_at,                              # crawl_ended_at
            doc.processed_at,                            # process_ended_at
            now,                                         # indexed_at
            int((doc.crawled_at - doc.discovered_at) * 1000),   # queue_wait_ms
            doc.fetch_ms,
            doc.process_ms,
            int((now - doc.processed_at) * 1000),        # index_ms
            doc.e2e_latency_ms,                          # total_ms (SLO metric)
            doc.content_hash,
        )

    async def get_slo_status(self, minutes: int = 60) -> dict:
        """
        Query current SLO metrics from PostgreSQL.

        Uses the slo_status() function defined in infra/postgres/init.sql.
        Returns: total, passed, failed counts + p50/p95 latencies.

        Called by the API's /stats endpoint.
        """
        if not self._ready:
            return {}
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM slo_status($1)", minutes
                )
                if row:
                    return dict(row)
        except Exception as e:
            log.error("SLO query failed", error=str(e))
        return {}

    async def get_page_count(self) -> int:
        """Return total number of indexed pages."""
        if not self._ready:
            return 0
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchval("SELECT COUNT(*) FROM pages WHERE is_indexed = TRUE")
        except Exception:
            return 0