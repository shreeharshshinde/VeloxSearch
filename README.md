# Real-Time Web Indexing System (VeloxSearch)
### PRN: 22510103 | Target: Index new pages within **60 seconds** of discovery

---

## Quick Start

```bash
# 1. Clone & setup (one time)
git clone https://github.com/shreeharsh157/VeloxSearch.git && cd VeloxSearch
chmod +x scripts/setup.sh && ./scripts/setup.sh

# 2. Start everything
make start

# 3. Load test URLs
make seed

# 4. Open the API
open http://localhost:8000/docs
```

---

## What This System Does

This is a production-grade web crawling and indexing pipeline where any newly
discovered URL becomes **fully searchable within 60 seconds**.

```
URL discovered → crawled → NLP processed → indexed → searchable
     0s              10s          25s           45s        <60s
```

---

## Architecture at a Glance

```
Discovery → [Redis Queue] → Crawler → [MinIO] → Processor → Indexer → API
  CT Logs                   Go/PW     Raw HTML    NLP+Embed   ES+Qdrant  FastAPI
  Sitemap                                          spaCy
  WebSub                                          MiniLM
  Links
```

---

## Services

| Service | Language | Port | Purpose |
|---------|----------|------|---------|
| `discovery` | Python | 9101 (metrics) | Finds new URLs |
| `crawler` | Go + Python | 9102 (metrics) | Fetches pages |
| `processor` | Python | 9103 (metrics) | NLP + embeddings |
| `indexer` | Python | 9104 (metrics) | Writes to indexes |
| `api` | Python FastAPI | 8000 | Search API |

## Infrastructure

| Service | Port | UI |
|---------|------|----|
| Redis | 6379 | http://localhost:8001 (RedisInsight) |
| MinIO | 9000 | http://localhost:9001 |
| PostgreSQL | 5432 | — |
| Meilisearch | 7700 | http://localhost:7700 |
| Qdrant | 6333 | http://localhost:6333/dashboard |
| Prometheus | 9090 | http://localhost:9090 |
| Grafana | 3000 | http://localhost:3000 |

---

## Documentation

| Doc | What it covers |
|-----|---------------|
| [`MASTER_PLAN.md`](MASTER_PLAN.md) | Full architecture, tech stack, project plan |
| [`docs/PHASE_1_DISCOVERY.md`](docs/PHASE_1_DISCOVERY.md) | Discovery service deep-dive |
| [`docs/API.md`](docs/API.md) | API endpoint reference |

---

## Common Commands

```bash
make start          # start all services
make stop           # stop all services
make logs           # tail all logs
make logs s=NAME    # tail one service (e.g. discovery)
make test           # run tests
make benchmark      # measure end-to-end latency
make seed           # load test URLs
make health         # check all services
make clean          # wipe everything
```

---

*PRN: 22510103 | 3-day build | Python + Go + Redis + Meilisearch + Qdrant*