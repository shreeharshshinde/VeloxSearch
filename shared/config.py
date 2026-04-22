"""
shared/config.py
================
Centralised configuration loader for all services.

Every service imports from here — no service reads os.environ directly.
All settings have sensible defaults so unit tests run without a .env file.

Usage:
    from shared.config import settings
    print(settings.redis_url)
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_bloom_capacity: int = 100_000_000   # 100M URLs
    redis_bloom_error_rate: float = 0.01      # 1% false positive

    # ── MinIO ─────────────────────────────────────────────────────────────────
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "raw-pages"
    minio_secure: bool = False

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    database_url: str = "postgresql://indexer:password@localhost:5432/indexer"

    # ── Meilisearch ───────────────────────────────────────────────────────────
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_api_key: str = "masterKey"
    meilisearch_index: str = "pages"

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "pages"

    # ── Crawler ───────────────────────────────────────────────────────────────
    crawler_workers: int = 10
    crawler_timeout_seconds: int = 30
    crawler_user_agent: str = "RealtimeIndexer/1.0 (+https://your-domain/bot)"
    max_redirects: int = 5
    default_crawl_rate: float = 1.0           # requests/sec per domain
    respect_crawl_delay: bool = True

    # ── Processor ─────────────────────────────────────────────────────────────
    processor_workers: int = 4
    embedding_model: str = "all-MiniLM-L6-v2"
    spacy_model: str = "en_core_web_sm"

    # ── API ───────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_result_cache_ttl: int = 15            # seconds

    # ── Discovery ─────────────────────────────────────────────────────────────
    sitemap_poll_interval: int = 30           # seconds
    websub_callback_url: str = "http://localhost:8000/websub/callback"
    certstream_url: str = "wss://certstream.calidog.io"

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"

    # ── Internal Queue Keys ───────────────────────────────────────────────────
    queue_key: str = "QUEUE:urls"             # Redis Sorted Set
    bloom_key: str = "BF:url_dedup"           # RedisBloom key
    minio_events_key: str = "minio_events"    # Redis list for MinIO events

    # ── Priority Scores ───────────────────────────────────────────────────────
    priority_websub: float = 10.0
    priority_sitemap: float = 7.0
    priority_ct_log: float = 5.0
    priority_link: float = 3.0
    priority_manual: float = 15.0             # manually submitted URLs


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Build Settings from environment variables.
    Cached after first call — settings are immutable at runtime.
    """
    return Settings(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_bloom_capacity=int(os.getenv("REDIS_BLOOM_CAPACITY", "100000000")),
        redis_bloom_error_rate=float(os.getenv("REDIS_BLOOM_ERROR_RATE", "0.01")),

        minio_endpoint=os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        minio_access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        minio_secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        minio_bucket=os.getenv("MINIO_BUCKET", "raw-pages"),
        minio_secure=os.getenv("MINIO_SECURE", "false").lower() == "true",

        database_url=os.getenv("DATABASE_URL", "postgresql://indexer:password@localhost:5432/indexer"),

        meilisearch_url=os.getenv("MEILISEARCH_URL", "http://localhost:7700"),
        meilisearch_api_key=os.getenv("MEILISEARCH_API_KEY", "masterKey"),
        meilisearch_index=os.getenv("MEILISEARCH_INDEX", "pages"),

        qdrant_url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "pages"),

        crawler_workers=int(os.getenv("CRAWLER_WORKERS", "10")),
        crawler_timeout_seconds=int(os.getenv("CRAWLER_TIMEOUT_SECONDS", "30")),
        crawler_user_agent=os.getenv("CRAWLER_USER_AGENT", "RealtimeIndexer/1.0"),
        max_redirects=int(os.getenv("MAX_REDIRECTS", "5")),
        default_crawl_rate=float(os.getenv("DEFAULT_CRAWL_RATE", "1.0")),
        respect_crawl_delay=os.getenv("RESPECT_CRAWL_DELAY", "true").lower() == "true",

        processor_workers=int(os.getenv("PROCESSOR_WORKERS", "4")),
        embedding_model=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        spacy_model=os.getenv("SPACY_MODEL", "en_core_web_sm"),

        api_host=os.getenv("API_HOST", "0.0.0.0"),
        api_port=int(os.getenv("API_PORT", "8000")),
        api_result_cache_ttl=int(os.getenv("API_RESULT_CACHE_TTL", "15")),

        sitemap_poll_interval=int(os.getenv("SITEMAP_POLL_INTERVAL", "30")),
        websub_callback_url=os.getenv("WEBSUB_CALLBACK_URL", "http://localhost:8000/websub/callback"),
        certstream_url=os.getenv("CERTSTREAM_URL", "wss://certstream.calidog.io"),

        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_format=os.getenv("LOG_FORMAT", "json"),
    )


# Module-level singleton — import this directly
settings = get_settings()