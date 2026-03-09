"""Feed Collector: fetches unread Miniflux entries and writes to cos.db.

Usage:
    python collectors/feed_collector.py
    python collectors/feed_collector.py --dry-run
    python collectors/feed_collector.py --config /path/to/config.toml
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, finish_run, get_db_path, init_db, insert_feed, start_run
from cos.log import get_logger, log_with_data

logger = get_logger("feed_collector")

try:
    import httpx

    def _get(url: str, headers: dict, params: dict) -> dict:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

except ImportError:
    import urllib.request

    def _get(url: str, headers: dict, params: dict) -> dict:
        from urllib.parse import urlencode

        full_url = f"{url}?{urlencode(params)}"
        req = urllib.request.Request(full_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())


def estimate_priority(reading_time: int) -> str:
    """P3 for short reads (<= 3 min), P4 otherwise."""
    return "P3" if reading_time <= 3 else "P4"


def fetch_entries(config: dict, limit: int | None = None) -> list[dict]:
    """Fetch unread entries from Miniflux API."""
    mx = config["miniflux"]
    base_url = mx["base_url"].rstrip("/")
    api_token = mx["api_token"]
    max_entries = limit or mx.get("max_entries", 50)
    lookback_hours = mx.get("lookback_hours", 24)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    published_after = int(cutoff.timestamp())

    url = f"{base_url}/v1/entries"
    headers = {"X-Auth-Token": api_token}
    params = {
        "status": "unread",
        "order": "published_at",
        "direction": "desc",
        "limit": max_entries,
        "published_after": published_after,
    }

    data = _get(url, headers, params)
    return data.get("entries", [])


def parse_entry(entry: dict) -> dict:
    """Parse a Miniflux entry into our feeds schema."""
    content = entry.get("content", "") or ""
    if len(content) > 2000:
        content = content[:2000]

    reading_time = entry.get("reading_time", 0) or 0
    tags = entry.get("tags") or []
    if entry.get("feed", {}).get("category", {}).get("title"):
        cat = entry["feed"]["category"]["title"]
        if cat not in tags:
            tags.append(cat)

    return {
        "id": str(entry["id"]),
        "feed_id": entry.get("feed_id", 0),
        "feed_title": entry.get("feed", {}).get("title", "Unknown"),
        "title": entry.get("title", "Untitled"),
        "url": entry.get("url", ""),
        "author": entry.get("author", ""),
        "content": content,
        "published_at": entry.get(
            "published_at", datetime.now(timezone.utc).isoformat()
        ),
        "reading_time": reading_time,
        "tags": tags,
        "priority": estimate_priority(reading_time),
    }


def collect_feeds(
    config: dict, dry_run: bool = False, limit: int | None = None
) -> dict:
    """Fetch Miniflux entries and write to cos.db.

    Args:
        config: loaded config.toml
        dry_run: if True, print titles without writing to db
        limit: override max_entries from config
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    entries = fetch_entries(config, limit=limit)
    log_with_data(
        logger, logging.INFO, f"Fetched {len(entries)} unread entries from Miniflux"
    )

    if dry_run:
        for entry in entries:
            parsed = parse_entry(entry)
            rt = parsed["reading_time"]
            pri = parsed["priority"]
            print(f"[{pri}] ({rt}m) {parsed['feed_title']}: {parsed['title']}")
            stats["processed"] += 1
        print(f"\n--- DRY RUN: {stats['processed']} entries found, 0 written ---")
        return stats

    db_path = get_db_path(config)
    init_db(db_path)

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="feed")

        for entry in entries:
            try:
                parsed = parse_entry(entry)
                queue_id = insert_feed(conn, parsed)

                if queue_id is not None:
                    stats["processed"] += 1
                    log_with_data(
                        logger,
                        logging.INFO,
                        f"Collected: {parsed['title'][:60]}",
                        {"feed": parsed["feed_title"], "priority": parsed["priority"]},
                    )
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                stats["failed"] += 1
                log_with_data(
                    logger,
                    logging.ERROR,
                    f"Failed to process entry {entry.get('id')}: {exc}",
                )

        status = "completed" if stats["failed"] == 0 else "partial"
        finish_run(
            conn,
            run_id,
            status=status,
            items_processed=stats["processed"],
            items_failed=stats["failed"],
        )

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Collect Miniflux feed entries")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print entries without writing to db"
    )
    parser.add_argument(
        "--limit", type=int, help="Max entries to fetch (overrides config)"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    stats = collect_feeds(config, dry_run=args.dry_run, limit=args.limit)

    print(
        f"Processed: {stats['processed']}, Skipped: {stats['skipped']}, Failed: {stats['failed']}"
    )


if __name__ == "__main__":
    main()
