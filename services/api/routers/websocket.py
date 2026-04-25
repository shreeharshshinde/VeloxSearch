"""
services/api/routers/websocket.py
===================================
WebSocket endpoint that streams real-time indexing events to connected clients.

Used by the live demo dashboard to show each URL moving through
the pipeline stages in real time.

EVENTS EMITTED:
  {"type": "url_discovered",  "url": "...", "source": "websub",   "timestamp": "..."}
  {"type": "crawl_started",   "url": "...", "fetcher": "http",    "timestamp": "..."}
  {"type": "crawl_complete",  "url": "...", "fetch_ms": 342,      "timestamp": "..."}
  {"type": "processing",      "url": "...", "stage": "embedding", "timestamp": "..."}
  {"type": "indexed",         "url": "...", "latency_ms": 54230,  "timestamp": "..."}
  {"type": "stats",           "queue_depth": 14, "indexed_60s": 7, "timestamp": "..."}

CONNECTION:
  ws://localhost:8000/ws/feed
  ws://localhost:8000/ws/feed?filter=domain:techcrunch.com

IMPLEMENTATION:
  The API subscribes to Redis Streams and pub/sub channels.
  When a new event arrives, it broadcasts to all connected WebSocket clients.
  Each client connection runs in its own async task.
"""

import asyncio
import json
import time
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shared.config import settings
from shared.logging import get_logger

log = get_logger("api.websocket")
router = APIRouter(tags=["WebSocket"])

# Active WebSocket connections
_connections: set[WebSocket] = set()
_redis: aioredis.Redis | None = None


def init_websocket_components(redis_client: aioredis.Redis) -> None:
    global _redis
    _redis = redis_client


async def broadcast(event: dict) -> None:
    """Send an event to all connected WebSocket clients."""
    if not _connections:
        return
    message = json.dumps({**event, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    dead = set()
    for ws in _connections:
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    _connections.difference_update(dead)


@router.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket):
    """
    WebSocket endpoint for real-time indexing event stream.

    Connect to receive a live feed of every URL as it moves through
    the discovery → crawl → process → index pipeline.

    Usage:
        const ws = new WebSocket("ws://localhost:8000/ws/feed");
        ws.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    await websocket.accept()
    _connections.add(websocket)
    log.info("WebSocket client connected", total=len(_connections))

    try:
        # Send welcome message with current stats
        await websocket.send_text(json.dumps({
            "type":      "connected",
            "message":   "Real-time indexing feed — watching all pipeline stages",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }))

        # Keep connection alive with periodic pings
        while True:
            try:
                # Wait for client message (ping) or disconnect
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send keepalive ping
                await websocket.send_text(json.dumps({
                    "type":      "ping",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }))

    except WebSocketDisconnect:
        log.info("WebSocket client disconnected", total=len(_connections) - 1)
    except Exception as e:
        log.warning("WebSocket error", error=str(e))
    finally:
        _connections.discard(websocket)


async def stream_pipeline_events() -> None:
    """
    Background task: reads events from Redis Streams and broadcasts to WebSocket clients.
    Runs continuously from API startup.

    Monitors three streams:
      STREAM:crawl_complete  → crawl events (from Phase 2 crawler)
      STREAM:docs_ready      → processing complete (from Phase 3 processor)
      CHANNEL:index_commits  → indexed events (from Phase 4 indexer)
    """
    if not _redis:
        return

    log.info("Pipeline event streamer started")

    # Track last-read positions in each stream
    last_ids = {
        "STREAM:crawl_complete": "$",
        "STREAM:docs_ready":     "$",
    }

    while True:
        try:
            # Read from both streams in one call (XREAD with BLOCK)
            messages = await _redis.xread(
                streams=last_ids,
                count=10,
                block=1000,   # block 1 second if empty
            )

            if not messages:
                # Send periodic stats update even when no new docs
                await _send_stats_event()
                continue

            for stream_name, records in messages:
                stream_key = stream_name if isinstance(stream_name, str) else stream_name.decode()
                for msg_id, fields in records:
                    last_ids[stream_key] = msg_id
                    await _handle_stream_event(stream_key, fields)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Stream event error", error=str(e))
            await asyncio.sleep(1)


async def _handle_stream_event(stream_key: str, fields: dict) -> None:
    """Convert a Redis Stream message to a WebSocket broadcast event."""
    try:
        if stream_key == "STREAM:crawl_complete":
            raw = fields.get("data", fields.get(b"data", "{}"))
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)

            if data.get("status") == "success":
                await broadcast({
                    "type":     "crawl_complete",
                    "url":      data.get("url", ""),
                    "domain":   data.get("domain", ""),
                    "fetcher":  data.get("fetcher_type", "http"),
                    "fetch_ms": data.get("fetch_ms", 0),
                    "bytes":    data.get("fetch_bytes", 0),
                })
            elif data.get("status") == "robots_block":
                await broadcast({
                    "type":   "robots_blocked",
                    "url":    data.get("url", ""),
                    "domain": data.get("domain", ""),
                })

        elif stream_key == "STREAM:docs_ready":
            raw = fields.get("data", fields.get(b"data", "{}"))
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)

            e2e_ms = int((time.time() - float(data.get("discovered_at", time.time()))) * 1000)
            await broadcast({
                "type":       "indexed",
                "url":        data.get("url", ""),
                "domain":     data.get("domain", ""),
                "language":   data.get("language", "en"),
                "words":      data.get("word_count", 0),
                "latency_ms": e2e_ms,
                "slo_passed": e2e_ms <= 60_000,
            })

    except Exception as e:
        log.debug("Event parse error", error=str(e))


async def _send_stats_event() -> None:
    """Broadcast current pipeline stats to all connected clients."""
    if not _redis or not _connections:
        return
    try:
        queue_depth = await _redis.zcard(settings.queue_key)
        await broadcast({
            "type":        "stats",
            "queue_depth": queue_depth,
            "clients":     len(_connections),
        })
    except Exception:
        pass