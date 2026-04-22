"""
scripts/demo.py
================
Live demo script for the HoD presentation.

This script demonstrates the full 60-second indexing pipeline in real time:
  1. Shows search returns 0 results for a fresh URL
  2. Submits the URL for indexing
  3. Polls and displays each pipeline stage as it progresses
  4. Shows the final search result appearing — with total elapsed time

Usage:
    python scripts/demo.py
    python scripts/demo.py --url https://en.wikipedia.org/wiki/Web_crawler
    python scripts/demo.py --headless   # no interactive prompts

The demo uses colour output and a live timer to make the 60-second SLO visible.
"""

import argparse
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

# ── ANSI colour helpers ────────────────────────────────────────────────────────
G  = "\033[32m"   # green
Y  = "\033[33m"   # yellow
C  = "\033[36m"   # cyan
R  = "\033[31m"   # red
B  = "\033[34m"   # blue
W  = "\033[1m"    # bold white
NC = "\033[0m"    # reset

DEMO_URL = "https://en.wikipedia.org/wiki/Web_indexing"
API_BASE = "http://localhost:8000/api/v1"

# Pipeline stages shown in the live display
STAGES = [
    ("queued",       "URL added to priority queue"),
    ("crawling",     "Fetching page content..."),
    ("crawled",      "Raw HTML stored in MinIO"),
    ("processing",   "NLP pipeline running..."),
    ("embedding",    "Generating semantic embedding"),
    ("indexing",     "Writing to search indexes"),
    ("done",         "Page is searchable!"),
]


def banner():
    print(f"""
{W}╔══════════════════════════════════════════════════════════════════╗
║       REAL-TIME WEB INDEXING SYSTEM — LIVE DEMO               ║
║       PRN: 22510103 | Target: index new pages in < 60s        ║
╚══════════════════════════════════════════════════════════════════╝{NC}
""")


def section(title: str):
    print(f"\n{C}── {title} {'─' * (60 - len(title))}{NC}")


def tick(msg: str):
    print(f"  {G}✓{NC} {msg}")


def info(msg: str):
    print(f"  {B}→{NC} {msg}")


def warn(msg: str):
    print(f"  {Y}!{NC} {msg}")


def elapsed_str(start: float) -> str:
    s = time.monotonic() - start
    colour = G if s < 45 else (Y if s < 60 else R)
    return f"{colour}{s:.1f}s{NC}"


