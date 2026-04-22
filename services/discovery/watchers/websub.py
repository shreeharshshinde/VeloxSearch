"""
services/discovery/watchers/websub.py
======================================
WebSub (PubSubHubbub) subscriber — receives real-time pings from sites
the moment their content changes.

HOW WEBSUB WORKS:
-----------------
1. We expose a public HTTP callback URL (e.g. https://indexer.example.com/websub/callback).
2. We subscribe to a site's hub, telling it our callback URL.
3. The hub sends a GET request to verify our subscription (challenge-response).
4. When the site publishes new content, the hub sends a POST to our callback
   with the new content (Atom/RSS feed or HTML).
5. We parse the POST body, extract URLs, and enqueue them immediately.

This is the FASTEST discovery mechanism — latency from publish to our queue
is typically under 2 seconds.

MAJOR HUBs:
- Google PubSubHubbub Hub (feeds.pubsubhubbub.appspot.com)
- SuperFeedr (pubsubhubbub.superfeedr.com)
- Self-hosted hubs

SETUP:
The callback endpoint is registered in the API service (services/api/routers/).
This module handles the subscription management and notification parsing.
"""

import asyncio
import hashlib
import hmac
import secrets
import time
from typing import Optional
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

from shared.config import settings
from shared.logging import get_logger
from shared.models.url_task import URLTask, DiscoverySource

log = get_logger("discovery")

# Google's public WebSub hub — works for most RSS/Atom feeds
DEFAULT_HUB = "https://pubsubhubbub.appspot.com/subscribe"


