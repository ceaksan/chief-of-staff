"""One-shot Miniflux backfill for taste training.

Pulls entries across all statuses (unread + read + removed) with pagination,
ignores lookback_hours, and upserts into the feeds table.

Usage:
    python collectors/feed_backfill.py --max 500
    python collectors/feed_backfill.py --max 10000 --statuses unread,read
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from collectors.feed_collector import parse_entry
from cos.config import load_config
from cos.db import connect, get_db_path, init_db, insert_feed
from cos.log import get_logger, log_with_data

logger = get_logger("feed_backfill")

PAGE_SIZE = 100


def fetch_page(base_url: str, token: str, status: str, offset: int) -> list[dict]:
    resp = httpx.get(
        f"{base_url.rstrip('/')}/v1/entries",
        headers={"X-Auth-Token": token},
        params={
            "status": status,
            "order": "published_at",
            "direction": "desc",
            "limit": PAGE_SIZE,
            "offset": offset,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("entries", []) or []


def backfill(config: dict, max_entries: int, statuses: list[str]) -> dict:
    mx = config["miniflux"]
    base_url = mx["base_url"]
    token = mx["api_token"]
    db_path = get_db_path(config)

    stats = {"fetched": 0, "inserted": 0, "skipped": 0}

    for status in statuses:
        offset = 0
        while stats["fetched"] < max_entries:
            entries = fetch_page(base_url, token, status, offset)
            if not entries:
                break
            log_with_data(
                logger,
                logging.INFO,
                f"[{status}] offset={offset} page={len(entries)} total={stats['fetched']}",
            )
            for e in entries:
                try:
                    parsed = parse_entry(e)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("parse failed: %s", exc)
                    stats["skipped"] += 1
                    continue
                with connect(db_path) as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM feeds WHERE id = ?", (parsed["id"],)
                    ).fetchone()
                    if exists:
                        stats["skipped"] += 1
                        continue
                    insert_feed(conn, parsed)
                    stats["inserted"] += 1
                stats["fetched"] += 1
                if stats["fetched"] >= max_entries:
                    break
            offset += len(entries)
            if len(entries) < PAGE_SIZE:
                break

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=2000)
    parser.add_argument("--statuses", default="unread,read", help="comma-separated")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    init_db(get_db_path(config))
    stats = backfill(
        config, args.max, [s.strip() for s in args.statuses.split(",") if s.strip()]
    )
    print(f"done: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
