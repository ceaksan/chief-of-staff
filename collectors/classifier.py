"""Classifier helper: exports pending items and imports classifications.

Used by the classifier prompt (claude -p) to read/write cos.db.

Usage:
    # Export pending items as JSON:
    python collectors/classifier.py export

    # Import classifications from JSON:
    python collectors/classifier.py import --json /tmp/cos_classifications.json

    # Direct classify (for testing):
    from collectors.classifier import apply_classifications
    apply_classifications(config, classifications)
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
    start_run,
)
from cos.log import get_logger, log_with_data

logger = get_logger("classifier")


DEFAULT_MAX_ITEMS = 60
DEFAULT_RECENT_DAYS = 3

_FEED_SQL = """SELECT q.queue_id, q.domain_type, q.domain_id, q.priority, q.status,
           q.content_hash, q.title, q.context, q.detail
       FROM v_queue_enriched q
       JOIN taste_scores ts ON ts.feed_id = q.domain_id
       WHERE q.status = 'pending'
         AND q.category IS NULL
         AND q.domain_type = 'feed'
         AND ts.bucket IN ('high_keep', 'auto_keep')
         AND q.collected_at >= datetime('now', ?)
       ORDER BY ts.score DESC, q.collected_at DESC
       LIMIT ?"""


def export_pending(config: dict) -> list[dict]:
    """Export the recent feed items worth classifying, best taste score first.

    Feeds are the only lane left; email, calendar, task and health collection were
    removed. They also arrive faster than the classifier can absorb, so the taste
    layer makes the first cut: auto_drop never reaches the LLM at all.

    The recency window matters as much as the score. Ranking the whole backlog by
    score alone lets tens of thousands of older entries hold every slot forever, so
    newly collected items never reach the classifier no matter how well they score.
    """
    db_path = get_db_path(config)
    cls_config = config.get("classification", {})
    max_items = cls_config.get("max_items", DEFAULT_MAX_ITEMS)
    recent_days = cls_config.get("recent_days", DEFAULT_RECENT_DAYS)

    with connect(db_path) as conn:
        rows = conn.execute(_FEED_SQL, (f"-{recent_days} days", max_items)).fetchall()

    return [dict(row) for row in rows]


def apply_classifications(
    config: dict,
    classifications: list[dict],
    model: str | None = None,
    prompt_version: str | None = None,
) -> dict:
    """Write classifications to cos.db.

    Args:
        config: loaded config.toml
        classifications: list of {queue_id, category, reason}
        model: model identifier
        prompt_version: git hash of classifier prompt
    """
    db_path = get_db_path(config)
    stats = {"classified": 0, "failed": 0}

    with connect(db_path) as conn:
        run_id = start_run(conn, "classifier")

        for cls in classifications:
            queue_id = cls.get("queue_id")
            category = cls.get("category", "").lower()
            reason = cls.get("reason")

            if not queue_id:
                log_with_data(
                    logger,
                    logging.WARNING,
                    "Classification missing queue_id",
                )
                stats["failed"] += 1
                continue

            if category not in ("dispatch", "prep", "yours", "skip"):
                log_with_data(
                    logger,
                    logging.WARNING,
                    f"Invalid category '{category}' for queue_id {queue_id}",
                )
                stats["failed"] += 1
                continue

            try:
                classify_item(conn, queue_id, category, reason, model, prompt_version)
                stats["classified"] += 1
            except Exception as e:
                log_with_data(
                    logger,
                    logging.ERROR,
                    f"Failed to classify queue_id {queue_id}: {e}",
                )
                stats["failed"] += 1

        status = "completed" if stats["failed"] == 0 else "partial"
        finish_run(
            conn,
            run_id,
            status=status,
            items_processed=stats["classified"],
            items_failed=stats["failed"],
        )

    return stats


def apply_force_rules(config: dict, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply force_yours and force_dispatch rules from config.

    Returns (auto_classified, remaining) tuples.
    """
    force_yours = config.get("classification", {}).get("force_yours", [])
    force_dispatch = config.get("classification", {}).get("force_dispatch", [])

    auto = []
    remaining = []

    for item in items:
        title = (item.get("title") or "").lower()
        detail = (item.get("detail") or "").lower()
        context = (item.get("context") or "").lower()
        text = f"{title} {detail} {context}"

        matched = False
        for keyword in force_yours:
            if keyword.lower() in text:
                auto.append(
                    {
                        "queue_id": item["queue_id"],
                        "category": "yours",
                        "reason": f"Force rule: '{keyword}'",
                    }
                )
                matched = True
                break

        if not matched:
            for keyword in force_dispatch:
                if keyword.lower() in text:
                    auto.append(
                        {
                            "queue_id": item["queue_id"],
                            "category": "dispatch",
                            "reason": f"Force rule: '{keyword}'",
                        }
                    )
                    matched = True
                    break

        if not matched:
            remaining.append(item)

    return auto, remaining


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Classifier helper")
    parser.add_argument(
        "action",
        choices=["export", "import"],
        help="export pending or import classifications",
    )
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--json", type=Path, help="Path to classifications JSON (for import)"
    )
    parser.add_argument("--model", type=str, help="Model identifier")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.action == "export":
        items = export_pending(config)

        # Apply force rules
        auto, remaining = apply_force_rules(config, items)

        if auto:
            stats = apply_classifications(config, auto, model="force-rules")
            log_with_data(
                logger,
                logging.INFO,
                f"Force rules applied: {stats['classified']} items",
            )

        output = {"pending_count": len(remaining), "items": remaining}
        print(json.dumps(output, ensure_ascii=False, indent=2))

    elif args.action == "import":
        if not args.json:
            print("Reading classifications from stdin...")
            classifications = json.load(sys.stdin)
        else:
            classifications = json.loads(args.json.read_text())

        stats = apply_classifications(config, classifications, model=args.model)
        print(json.dumps(stats))


if __name__ == "__main__":
    main()
