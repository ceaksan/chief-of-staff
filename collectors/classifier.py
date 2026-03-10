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


def export_pending(config: dict) -> list[dict]:
    """Export pending items that need classification."""
    db_path = get_db_path(config)

    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT queue_id, domain_type, domain_id, priority, status,
                   content_hash, title, context, detail
               FROM v_queue_enriched
               WHERE status = 'pending'
               AND category IS NULL
               ORDER BY priority, collected_at"""
        ).fetchall()

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
