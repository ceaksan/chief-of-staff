"""Radar Collector: reads Opportunity Radar's pending.json and writes to cos.db.

Radar entries arrive pre-classified (opportunity, trend, hiring).
The collector maps these to CoS classifications:
  opportunity -> dispatch (P2)
  trend       -> prep (P3)
  hiring      -> dispatch (P2)

Usage:
    python collectors/radar_collector.py
    python collectors/radar_collector.py --dry-run
    python collectors/radar_collector.py --config /path/to/config.toml
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import (
    classify_item,
    connect,
    finish_run,
    get_db_path,
    init_db,
    insert_radar_entry,
    start_run,
)
from cos.log import get_logger, log_with_data

logger = get_logger("radar_collector")

# Map radar categories to CoS classification categories
CATEGORY_MAP = {
    "opportunity": "dispatch",
    "trend": "prep",
    "hiring": "dispatch",
}


def load_pending(pending_path: str) -> list[dict]:
    """Load entries from Opportunity Radar's pending.json."""
    path = Path(pending_path).expanduser()
    if not path.exists():
        logger.info("No pending.json found at %s", path)
        return []

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return data.get("entries", [])


def collect_radar(config: dict, dry_run: bool = False) -> dict:
    """Read pending.json and write entries to cos.db.

    Args:
        config: loaded config.toml
        dry_run: if True, print entries without writing to db
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    radar_config = config.get("radar", {})
    pending_path = radar_config.get(
        "pending_json", "../opportunity-radar/output/pending.json"
    )

    entries = load_pending(pending_path)
    log_with_data(
        logger, logging.INFO, f"Loaded {len(entries)} entries from pending.json"
    )

    if not entries:
        return stats

    if dry_run:
        for entry in entries:
            cat = entry.get("category", "?")
            conf = entry.get("confidence", 0)
            cos_cat = CATEGORY_MAP.get(cat, "skip")
            print(f"[{cos_cat}] {cat} ({conf:.0%}) {entry.get('title', 'Untitled')}")
            stats["processed"] += 1
        print(f"\n--- DRY RUN: {stats['processed']} entries found, 0 written ---")
        return stats

    db_path = get_db_path(config)
    init_db(db_path)

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="radar")

        for entry in entries:
            try:
                queue_id = insert_radar_entry(conn, entry)

                if queue_id is not None:
                    # Pre-classify: radar entries arrive already classified
                    cos_category = CATEGORY_MAP.get(entry.get("category", ""), "skip")
                    reason = entry.get("reason", "")
                    classify_item(
                        conn,
                        queue_id,
                        cos_category,
                        reason=f"[radar] {reason}",
                        model=f"radar:ollama",
                    )

                    stats["processed"] += 1
                    log_with_data(
                        logger,
                        logging.INFO,
                        f"Collected: {entry.get('title', '')[:60]}",
                        {
                            "category": entry.get("category"),
                            "cos_category": cos_category,
                            "confidence": entry.get("confidence"),
                        },
                    )
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                stats["failed"] += 1
                log_with_data(
                    logger,
                    logging.ERROR,
                    f"Failed to process radar entry {entry.get('id')}: {exc}",
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

    parser = argparse.ArgumentParser(description="Collect Opportunity Radar entries")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print entries without writing to db"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    stats = collect_radar(config, dry_run=args.dry_run)

    print(
        f"Processed: {stats['processed']}, Skipped: {stats['skipped']}, Failed: {stats['failed']}"
    )


if __name__ == "__main__":
    main()
