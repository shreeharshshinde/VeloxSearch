# VeloxSearch Architecture Graph Report

## Overview
- 81 files · ~46,121 words
- 704 nodes · 1325 edges
- 61 communities

## God Nodes
Critical hub components that hold the system together:

1. **URLTask** (unknown)
2. **DiscoverySource** (unknown)
3. **info()** (unknown)
4. **WebSubSubscriber** (unknown)
5. **LinkExtractor** (unknown)
6. **CTLogScanner** (unknown)
7. **BloomFilter** (unknown)
8. **SitemapWatcher** (unknown)
9. **TestPageClassifier** (unknown)
10. **CrawlStatus** (unknown)

## Surprising Connections
Cross-community patterns that reveal system complexity:

1. {
  "source": "Run all pipeline stages sequentially and return an IndexedDocument.      This fu",
  "target": "CrawlStatus",
  "source_files": [
    "services/processor/main.py",
    "shared/models/crawled_page.py"
  ],
  "confidence": "INFERRED",
  "relation": "uses",
  "why": "inferred connection - not explicitly stated in source; connects across different repos/directories; bridges separate communities; peripheral node `Run all pipeline stages sequentially and return an IndexedDocument.      This fu` unexpectedly reaches hub `CrawlStatus`"
}
2. {
  "source": "services/discovery/queue/redis_queue.py ========================================",
  "target": "URLTask",
  "source_files": [
    "services/discovery/queue/redis_queue.py",
    "shared/models/url_task.py"
  ],
  "confidence": "INFERRED",
  "relation": "uses",
  "why": "inferred connection - not explicitly stated in source; connects across different repos/directories; bridges separate communities; peripheral node `services/discovery/queue/redis_queue.py ========================================` unexpectedly reaches hub `URLTask`"
}
3. {
  "source": "Batch enqueue multiple URL tasks in a single Redis call.          Returns:",
  "target": "URLTask",
  "source_files": [
    "services/discovery/queue/redis_queue.py",
    "shared/models/url_task.py"
  ],
  "confidence": "INFERRED",
  "relation": "uses",
  "why": "inferred connection - not explicitly stated in source; connects across different repos/directories; bridges separate communities; peripheral node `Batch enqueue multiple URL tasks in a single Redis call.          Returns:` unexpectedly reaches hub `URLTask`"
}
4. {
  "source": "Non-blocking pop \u2014 returns None if queue is empty.         Use this when you wan",
  "target": "URLTask",
  "source_files": [
    "services/discovery/queue/redis_queue.py",
    "shared/models/url_task.py"
  ],
  "confidence": "INFERRED",
  "relation": "uses",
  "why": "inferred connection - not explicitly stated in source; connects across different repos/directories; bridges separate communities; peripheral node `Non-blocking pop \u2014 returns None if queue is empty.         Use this when you wan` unexpectedly reaches hub `URLTask`"
}
5. {
  "source": "Blocking pop \u2014 blocks until an item is available.         Crawlers use this to i",
  "target": "URLTask",
  "source_files": [
    "services/discovery/queue/redis_queue.py",
    "shared/models/url_task.py"
  ],
  "confidence": "INFERRED",
  "relation": "uses",
  "why": "inferred connection - not explicitly stated in source; connects across different repos/directories; bridges separate communities; peripheral node `Blocking pop \u2014 blocks until an item is available.         Crawlers use this to i` unexpectedly reaches hub `URLTask`"
}

## Communities
The system organizes into 61 distinct communities:

**Community 0**: 146 nodes
  Sample: bloom_filter_bloomfilter, bloom_filter_bloomfilter_add, bloom_filter_bloomfilter_init
**Community 1**: 61 nodes
  Sample: crawled_page_crawledpage, crawled_page_crawledpage_repr, crawled_page_crawledpage_to_json
**Community 2**: 49 nodes
  Sample: bloom_filter, bm25_ranking, celery
**Community 3**: 34 nodes
  Sample: cleaner_clean_html, cleaner_clean_str, cleaner_cleanresult
**Community 4**: 34 nodes
  Sample: bloom_filter_bloomfilter_add_many, bloom_filter_bloomfilter_ensure_initialised, bloom_filter_bloomfilter_exists
**Community 5**: 32 nodes
  Sample: asyncio, demo_banner, demo_check_api_health
**Community 6**: 32 nodes
  Sample: cmd_config, cmd_crawlevent, cmd_urltask
**Community 7**: 31 nodes
  Sample: metadata_extract_canonical, metadata_extract_metadata, metadata_metadataresult
**Community 8**: 31 nodes
  Sample: fetcher_config, fetcher_fetchresult, fetcher_httpfetcher
**Community 9**: 31 nodes
  Sample: indexed_doc_display_description, indexed_doc_display_title, indexed_doc_e2e_latency_ms
