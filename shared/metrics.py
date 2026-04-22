"""
shared/metrics.py
=================
All Prometheus metrics for the indexing pipeline, defined in one place.

Each service imports only what it needs.  Defining metrics centrally prevents
naming inconsistencies and makes dashboards easy to build.

Usage:
    from shared.metrics import METRICS
    METRICS.urls_discovered.labels(source="websub").inc()
    METRICS.crawl_duration.observe(1.23)

The prometheus_client HTTP server is started per-service on a separate port
(default 9100 + offset based on SERVICE_NAME).
"""

import os
from dataclasses import dataclass

from prometheus_client import Counter, Gauge, Histogram, start_http_server


# Standard latency buckets covering 0–120 seconds (our SLO window)
LATENCY_BUCKETS = [1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90, 120]

# Standard HTTP latency buckets (ms → seconds)
HTTP_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30]


@dataclass
class PipelineMetrics:
    # ── Discovery ─────────────────────────────────────────────────────────────
    urls_discovered: Counter           # total URLs discovered, by source
    queue_depth: Gauge                 # current items in Redis priority queue
    bloom_filter_hits: Counter         # URLs rejected by Bloom filter (already seen)

    # ── Crawling ─────────────────────────────────────────────────────────────
    crawl_started: Counter             # crawl jobs started
    crawl_completed: Counter           # successful crawls
    crawl_errors: Counter              # failed crawls, by error type
    crawl_duration: Histogram          # time to fetch a page (seconds)
    crawl_bytes: Counter               # total bytes downloaded
    robots_blocked: Counter            # URLs blocked by robots.txt

    # ── Processing ────────────────────────────────────────────────────────────
    docs_processed: Counter            # documents through NLP pipeline
    process_duration: Histogram        # total processing time (seconds)
    embed_duration: Histogram          # embedding generation time (seconds)
    process_errors: Counter            # processing failures, by stage

    # ── Indexing ──────────────────────────────────────────────────────────────
    docs_indexed: Counter              # documents written to all indexes
    index_duration: Histogram          # time to write to all indexes (seconds)
    index_errors: Counter              # index write failures, by backend

    # ── End-to-End SLO ───────────────────────────────────────────────────────
    e2e_latency: Histogram             # discovery → indexed (seconds)
    slo_breached: Counter              # URLs that took > 60s (SLO violation)

    # ── API ───────────────────────────────────────────────────────────────────
    search_requests: Counter           # total search API requests, by mode
    search_latency: Histogram          # query response time (seconds)
    search_cache_hits: Counter         # served from Redis cache
    search_cache_misses: Counter       # required full retrieval


def _build_metrics() -> PipelineMetrics:
    return PipelineMetrics(
        # Discovery
        urls_discovered=Counter(
            "indexer_urls_discovered_total",
            "Total URLs discovered, labelled by discovery source",
            ["source"],
        ),
        queue_depth=Gauge(
            "indexer_queue_depth",
            "Current number of URLs waiting in the priority queue",
        ),
        bloom_filter_hits=Counter(
            "indexer_bloom_filter_hits_total",
            "URLs rejected by the Bloom filter as already-seen duplicates",
        ),

        # Crawling
        crawl_started=Counter(
            "indexer_crawl_started_total",
            "Number of crawl jobs started",
        ),
        crawl_completed=Counter(
            "indexer_crawl_completed_total",
            "Number of pages successfully fetched",
            ["fetcher"],           # http | playwright
        ),
        crawl_errors=Counter(
            "indexer_crawl_errors_total",
            "Crawl failures by error type",
            ["error_type"],        # timeout | http_error | robots | parse_error
        ),
        crawl_duration=Histogram(
            "indexer_crawl_duration_seconds",
            "Time to fetch a single page",
            buckets=HTTP_BUCKETS,
        ),
        crawl_bytes=Counter(
            "indexer_crawl_bytes_total",
            "Total bytes downloaded by crawlers",
        ),
        robots_blocked=Counter(
            "indexer_robots_blocked_total",
            "URLs blocked due to robots.txt disallow rules",
        ),

        # Processing
        docs_processed=Counter(
            "indexer_docs_processed_total",
            "Documents that completed the NLP processing pipeline",
        ),
        process_duration=Histogram(
            "indexer_process_duration_seconds",
            "Total time to run NLP pipeline on one document",
            buckets=HTTP_BUCKETS,
        ),
        embed_duration=Histogram(
            "indexer_embed_duration_seconds",
            "Time to generate sentence embeddings for one document",
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
        ),
        process_errors=Counter(
            "indexer_process_errors_total",
            "Processing failures by pipeline stage",
            ["stage"],            # clean | nlp | embed | metadata
        ),

        # Indexing
        docs_indexed=Counter(
            "indexer_docs_indexed_total",
            "Documents successfully written to all search indexes",
        ),
        index_duration=Histogram(
            "indexer_index_duration_seconds",
            "Time to write one document to all indexes",
            buckets=HTTP_BUCKETS,
        ),
        index_errors=Counter(
            "indexer_index_errors_total",
            "Index write failures by backend",
            ["backend"],          # meilisearch | qdrant | postgres
        ),

        # End-to-end SLO
        e2e_latency=Histogram(
            "indexer_e2e_latency_seconds",
            "End-to-end latency from URL discovery to searchable in index",
            buckets=LATENCY_BUCKETS,
        ),
        slo_breached=Counter(
            "indexer_slo_breached_total",
            "URLs that were indexed after the 60-second SLO target",
        ),

        # API
        search_requests=Counter(
            "indexer_search_requests_total",
            "Total search API requests by retrieval mode",
            ["mode"],             # bm25 | semantic | hybrid
        ),
        search_latency=Histogram(
            "indexer_search_latency_seconds",
            "Search query end-to-end latency",
            buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
        ),
        search_cache_hits=Counter(
            "indexer_search_cache_hits_total",
            "Search results served from the Redis result cache",
        ),
        search_cache_misses=Counter(
            "indexer_search_cache_misses_total",
            "Search queries that required a full index retrieval",
        ),
    )


# Module-level singleton — import this directly
METRICS = _build_metrics()


# Per-service Prometheus HTTP port map
_METRICS_PORTS = {
    "discovery": 9101,
    "crawler":   9102,
    "processor": 9103,
    "indexer":   9104,
    "api":       9105,
}


def start_metrics_server(service_name: str | None = None) -> None:
    """
    Start the Prometheus HTTP metrics server for the given service.
    Call this once at service startup.

    Args:
        service_name: One of the keys in _METRICS_PORTS.
                      Falls back to SERVICE_NAME env var, then port 9100.
    """
    name = service_name or os.getenv("SERVICE_NAME", "unknown")
    port = _METRICS_PORTS.get(name, 9100)
    start_http_server(port)