class WebSubSubscriber:
    """
    Manages WebSub subscriptions and processes incoming notifications.

    Subscriptions are stored in Redis so they survive restarts.
    Each subscription has a secret used to verify the hub's HMAC signature.
    """

    def __init__(
        self,
        redis_client,
        on_url_found,         # async callback: (URLTask) -> None
        callback_url: str = None,
        hub_url: str = DEFAULT_HUB,
    ):
        self._redis = redis_client
        self._on_url_found = on_url_found
        self._callback_url = callback_url or settings.websub_callback_url
        self._hub_url = hub_url
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": settings.crawler_user_agent},
        )
        log.info("WebSub subscriber started", callback=self._callback_url)
        # Re-subscribe to all known topics (renew expired subscriptions)
        await self._renew_subscriptions()

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()

    # ── Subscription management ───────────────────────────────────────────────

    async def subscribe(self, topic_url: str, hub_url: str = None) -> bool:
        """
        Subscribe to a topic (RSS/Atom feed URL) via its hub.

        Args:
            topic_url: The feed URL to subscribe to
            hub_url:   The hub URL (defaults to Google's public hub)

        Returns:
            True if subscription request was sent successfully
        """
        hub = hub_url or self._hub_url
        secret = secrets.token_hex(32)
        lease_seconds = 86400 * 7  # 7 days

        try:
            resp = await self._client.post(hub, data={
                "hub.callback":       self._callback_url,
                "hub.mode":           "subscribe",
                "hub.topic":          topic_url,
                "hub.verify":         "async",
                "hub.secret":         secret,
                "hub.lease_seconds":  str(lease_seconds),
            })

            if resp.status_code in (202, 204):
                # Store subscription in Redis for renewal tracking
                sub_key = f"WEBSUB:sub:{hashlib.sha256(topic_url.encode()).hexdigest()[:16]}"
                await self._redis.hset(sub_key, mapping={
                    "topic":         topic_url,
                    "hub":           hub,
                    "secret":        secret,
                    "subscribed_at": str(time.time()),
                    "expires_at":    str(time.time() + lease_seconds),
                })
                await self._redis.expire(sub_key, lease_seconds)
                log.info("WebSub subscription requested", topic=topic_url, hub=hub)
                return True
            else:
                log.warning(
                    "WebSub subscription failed",
                    topic=topic_url,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return False

        except Exception as e:
            log.error("WebSub subscribe error", topic=topic_url, error=str(e))
            return False

    async def _renew_subscriptions(self) -> None:
        """Re-subscribe to all topics whose subscriptions are expiring soon."""
        keys = await self._redis.keys("WEBSUB:sub:*")
        for key in keys:
            data = await self._redis.hgetall(key)
            if not data:
                continue
            expires_at = float(data.get(b"expires_at", 0))
            # Renew if expiring within the next hour
            if expires_at - time.time() < 3600:
                topic = data[b"topic"].decode()
                hub = data[b"hub"].decode()
                log.info("Renewing WebSub subscription", topic=topic)
                await self.subscribe(topic, hub)

    # ── Notification handling ─────────────────────────────────────────────────

    async def handle_verification(self, params: dict) -> Optional[str]:
        """
        Handle hub verification challenge (GET request to callback URL).

        The hub sends:
          hub.mode      = "subscribe" | "unsubscribe"
          hub.topic     = the subscribed feed URL
          hub.challenge = random string we must echo back
          hub.lease_seconds = subscription duration

        We echo back hub.challenge to confirm the subscription.

        Returns:
            The challenge string to return as the HTTP response body,
            or None if the verification should be rejected.
        """
        mode      = params.get("hub.mode")
        topic     = params.get("hub.topic")
        challenge = params.get("hub.challenge")

        if mode not in ("subscribe", "unsubscribe") or not challenge:
            log.warning("Invalid WebSub verification request", params=params)
            return None

        log.info("WebSub verification successful", mode=mode, topic=topic)
        return challenge

    async def handle_notification(
        self,
        body: bytes,
        content_type: str,
        topic: str,
        signature: Optional[str] = None,
    ) -> int:
        """
        Handle incoming content notification (POST to callback URL).

        The hub sends the updated feed content.  We parse it and enqueue
        all new/updated URLs.

        Args:
            body:         Raw request body bytes
            content_type: MIME type of the body
            topic:        The feed URL this notification is for
            signature:    X-Hub-Signature header value for HMAC verification

        Returns:
            Number of URLs enqueued from this notification
        """
        # Verify HMAC signature if we have the secret
        if signature:
            if not await self._verify_signature(body, topic, signature):
                log.warning("WebSub HMAC verification failed", topic=topic)
                return 0

        # Parse the notification body
        urls = self._parse_notification(body, content_type, topic)

        # Enqueue each discovered URL
        count = 0
        for url in urls:
            task = URLTask(
                url=url,
                source=DiscoverySource.WEBSUB,
                metadata={"feed_url": topic},
            )
            await self._on_url_found(task)
            count += 1

        if count > 0:
            log.info("WebSub notification processed", topic=topic, urls_found=count)

        return count

    async def _verify_signature(self, body: bytes, topic: str, signature: str) -> bool:
        """
        Verify the X-Hub-Signature HMAC provided by the hub.

        Format: sha256=<hex_digest>
        """
        if not signature.startswith("sha256="):
            return False

        # Look up the secret for this topic
        sub_key = f"WEBSUB:sub:{hashlib.sha256(topic.encode()).hexdigest()[:16]}"
        data = await self._redis.hgetall(sub_key)
        if not data:
            log.debug("No subscription found for signature verification", topic=topic)
            return True  # allow if we don't have a record (e.g. old sub)

        secret = data.get(b"secret", b"").decode()
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        received = signature[len("sha256="):]
        return hmac.compare_digest(expected, received)

    def _parse_notification(
        self,
        body: bytes,
        content_type: str,
        topic: str,
    ) -> list[str]:
        """
        Extract page URLs from an Atom/RSS notification body.

        Supports:
        - Atom feeds (application/atom+xml)
        - RSS 2.0 (application/rss+xml)
        - HTML (text/html) — extract <link rel="canonical"> and <a href>
        """
        urls = []
        ct = content_type.lower()

        try:
            if "atom" in ct or "xml" in ct or "rss" in ct:
                urls = self._parse_feed(body)
            elif "html" in ct:
                urls = self._parse_html_links(body, topic)
            else:
                # Try XML first, fall back to treating as HTML
                try:
                    urls = self._parse_feed(body)
                except Exception:
                    urls = self._parse_html_links(body, topic)
        except Exception as e:
            log.warning("Notification parse error", topic=topic, error=str(e))

        return urls

    def _parse_feed(self, body: bytes) -> list[str]:
        """Parse Atom/RSS feed and extract entry/item URLs."""
        root = ET.fromstring(body)
        urls = []

        # Atom feed entries
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            link = entry.find("{http://www.w3.org/2005/Atom}link")
            if link is not None:
                href = link.get("href", "").strip()
                if href.startswith("http"):
                    urls.append(href)

        # RSS 2.0 items
        for item in root.iter("item"):
            link = item.find("link")
            if link is not None and link.text:
                url = link.text.strip()
                if url.startswith("http"):
                    urls.append(url)

        return urls

    def _parse_html_links(self, body: bytes, base_url: str) -> list[str]:
        """Extract URLs from HTML notification body."""
        from html.parser import HTMLParser

        class LinkParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "link" and attrs.get("rel") == "canonical":
                    self.links.append(attrs.get("href", ""))
                elif tag == "a":
                    href = attrs.get("href", "")
                    if href.startswith("http"):
                        self.links.append(href)

        parser = LinkParser()
        parser.feed(body.decode(errors="ignore"))
        return [u for u in parser.links if u.startswith("http")]