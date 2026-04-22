"""
services/discovery/watchers/ct_logs.py
========================================
Certificate Transparency Log scanner — discovers brand-new domains
by monitoring SSL certificate issuances in real time.

WHAT ARE CT LOGS?
-----------------
Every SSL/TLS certificate issued by a CA (Certificate Authority) must be
logged in a publicly auditable Certificate Transparency (CT) log.  This
means every new HTTPS website that gets a certificate appears in these logs
within seconds of the cert being issued.

We connect to Certstream — a service that aggregates all major CT logs and
streams new certificates via WebSocket in real time.

WHAT WE DO WITH EACH CERT:
1. Extract all domains from the certificate (CN + SANs)
2. Filter out wildcard domains (*.example.com — not directly crawlable)
3. Filter out known bad patterns (IP addresses, internal domains)
4. Construct https:// URL for each domain
5. Enqueue for crawling

LATENCY:
Certstream delivers new certificates within ~5 seconds of issuance.
Combined with our crawl pipeline, new domains are indexed within ~60 seconds.

VOLUME:
Certstream delivers ~5–10 certificates per second.
Each cert can have multiple SANs, so we process ~20–50 domains/second.
The Bloom filter handles deduplication efficiently.

REFERENCE:
- https://certificate.transparency.dev/
- https://github.com/CaliDog/certstream-python
- Certstream WebSocket: wss://certstream.calidog.io
"""

import asyncio
import json
import re
import time
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from shared.config import settings
from shared.logging import get_logger
from shared.models.url_task import URLTask, DiscoverySource

log = get_logger("discovery")

# Patterns for domains we should NOT crawl
SKIP_PATTERNS = [
    re.compile(r"^\*\."),                    # wildcard domains
    re.compile(r"^[\d.]+$"),                 # IPv4 addresses
    re.compile(r"\.local$"),                 # local network domains
    re.compile(r"\.internal$"),
    re.compile(r"\.test$"),
    re.compile(r"\.example(\.com)?$"),
    re.compile(r"\.invalid$"),
    re.compile(r"^localhost"),
]

# TLDs that are unlikely to have useful public content
SKIP_TLDS = {".onion", ".i2p", ".bit", ".coin", ".emc"}

# Maximum domains to enqueue per certificate (avoid cert spam)
MAX_DOMAINS_PER_CERT = 10


class CTLogScanner:
    """
    Streams new SSL certificates from Certstream and discovers new domains.

    Reconnects automatically on connection loss with exponential backoff.
    """

    def __init__(
        self,
        on_url_found,          # async callback: (URLTask) -> None
        certstream_url: str = None,
    ):
        self._on_url_found = on_url_found
        self._certstream_url = certstream_url or settings.certstream_url
        self._running = False

        # Stats (for logging)
        self._certs_processed = 0
        self._domains_discovered = 0
        self._domains_skipped = 0
        self._last_stats_log = time.time()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the CT log scanner. Reconnects on failure."""
        self._running = True
        log.info("CT log scanner starting", url=self._certstream_url)
        await self._connect_with_retry()

    async def stop(self) -> None:
        self._running = False
        log.info("CT log scanner stopped")

    # ── WebSocket connection ───────────────────────────────────────────────────

    async def _connect_with_retry(self) -> None:
        """
        Connect to Certstream WebSocket.
        Reconnects with exponential backoff (1s → 2s → 4s → ... → 60s max).
        """
        backoff = 1.0

        while self._running:
            try:
                log.info("Connecting to Certstream", url=self._certstream_url)
                async with websockets.connect(
                    self._certstream_url,
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=2**20,  # 1MB max message size
                ) as ws:
                    backoff = 1.0  # reset on successful connection
                    log.info("Connected to Certstream — streaming certificates")
                    await self._stream(ws)

            except ConnectionClosed as e:
                log.warning("Certstream connection closed", reason=str(e))
            except OSError as e:
                log.error("Certstream connection error", error=str(e))
            except Exception as e:
                log.error("Unexpected Certstream error", error=str(e))

            if not self._running:
                break

            log.info(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _stream(self, ws) -> None:
        """Process incoming WebSocket messages from Certstream."""
        async for raw_message in ws:
            if not self._running:
                break
            try:
                message = json.loads(raw_message)
                await self._process_message(message)
                await self._log_stats()
            except json.JSONDecodeError:
                log.debug("Non-JSON message from Certstream, skipping")
            except Exception as e:
                log.warning("Error processing cert message", error=str(e))

    # ── Certificate processing ────────────────────────────────────────────────

    async def _process_message(self, message: dict) -> None:
        """
        Process a single Certstream message.

        Certstream message structure:
        {
          "message_type": "certificate_update",
          "data": {
            "cert_index": 123456789,
            "cert_link": "https://crt.sh/?id=...",
            "leaf_cert": {
              "subject": { "CN": "example.com" },
              "all_domains": ["example.com", "www.example.com"],
              "not_before": "...",
              "not_after": "...",
              "issuer": { "O": "Let's Encrypt" }
            },
            "seen": 1704067200.0
          }
        }
        """
        if message.get("message_type") != "certificate_update":
            return

        data = message.get("data", {})
        leaf = data.get("leaf_cert", {})
        all_domains = leaf.get("all_domains", [])

        if not all_domains:
            return

        self._certs_processed += 1

        # Process each domain (up to MAX_DOMAINS_PER_CERT)
        processed = 0
        for domain in all_domains[:MAX_DOMAINS_PER_CERT]:
            if self._should_skip(domain):
                self._domains_skipped += 1
                continue

            url = f"https://{domain}/"
            task = URLTask(
                url=url,
                source=DiscoverySource.CT_LOG,
                metadata={
                    "cert_index": data.get("cert_index"),
                    "issuer": leaf.get("issuer", {}).get("O", "unknown"),
                    "cert_not_after": leaf.get("not_after"),
                },
            )
            await self._on_url_found(task)
            self._domains_discovered += 1
            processed += 1

    def _should_skip(self, domain: str) -> bool:
        """
        Determine if a domain should be skipped.

        Skips:
        - Wildcard domains (*.example.com)
        - IP addresses
        - Local/internal/test domains
        - Known bad TLDs
        - Empty or too-short domains
        """
        if not domain or len(domain) < 4:
            return True

        domain_lower = domain.lower()

        # Check TLD
        for tld in SKIP_TLDS:
            if domain_lower.endswith(tld):
                return True

        # Check skip patterns
        for pattern in SKIP_PATTERNS:
            if pattern.search(domain_lower):
                return True

        return False

    # ── Stats logging ─────────────────────────────────────────────────────────

    async def _log_stats(self) -> None:
        """Log throughput stats every 60 seconds."""
        now = time.time()
        if now - self._last_stats_log < 60:
            return
        log.info(
            "CT log scanner stats",
            certs_processed=self._certs_processed,
            domains_discovered=self._domains_discovered,
            domains_skipped=self._domains_skipped,
            rate_per_min=round(self._domains_discovered / max(1, (now - self._last_stats_log) / 60), 1),
        )
        self._last_stats_log = now
        # Reset counters
        self._certs_processed = 0
        self._domains_discovered = 0
        self._domains_skipped = 0