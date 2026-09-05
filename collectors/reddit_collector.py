"""Reddit Collector: fetches subreddit posts straight into cos.db.

Reddit blocks the Miniflux host (its Hetzner address has been getting 403 since
2026-06-05), but serves the same feeds fine from a residential connection with a
browser user agent. So these subreddits bypass Miniflux and are fetched here, on
whichever machine runs the pipeline.

Subreddits are the pain lane: people describe problems they could not solve, which
is what the announcement feeds in Miniflux never contain.

Usage:
    python collectors/reddit_collector.py
    python collectors/reddit_collector.py --dry-run
    python collectors/reddit_collector.py --subreddit TechSEO
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, finish_run, get_db_path, init_db, insert_feed, start_run
from cos.log import get_logger, log_with_data

logger = get_logger("reddit_collector")

ATOM = {"a": "http://www.w3.org/2005/Atom"}

# Reddit answers curl's default agent with 403 and a browser agent with 200.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

DEFAULT_SUBREDDITS = [
    "GoogleAnalytics",
    "GoogleTagManager",
    "analytics",
    "PPC",
    "TechSEO",
    "shopify",
    "ecommerce",
]

# Reddit rate limits hard. One request every few seconds keeps a whole run under
# the limit, and a manual run is not in a hurry.
REQUEST_GAP_SECONDS = 20
RETRY_BACKOFF_SECONDS = (30, 60, 120)
DEFAULT_PER_SUBREDDIT = 25


def reddit_config(config: dict) -> dict:
    section = config.get("reddit", {})
    return {
        "subreddits": section.get("subreddits", DEFAULT_SUBREDDITS),
        "per_subreddit": section.get("per_subreddit", DEFAULT_PER_SUBREDDIT),
        "timeout": section.get("timeout", 30),
    }


def fetch_subreddit(name: str, client: httpx.Client, limit: int) -> list[dict]:
    """Fetch one subreddit's newest posts. Returns [] rather than raising, so one
    blocked or renamed subreddit does not cost the whole run.

    Reddit allows the first request and then answers 429 for a while, so a fixed
    gap between subreddits is not enough on its own: each 429 is waited out here.
    """
    url = f"https://www.reddit.com/r/{name}/new/.rss"

    for attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        try:
            resp = client.get(url)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After") or backoff)
                logger.warning(
                    "r/%s: rate limited (attempt %d/%d), waiting %ds",
                    name,
                    attempt,
                    len(RETRY_BACKOFF_SECONDS),
                    wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.text)
            break
        except Exception as e:
            logger.warning("r/%s: fetch failed (%s)", name, e)
            return []
    else:
        logger.warning("r/%s: still rate limited, giving up", name)
        return []

    entries = []
    for el in root.findall("a:entry", ATOM)[:limit]:
        link = el.find("a:link", ATOM)
        author = el.find("a:author/a:name", ATOM)
        content = el.find("a:content", ATOM)
        title = el.find("a:title", ATOM)
        updated = el.find("a:updated", ATOM)
        entry_id = el.find("a:id", ATOM)

        if title is None or entry_id is None:
            continue

        entries.append(
            {
                "id": entry_id.text,
                "subreddit": name,
                "title": title.text or "",
                "url": link.get("href") if link is not None else "",
                "author": (author.text if author is not None else "") or "",
                "content": (content.text if content is not None else "") or "",
                "published_at": (updated.text if updated is not None else "")
                or datetime.now(timezone.utc).isoformat(),
            }
        )
    return entries


def parse_entry(entry: dict) -> dict:
    """Shape a Reddit entry like a Miniflux one so the rest of the pipeline, taste
    scoring included, cannot tell the difference."""
    from cos.language import detect as detect_language

    content = entry["content"][:2000]
    title = entry["title"]
    subreddit = entry["subreddit"]

    return {
        "id": entry["id"],
        "feed_id": 0,
        "feed_title": f"reddit: r/{subreddit}",
        "title": title,
        "url": entry["url"],
        "author": entry["author"],
        "content": content,
        "published_at": entry["published_at"],
        "reading_time": max(1, len(content.split()) // 200),
        "tags": ["pain", f"r/{subreddit}"],
        # Pain lane: worth a look before the announcement feeds.
        "priority": "P3",
        "language": detect_language(f"{title} {content}"),
    }


def collect_reddit(
    config: dict, dry_run: bool = False, only: str | None = None
) -> dict:
    cfg = reddit_config(config)
    subreddits = [only] if only else cfg["subreddits"]
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    entries: list[dict] = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml, text/xml"}
    with httpx.Client(
        timeout=cfg["timeout"], headers=headers, follow_redirects=True
    ) as client:
        for i, name in enumerate(subreddits):
            if i:
                time.sleep(REQUEST_GAP_SECONDS)
            found = fetch_subreddit(name, client, cfg["per_subreddit"])
            log_with_data(logger, logging.INFO, f"r/{name}: {len(found)} entries")
            entries.extend(found)

    if dry_run:
        for entry in entries:
            parsed = parse_entry(entry)
            print(f"  [{parsed['feed_title']}] {parsed['title'][:70]}")
        print(f"\n--- DRY RUN: {len(entries)} entries found, 0 written ---")
        stats["processed"] = len(entries)
        return stats

    db_path = get_db_path(config)
    init_db(db_path)

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="reddit")

        for entry in entries:
            try:
                parsed = parse_entry(entry)
                if insert_feed(conn, parsed) is not None:
                    stats["processed"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                stats["failed"] += 1
                log_with_data(
                    logger, logging.ERROR, f"Failed on {entry.get('id')}: {exc}"
                )

        finish_run(
            conn,
            run_id,
            status="completed" if stats["failed"] == 0 else "partial",
            items_processed=stats["processed"],
            items_failed=stats["failed"],
        )

    return stats


def main():
    parser = argparse.ArgumentParser(description="Collect subreddit posts into cos.db")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--subreddit", help="Fetch only this subreddit")
    args = parser.parse_args()

    config = load_config(args.config) if args.config else load_config()
    stats = collect_reddit(config, dry_run=args.dry_run, only=args.subreddit)
    print(
        f"Processed: {stats['processed']}, "
        f"Skipped: {stats['skipped']}, Failed: {stats['failed']}"
    )


if __name__ == "__main__":
    main()
