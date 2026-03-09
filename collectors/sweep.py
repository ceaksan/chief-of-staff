"""Sweep helper: exports classified items and records agent actions.

Used by the sweep prompt (claude -p) to read/write cos.db.

Usage:
    # Export items ready for sweep:
    python collectors/sweep.py export

    # Record an agent action:
    python collectors/sweep.py record --json /path/to/actions.json

    # Mark items as done:
    python collectors/sweep.py complete --ids 1,2,3
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import (
    connect,
    finish_run,
    get_db_path,
    record_action,
    start_run,
)
from cos.log import get_logger, log_with_data

logger = get_logger("sweep")


def export_sweep_items(config: dict) -> dict:
    """Export classified items grouped by category for sweep processing."""
    db_path = get_db_path(config)

    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT
                   wq.id AS queue_id,
                   wq.domain_type,
                   wq.domain_id,
                   wq.priority,
                   wq.status AS queue_status,
                   c.category,
                   c.reason,
                   CASE wq.domain_type
                       WHEN 'email' THEN e.subject
                       WHEN 'event' THEN ev.summary
                       WHEN 'task' THEN t.content
                       WHEN 'health' THEN h.project || ': ' || h.status
                   END AS title,
                   CASE wq.domain_type
                       WHEN 'email' THEN e.sender
                       WHEN 'event' THEN ev.calendar_id
                       WHEN 'task' THEN t.project
                       WHEN 'health' THEN h.project
                   END AS context,
                   CASE wq.domain_type
                       WHEN 'email' THEN e.snippet
                       WHEN 'event' THEN ev.location
                       WHEN 'task' THEN t.file_path
                       WHEN 'health' THEN h.last_error
                   END AS detail,
                   CASE wq.domain_type
                       WHEN 'email' THEN e.thread_id
                       WHEN 'event' THEN ev.start_time
                       WHEN 'task' THEN t.due_date
                       WHEN 'health' THEN h.checked_at
                   END AS extra
               FROM work_queue wq
               JOIN classifications c ON c.id = (
                   SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
               )
               LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
               LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
               LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
               LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
               WHERE wq.status = 'classified'
               AND c.category IN ('dispatch', 'prep')
               ORDER BY
                   CASE c.category WHEN 'dispatch' THEN 0 WHEN 'prep' THEN 1 END,
                   wq.priority, wq.collected_at"""
        ).fetchall()

    grouped = {"dispatch": [], "prep": []}
    for row in rows:
        r = dict(row)
        cat = r.get("category", "prep")
        if cat in grouped:
            grouped[cat].append(r)

    return grouped


def export_yours_items(config: dict) -> list[dict]:
    """Export 'yours' items for context summary generation."""
    db_path = get_db_path(config)

    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT
                   wq.id AS queue_id,
                   wq.domain_type,
                   wq.domain_id,
                   wq.priority,
                   c.category,
                   c.reason,
                   CASE wq.domain_type
                       WHEN 'email' THEN e.subject
                       WHEN 'event' THEN ev.summary
                       WHEN 'task' THEN t.content
                       WHEN 'health' THEN h.project || ': ' || h.status
                   END AS title,
                   CASE wq.domain_type
                       WHEN 'email' THEN e.sender
                       WHEN 'event' THEN ev.calendar_id
                       WHEN 'task' THEN t.project
                       WHEN 'health' THEN h.project
                   END AS context
               FROM work_queue wq
               JOIN classifications c ON c.id = (
                   SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
               )
               LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
               LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
               LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
               LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
               WHERE wq.status = 'classified'
               AND c.category = 'yours'
               ORDER BY wq.priority, wq.collected_at"""
        ).fetchall()

    return [dict(row) for row in rows]


def apply_actions(config: dict, actions: list[dict]) -> dict:
    """Record agent actions and update work queue status.

    Args:
        config: loaded config.toml
        actions: list of {queue_id, agent, action_type, external_ref, output_summary, status}
    """
    db_path = get_db_path(config)
    stats = {"recorded": 0, "failed": 0}

    with connect(db_path) as conn:
        run_id = start_run(conn, "sweep")

        for action in actions:
            queue_id = action.get("queue_id")
            if not queue_id:
                log_with_data(logger, logging.WARNING, "Action missing queue_id")
                stats["failed"] += 1
                continue

            agent = action.get("agent", "unknown")
            action_type = action.get("action_type", "unknown")

            try:
                record_action(
                    conn,
                    queue_id,
                    agent=agent,
                    action_type=action_type,
                    external_ref=action.get("external_ref"),
                    output_summary=action.get("output_summary"),
                    status=action.get("status", "completed"),
                )
                # Update queue status to dispatched
                conn.execute(
                    "UPDATE work_queue SET status = 'dispatched', processed_at = datetime('now') WHERE id = ?",
                    (queue_id,),
                )
                stats["recorded"] += 1
            except Exception as e:
                log_with_data(
                    logger,
                    logging.ERROR,
                    f"Failed to record action for queue_id {queue_id}: {e}",
                )
                stats["failed"] += 1

        status = "completed" if stats["failed"] == 0 else "partial"
        finish_run(
            conn,
            run_id,
            status=status,
            items_processed=stats["recorded"],
            items_failed=stats["failed"],
        )

    return stats


def mark_done(config: dict, queue_ids: list[int]) -> dict:
    """Mark work queue items as done after successful sweep."""
    db_path = get_db_path(config)
    stats = {"done": 0, "failed": 0}

    with connect(db_path) as conn:
        for qid in queue_ids:
            try:
                cursor = conn.execute(
                    "UPDATE work_queue SET status = 'done', processed_at = datetime('now') WHERE id = ?",
                    (qid,),
                )
                if cursor.rowcount > 0:
                    stats["done"] += 1
            except Exception as e:
                log_with_data(
                    logger, logging.ERROR, f"Failed to mark queue_id {qid} done: {e}"
                )
                stats["failed"] += 1

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sweep helper")
    parser.add_argument(
        "action",
        choices=["export", "record", "complete"],
        help="export classified items, record actions, or mark done",
    )
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument("--json", type=Path, help="Path to JSON file (for record)")
    parser.add_argument(
        "--ids", type=str, help="Comma-separated queue IDs (for complete)"
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.action == "export":
        sweep_items = export_sweep_items(config)
        yours_items = export_yours_items(config)
        output = {
            "dispatch": sweep_items["dispatch"],
            "prep": sweep_items["prep"],
            "yours": yours_items,
            "counts": {
                "dispatch": len(sweep_items["dispatch"]),
                "prep": len(sweep_items["prep"]),
                "yours": len(yours_items),
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))

    elif args.action == "record":
        if args.json:
            actions = json.loads(args.json.read_text())
        else:
            print("Reading actions from stdin (JSON)...")
            actions = json.load(sys.stdin)

        stats = apply_actions(config, actions)
        print(json.dumps(stats))

    elif args.action == "complete":
        if not args.ids:
            print("Error: --ids required for complete action")
            sys.exit(1)
        queue_ids = [int(x.strip()) for x in args.ids.split(",")]
        stats = mark_done(config, queue_ids)
        print(json.dumps(stats))


if __name__ == "__main__":
    main()