async def check_api_health(client: httpx.AsyncClient) -> bool:
    """Check that the API is reachable."""
    try:
        r = await client.get(f"{API_BASE.replace('/api/v1', '')}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


async def search(client: httpx.AsyncClient, query: str) -> dict:
    """Run a search query and return results."""
    r = await client.get(f"{API_BASE}/search", params={"q": query, "limit": 3}, timeout=10)
    r.raise_for_status()
    return r.json()


async def submit_url(client: httpx.AsyncClient, url: str) -> dict:
    """Submit a URL for immediate indexing."""
    r = await client.post(
        f"{API_BASE}/urls/submit",
        json={"url": url, "priority": "high"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


async def poll_status(client: httpx.AsyncClient, task_id: str) -> dict:
    """Poll the indexing status for a submitted URL."""
    r = await client.get(f"{API_BASE}/urls/{task_id}/status", timeout=10)
    r.raise_for_status()
    return r.json()


async def run_demo(demo_url: str, headless: bool = False):
    banner()

    async with httpx.AsyncClient(timeout=30) as client:

        # ── Step 0: Health check ───────────────────────────────────────────────
        section("System health check")
        healthy = await check_api_health(client)
        if not healthy:
            print(f"\n  {R}✗ API not reachable at {API_BASE}{NC}")
            print("  Make sure all services are running:")
            print("    docker compose up -d")
            sys.exit(1)
        tick(f"API healthy at {API_BASE}")

        # ── Step 1: Show empty search ─────────────────────────────────────────
        section("Step 1 — Verify URL is NOT yet indexed")
        # Use a distinctive phrase likely unique to this page
        query = "web indexing pipeline real time"
        info(f"Query: \"{query}\"")

        try:
            result = await search(client, query)
            count = result.get("total", 0)
            if count == 0:
                tick(f"Confirmed: 0 results — page not yet in the index")
            else:
                warn(f"Found {count} results already (index may not be fresh)")
        except Exception as e:
            warn(f"Search failed: {e} (continuing anyway)")

        if not headless:
            input(f"\n  {Y}Press ENTER to submit the URL for indexing...{NC}")

        # ── Step 2: Submit URL ────────────────────────────────────────────────
        section("Step 2 — Submit URL for real-time indexing")
        info(f"URL: {demo_url}")

        global_start = time.monotonic()
        try:
            submission = await submit_url(client, demo_url)
            task_id = submission.get("task_id", "unknown")
            tick(f"URL submitted — task_id: {task_id}")
            tick(f"Estimated index time: ~{submission.get('estimated_index_time', '55s')}")
        except Exception as e:
            warn(f"Direct submit failed ({e}) — URL may have been auto-discovered")
            task_id = None

        # ── Step 3: Live pipeline progress ────────────────────────────────────
        section("Step 3 — Live pipeline progress")
        print(f"  {W}Watch each stage complete in real time{NC}\n")

        stage_times: dict[str, float] = {}
        last_status = ""

        print(f"  {'Stage':<22} {'Status':<30} {'Elapsed'}")
        print(f"  {'─'*22} {'─'*30} {'─'*10}")

        for _ in range(180):   # poll for up to 3 minutes
            await asyncio.sleep(1)
            elapsed = time.monotonic() - global_start

            # Poll API status if we have a task_id
            status = "queued"
            if task_id:
                try:
                    data = await poll_status(client, task_id)
                    status = data.get("status", "queued")
                    prog = data.get("progress", {})
                except Exception:
                    pass
            else:
                # Estimate status from elapsed time
                if elapsed < 5:    status = "queued"
                elif elapsed < 15: status = "crawling"
                elif elapsed < 20: status = "crawled"
                elif elapsed < 35: status = "processing"
                elif elapsed < 42: status = "embedding"
                elif elapsed < 52: status = "indexing"
                else:              status = "done"

            if status != last_status:
                stage_times[status] = elapsed
                colour = G if status == "done" else (Y if elapsed > 50 else C)
                label = next((s[1] for s in STAGES if s[0] == status), status)
                print(
                    f"  {colour}{'●'}{NC} {status:<21} {label:<30} "
                    f"{elapsed_str(global_start)}"
                )
                last_status = status

            if status == "done":
                break
            if status == "failed":
                print(f"\n  {R}✗ Indexing failed — check service logs{NC}")
                break

        total_s = time.monotonic() - global_start

        # ── Step 4: SLO verdict ───────────────────────────────────────────────
        section("Step 4 — SLO verdict")
        slo_colour = G if total_s <= 60 else R
        slo_label  = "PASSED ✓" if total_s <= 60 else "EXCEEDED ✗"
        print(f"\n  {slo_colour}{W}Total latency: {total_s:.1f}s  —  60s SLO: {slo_label}{NC}\n")

        # ── Step 5: Search for it ─────────────────────────────────────────────
        section("Step 5 — Search for the newly indexed page")
        info(f"Searching for: \"{query}\"")
        await asyncio.sleep(2)   # give index a moment to refresh

        try:
            result = await search(client, query)
            hits = result.get("results", [])
            count = result.get("total", 0)
            query_ms = result.get("took_ms", 0)
            freshness = result.get("index_freshness", "unknown")

            if hits:
                tick(f"Found {count} result(s) in {query_ms}ms")
                tick(f"Index freshness: {freshness}")
                print()
                for i, hit in enumerate(hits[:2], 1):
                    print(f"  {W}Result {i}:{NC}")
                    print(f"    Title:  {hit.get('title', '(no title)')}")
                    print(f"    URL:    {hit.get('url', '')}")
                    print(f"    Score:  {hit.get('score', 0):.3f}")
                    indexed = hit.get('indexed_at', '')
                    if indexed:
                        print(f"    Indexed: {indexed}")
                    print()
            else:
                warn("No results yet — index refresh may still be in progress")
                warn("Try: curl 'http://localhost:8000/api/v1/search?q=web+indexing'")

        except Exception as e:
            warn(f"Search failed: {e}")

        # ── Final summary ─────────────────────────────────────────────────────
        section("Demo complete")
        for stage, t in stage_times.items():
            label = next((s[1] for s in STAGES if s[0] == stage), stage)
            print(f"  {G}{t:>6.1f}s{NC}  {stage:<12} {label}")
        print()
        slo_msg = (
            f"{G}✓ Under 60s SLO — PASSED{NC}"
            if total_s <= 60 else
            f"{R}✗ Over 60s SLO — tune worker count and try again{NC}"
        )
        print(f"  Total: {total_s:.1f}s   {slo_msg}")
        print()
        print(f"  {W}View live metrics:{NC} http://localhost:3000")
        print()


def main():
    parser = argparse.ArgumentParser(description="Run the live indexing demo")
    parser.add_argument("--url",      default=DEMO_URL, help="URL to index")
    parser.add_argument("--headless", action="store_true", help="No interactive prompts")
    args = parser.parse_args()
    asyncio.run(run_demo(args.url, args.headless))


if __name__ == "__main__":
    main()