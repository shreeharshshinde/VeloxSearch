-- =============================================================================
-- Real-Time Indexing System — PostgreSQL Schema
-- PRN: 22510103
--
-- Run automatically by Docker Compose on first container start.
-- To re-run manually:
--   docker exec -i indexer_postgres psql -U indexer -d indexer < infra/postgres/init.sql
-- =============================================================================

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- uuid_generate_v4()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- trigram index for LIKE queries

-- =============================================================================
-- DOMAINS
-- Registry of known domains, their crawl settings, and authority scores.
-- =============================================================================
CREATE TABLE IF NOT EXISTS domains (
    id                  SERIAL PRIMARY KEY,
    domain              VARCHAR(255) UNIQUE NOT NULL,
    authority_score     FLOAT DEFAULT 0.5 CHECK (authority_score BETWEEN 0 AND 1),
    crawl_rate_limit    FLOAT DEFAULT 1.0,          -- requests per second (can be fractional)
    robots_txt          TEXT,                        -- cached robots.txt content
    robots_cached_at    TIMESTAMPTZ,
    sitemap_url         TEXT,                        -- discovered sitemap URL
    websub_hub          TEXT,                        -- WebSub hub URL if known
    first_seen_at       TIMESTAMPTZ DEFAULT NOW(),
    last_crawled_at     TIMESTAMPTZ,
    total_pages_indexed INTEGER DEFAULT 0,
    is_blocked          BOOLEAN DEFAULT FALSE,       -- manually blocked domains
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_domains_authority ON domains(authority_score DESC);
CREATE INDEX IF NOT EXISTS idx_domains_last_crawled ON domains(last_crawled_at);

-- =============================================================================
-- PAGES
-- One row per indexed page URL (current state).
-- Updated in-place on re-crawl.
-- =============================================================================
CREATE TABLE IF NOT EXISTS pages (
    id                  BIGSERIAL PRIMARY KEY,
    url                 TEXT UNIQUE NOT NULL,
    domain              VARCHAR(255) REFERENCES domains(domain) ON DELETE SET NULL,

    -- Content
    title               TEXT,
    language            CHAR(2),                     -- ISO 639-1 code
    word_count          INTEGER,
    content_hash        CHAR(16) NOT NULL,            -- xxHash64, for change detection

    -- Crawl metadata
    crawl_count         INTEGER DEFAULT 1,
    crawled_at          TIMESTAMPTZ NOT NULL,
    indexed_at          TIMESTAMPTZ NOT NULL,
    http_status         SMALLINT,                    -- last HTTP response code
    fetch_bytes         INTEGER,                     -- raw response size in bytes

    -- Discovery
    discovery_source    VARCHAR(20) CHECK (
                            discovery_source IN ('websub','sitemap','ct_log','link','manual')
                        ),
    discovery_latency_ms INTEGER,                    -- ms from discovery to indexed_at
    crawl_depth         SMALLINT DEFAULT 0,

    -- Ranking signals
    freshness_score     FLOAT DEFAULT 0.5,           -- 0=stale, 1=just published
    domain_authority    FLOAT DEFAULT 0.5,

    -- Status
    is_indexed          BOOLEAN DEFAULT TRUE,        -- False if removed from index
    index_error         TEXT                         -- error message if indexing failed
);

CREATE INDEX IF NOT EXISTS idx_pages_domain        ON pages(domain);
CREATE INDEX IF NOT EXISTS idx_pages_indexed_at    ON pages(indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_pages_crawled_at    ON pages(crawled_at DESC);
CREATE INDEX IF NOT EXISTS idx_pages_content_hash  ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_pages_language      ON pages(language);
CREATE INDEX IF NOT EXISTS idx_pages_source        ON pages(discovery_source);
-- Trigram index for URL prefix searches
CREATE INDEX IF NOT EXISTS idx_pages_url_trgm      ON pages USING gin (url gin_trgm_ops);

-- =============================================================================
-- CRAWL_LOG
-- Append-only audit log of every crawl attempt.
-- Used for debugging, SLO reporting, and latency analysis.
-- =============================================================================
CREATE TABLE IF NOT EXISTS crawl_log (
    id                  BIGSERIAL PRIMARY KEY,
    url                 TEXT NOT NULL,
    domain              VARCHAR(255),

    -- Timing (all in milliseconds)
    queued_at           TIMESTAMPTZ,
    crawl_started_at    TIMESTAMPTZ,
    crawl_ended_at      TIMESTAMPTZ,
    process_started_at  TIMESTAMPTZ,
    process_ended_at    TIMESTAMPTZ,
    indexed_at          TIMESTAMPTZ,

    -- Per-stage latency (ms)
    queue_wait_ms       INTEGER,                     -- time in queue before crawl started
    fetch_ms            INTEGER,                     -- HTTP fetch duration
    process_ms          INTEGER,                     -- NLP pipeline duration
    index_ms            INTEGER,                     -- index write duration
    total_ms            INTEGER,                     -- discovery to indexed

    -- Result
    http_status         SMALLINT,
    content_hash        CHAR(16),
    fetch_bytes         INTEGER,
    fetcher_type        VARCHAR(20),                 -- 'http' or 'playwright'
    error               TEXT,                        -- error message if failed
    success             BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_crawl_log_url     ON crawl_log(url);
CREATE INDEX IF NOT EXISTS idx_crawl_log_started ON crawl_log(crawl_started_at DESC);
CREATE INDEX IF NOT EXISTS idx_crawl_log_total   ON crawl_log(total_ms);

-- =============================================================================
-- SLO_REPORT (materialised view refreshed every minute)
-- Pre-computes the 60s SLO metrics for the Grafana dashboard.
-- =============================================================================
CREATE TABLE IF NOT EXISTS slo_report (
    window_start        TIMESTAMPTZ PRIMARY KEY,
    window_minutes      INTEGER DEFAULT 1,
    total_indexed       INTEGER,
    within_30s          INTEGER,
    within_60s          INTEGER,                     -- our SLO target
    within_90s          INTEGER,
    p50_ms              FLOAT,
    p95_ms              FLOAT,
    p99_ms              FLOAT,
    slo_percent         FLOAT                        -- (within_60s / total_indexed) * 100
);

-- =============================================================================
-- SITEMAP_REGISTRY
-- Domains registered for sitemap monitoring.
-- =============================================================================
CREATE TABLE IF NOT EXISTS sitemap_registry (
    id          SERIAL PRIMARY KEY,
    domain      VARCHAR(255) UNIQUE REFERENCES domains(domain),
    sitemap_url TEXT NOT NULL,
    added_at    TIMESTAMPTZ DEFAULT NOW(),
    last_polled TIMESTAMPTZ,
    active      BOOLEAN DEFAULT TRUE
);

-- =============================================================================
-- SEED DOMAINS (initial data for development)
-- =============================================================================
INSERT INTO domains (domain, authority_score, crawl_rate_limit)
VALUES
    ('news.ycombinator.com', 0.95, 0.5),
    ('techcrunch.com',       0.90, 1.0),
    ('wikipedia.org',        0.99, 2.0),
    ('github.com',           0.97, 1.0),
    ('dev.to',               0.75, 1.0)
ON CONFLICT (domain) DO NOTHING;

-- =============================================================================
-- HELPER FUNCTIONS
-- =============================================================================

-- Compute freshness score from indexed_at (1.0 = just indexed, decays over time)
CREATE OR REPLACE FUNCTION compute_freshness(indexed_at TIMESTAMPTZ)
RETURNS FLOAT AS $$
    SELECT GREATEST(0.0, 1.0 - EXTRACT(EPOCH FROM (NOW() - indexed_at)) / 86400.0)
$$ LANGUAGE SQL IMMUTABLE;

-- SLO reporting function: returns pass/fail for last N minutes
CREATE OR REPLACE FUNCTION slo_status(minutes INTEGER DEFAULT 60)
RETURNS TABLE(
    total       BIGINT,
    passed      BIGINT,
    failed      BIGINT,
    slo_pct     FLOAT,
    p50_ms      FLOAT,
    p95_ms      FLOAT
) AS $$
    SELECT
        COUNT(*)                                        AS total,
        COUNT(*) FILTER (WHERE total_ms <= 60000)      AS passed,
        COUNT(*) FILTER (WHERE total_ms > 60000)       AS failed,
        ROUND(
            100.0 * COUNT(*) FILTER (WHERE total_ms <= 60000) / NULLIF(COUNT(*), 0),
            2
        )                                               AS slo_pct,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY total_ms) AS p50_ms,
        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95_ms
    FROM crawl_log
    WHERE
        crawl_started_at >= NOW() - (minutes || ' minutes')::INTERVAL
        AND success = TRUE
        AND total_ms IS NOT NULL
$$ LANGUAGE SQL;