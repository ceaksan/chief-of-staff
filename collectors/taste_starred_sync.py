"""Sync starred entries from Miniflux as implicit 'relevant' labels.

The user starring an item in Reeder/Miniflux is a strong positive signal.
This script pulls all currently-starred entries and writes a 'relevant' label
for every matching feed id, unless the user has explicitly labeled it as
'not_relevant' (explicit no always wins over implicit yes).

Usage:
    python collectors/taste_starred_sync.py
    python collectors/taste_starred_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from collectors.feed_collector import parse_entry
from cos.config import load_config
from cos.db import connect, get_db_path, init_db, insert_feed
from cos.taste import add_label, ensure_schema

PAGE_SIZE = 200


def fetch_starred(base_url: str, token: str) -> list[dict]:
    results: list[dict] = []
    offset = 0
    while True:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/v1/entries",
            headers={"X-Auth-Token": token},
            params={"starred": "true", "limit": PAGE_SIZE, "offset": offset},
            timeout=60,
        )
        resp.raise_for_status()
        entries = resp.json().get("entries", []) or []
        if not entries:
            break
        results.extend(entries)
        offset += len(entries)
        if len(entries) < PAGE_SIZE:
            break
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    mx = config["miniflux"]
    db_path = get_db_path(config)
    ensure_schema(db_path)

    starred = fetch_starred(mx["base_url"], mx["api_token"])
    print(f"found {len(starred)} starred entries in miniflux")

    if not starred:
        return 0

    starred_ids = {str(e["id"]) for e in starred}

    with connect(db_path) as conn:
        known_rows = conn.execute(
            f"SELECT id, title FROM feeds WHERE id IN ({','.join('?' * len(starred_ids))})",
            tuple(starred_ids),
        ).fetchall()
        known_ids = {r["id"]: r["title"] for r in known_rows}
        existing = (
            {
                r["feed_id"]: r["label"]
                for r in conn.execute(
                    f"SELECT feed_id, label FROM taste_labels WHERE feed_id IN ({','.join('?' * len(known_ids))})",
                    tuple(known_ids.keys()) if known_ids else tuple(),
                ).fetchall()
            }
            if known_ids
            else {}
        )

    missing = starred_ids - set(known_ids.keys())

    # Ingest any starred entries that are older than the regular backfill window.
    # They carry a strong positive signal so we always pull them into the feeds table.
    if missing and not args.dry_run:
        init_db(db_path)
        ingested = 0
        with connect(db_path) as conn:
            for e in starred:
                if str(e["id"]) not in missing:
                    continue
                try:
                    parsed = parse_entry(e)
                    queue_id = insert_feed(conn, parsed)
                    if queue_id is not None:
                        ingested += 1
                        known_ids[parsed["id"]] = parsed["title"]
                except Exception as exc:
                    print(f"  ingest failed for {e.get('id')}: {exc}")
        if ingested:
            print(f"  ingested {ingested} older starred entries into feeds table")
    to_label: list[str] = []
    to_upgrade: list[str] = []
    respected: list[str] = []
    already_rel: list[str] = []

    for fid in known_ids:
        current = existing.get(fid)
        if current == "relevant":
            already_rel.append(fid)
        elif current == "not_relevant":
            respected.append(fid)
        elif current == "maybe":
            to_upgrade.append(fid)
        else:
            to_label.append(fid)

    print(f"  matched in feeds table: {len(known_ids)}")
    print(f"  not in feeds table:     {len(missing)}  (older than backfill window)")
    print(f"  already relevant:       {len(already_rel)}")
    print(f"  will auto-label:        {len(to_label)}")
    print(f"  will upgrade from maybe: {len(to_upgrade)}")
    print(f"  respected (not_relevant): {len(respected)}")

    if args.dry_run:
        return 0

    for fid in to_label:
        add_label(fid, "relevant", notes="auto: miniflux starred", db_path=db_path)
    for fid in to_upgrade:
        add_label(
            fid,
            "relevant",
            notes="auto: upgraded from maybe (miniflux starred)",
            db_path=db_path,
        )

    print(f"wrote {len(to_label) + len(to_upgrade)} labels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
