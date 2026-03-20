"""Task Collector: scans Obsidian vault for open tasks and writes to cos.db.

Parses Dataview checkbox format:
    - [ ] Task content #tag @due(2026-03-10)
    - [ ] [P1] Another task #project

Usage:
    python collectors/task_collector.py
    python collectors/task_collector.py --config /path/to/config.toml
"""

import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, finish_run, get_db_path, init_db, insert_task, start_run
from cos.log import get_logger, log_with_data

logger = get_logger("task_collector")

TASK_PATTERN = re.compile(r"^(\s*)-\s+\[ \]\s+(.+)$")
PRIORITY_PATTERN = re.compile(r"\[P([1-4])\]")
DUE_PATTERN = re.compile(r"@due\((\d{4}-\d{2}-\d{2})\)")
PROJECT_TAG_PATTERN = re.compile(r"#(\w[\w-]*)")

EXCLUDED_DIRS = {
    ".obsidian",
    ".trash",
    ".git",
    "node_modules",
    "templates",
    "Templates",
    ".stversions",
}


def parse_task_line(line: str, file_path: str, line_number: int) -> dict | None:
    """Parse a single task line into a task dict."""
    match = TASK_PATTERN.match(line)
    if not match:
        return None

    content = match.group(2).strip()
    if not content:
        return None

    priority_match = PRIORITY_PATTERN.search(content)
    priority = f"P{priority_match.group(1)}" if priority_match else None

    due_match = DUE_PATTERN.search(content)
    due_date = due_match.group(1) if due_match else None

    tags = PROJECT_TAG_PATTERN.findall(content)
    project = tags[0] if tags else None

    # Clean content: remove priority tag, due date, keep hashtags for context
    clean = content
    if priority_match:
        clean = clean.replace(priority_match.group(0), "").strip()
    if due_match:
        clean = clean.replace(due_match.group(0), "").strip()

    task_id = hashlib.sha256(f"{file_path}:{clean}".encode()).hexdigest()[:16]

    return {
        "id": task_id,
        "file_path": file_path,
        "line_number": line_number,
        "content": clean,
        "project": project,
        "due_date": due_date,
        "priority": priority,
    }


def scan_vault(vault_path: Path, config: dict | None = None) -> list[dict]:
    """Scan Obsidian vault for open tasks.

    Skips the daily notes directory to avoid circular collection
    (cos writes tasks there, scanning them back creates duplicates).
    """
    tasks = []
    daily_dir = "daily"
    if config:
        daily_dir = config.get("paths", {}).get("daily_notes_dir", "daily")

    excluded = EXCLUDED_DIRS | {daily_dir}
    excluded_lower = {d.lower() for d in excluded}

    for md_file in vault_path.rglob("*.md"):
        # Skip excluded directories (case-insensitive for daily_dir portability)
        if any(part.lower() in excluded_lower for part in md_file.parts):
            continue

        rel_path = str(md_file.relative_to(vault_path))

        try:
            lines = md_file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, PermissionError):
            continue

        for i, line in enumerate(lines, start=1):
            task = parse_task_line(line, rel_path, i)
            if task:
                tasks.append(task)

    return tasks


def collect(config: dict) -> dict:
    """Scan vault and write tasks to cos.db."""
    vault_path = Path(config["paths"]["obsidian_vault"]).expanduser()
    db_path = get_db_path(config)
    init_db(db_path)

    stats = {"processed": 0, "skipped": 0, "failed": 0}

    if not vault_path.exists():
        log_with_data(logger, logging.ERROR, f"Vault not found: {vault_path}")
        return stats

    tasks = scan_vault(vault_path, config)
    log_with_data(
        logger,
        logging.INFO,
        f"Found {len(tasks)} open tasks",
        {"vault": str(vault_path)},
    )

    current_ids = {t["id"] for t in tasks}

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="task")

        for task in tasks:
            queue_id = insert_task(conn, task)
            if queue_id is not None:
                stats["processed"] += 1
            else:
                stats["skipped"] += 1

        # Mark tasks removed from vault as done
        stale = conn.execute(
            """SELECT wq.id, wq.domain_id FROM work_queue wq
               WHERE wq.domain_type = 'task'
               AND wq.status IN ('pending', 'classified')"""
        ).fetchall()
        stale_count = 0
        for row in stale:
            if row["domain_id"] not in current_ids:
                conn.execute(
                    "UPDATE work_queue SET status = 'done', processed_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                stale_count += 1
        if stale_count:
            log_with_data(
                logger,
                logging.INFO,
                f"Marked {stale_count} removed tasks as done",
            )

        finish_run(conn, run_id, status="completed", items_processed=stats["processed"])

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Collect tasks from Obsidian vault")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    args = parser.parse_args()

    config = load_config(args.config)
    stats = collect(config)
    log_with_data(logger, logging.INFO, "Task collection complete", stats)


if __name__ == "__main__":
    main()
