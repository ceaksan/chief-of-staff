"""Database access layer for Chief of Staff."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).parent.parent / "cos.db"


def get_db_path(config: dict | None = None) -> Path:
    if config and "paths" in config:
        cos_dir = Path(config["paths"].get("cos_dir", str(DEFAULT_DB_PATH.parent)))
        cos_dir = cos_dir.expanduser()
        return cos_dir / "cos.db"
    return DEFAULT_DB_PATH


def init_db(db_path: Path | None = None) -> None:
    """Create database and apply schema."""
    db_path = db_path or DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text()
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(schema)


@contextmanager
def connect(db_path: Path | None = None):
    """Context manager for DB connections with WAL mode and foreign keys."""
    db_path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def content_hash(data: dict) -> str:
    """SHA256 hash of JSON payload for dedup/cache."""
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# -- Domain inserts ----------------------------------------------------------


def insert_email(conn: sqlite3.Connection, email: dict) -> int | None:
    """Insert email and create work queue entry. Returns queue_id or None if duplicate."""
    try:
        conn.execute(
            """INSERT INTO emails (id, thread_id, subject, sender, snippet, labels, received_at, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                email["id"],
                email.get("thread_id"),
                email.get("subject"),
                email.get("sender"),
                email.get("snippet"),
                json.dumps(email.get("labels", [])),
                email["received_at"],
                json.dumps(email),
            ),
        )
    except sqlite3.IntegrityError:
        return None

    return _enqueue(conn, "email", email["id"], email.get("priority"), email)


def insert_event(conn: sqlite3.Connection, event: dict) -> int | None:
    """Insert calendar event and create work queue entry."""
    try:
        conn.execute(
            """INSERT INTO events (id, calendar_id, summary, start_time, end_time, location, is_calendly, prep_needed, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["id"],
                event["calendar_id"],
                event.get("summary"),
                event["start_time"],
                event["end_time"],
                event.get("location"),
                int(event.get("is_calendly", False)),
                int(event.get("prep_needed", False)),
                json.dumps(event),
            ),
        )
    except sqlite3.IntegrityError:
        return None

    return _enqueue(conn, "event", event["id"], event.get("priority"), event)


def insert_task(conn: sqlite3.Connection, task: dict) -> int | None:
    """Insert obsidian task and create work queue entry."""
    try:
        conn.execute(
            """INSERT INTO tasks (id, file_path, line_number, content, project, due_date, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                task["id"],
                task["file_path"],
                task.get("line_number"),
                task["content"],
                task.get("project"),
                task.get("due_date"),
                json.dumps(task),
            ),
        )
    except sqlite3.IntegrityError:
        return None

    return _enqueue(conn, "task", task["id"], task.get("priority"), task)


def insert_health_check(conn: sqlite3.Connection, check: dict) -> int | None:
    """Insert health check and create work queue entry."""
    try:
        conn.execute(
            """INSERT INTO health_checks (id, project, status, uptime, errors_24h, last_error, last_deploy, checked_at, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                check["id"],
                check["project"],
                check["status"],
                check.get("uptime"),
                check.get("errors_24h", 0),
                check.get("last_error"),
                check.get("last_deploy"),
                check["checked_at"],
                json.dumps(check),
            ),
        )
    except sqlite3.IntegrityError:
        return None

    return _enqueue(conn, "health", check["id"], check.get("priority"), check)


def insert_feed(conn: sqlite3.Connection, feed: dict) -> int | None:
    """Insert feed entry and create work queue entry. Returns queue_id or None if duplicate."""
    try:
        conn.execute(
            """INSERT INTO feeds (id, feed_id, feed_title, title, url, author, content, published_at, reading_time, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed["id"],
                feed["feed_id"],
                feed["feed_title"],
                feed["title"],
                feed["url"],
                feed.get("author"),
                feed.get("content"),
                feed["published_at"],
                feed.get("reading_time", 0),
                json.dumps(feed.get("tags", [])),
            ),
        )
    except sqlite3.IntegrityError:
        return None

    return _enqueue(conn, "feed", feed["id"], feed.get("priority"), feed)


def _enqueue(
    conn: sqlite3.Connection,
    domain_type: str,
    domain_id: str,
    priority: str | None,
    data: dict,
) -> int:
    """Add item to work queue with content hash."""
    cursor = conn.execute(
        """INSERT INTO work_queue (domain_type, domain_id, priority, content_hash)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(domain_type, domain_id) DO UPDATE SET
               content_hash = excluded.content_hash,
               priority = COALESCE(excluded.priority, work_queue.priority)""",
        (domain_type, domain_id, priority, content_hash(data)),
    )
    if cursor.lastrowid:
        return cursor.lastrowid
    row = conn.execute(
        "SELECT id FROM work_queue WHERE domain_type = ? AND domain_id = ?",
        (domain_type, domain_id),
    ).fetchone()
    return row["id"]


# -- Classification ----------------------------------------------------------


def classify_item(
    conn: sqlite3.Connection,
    queue_id: int,
    category: str,
    reason: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> None:
    """Add classification and update queue status."""
    conn.execute(
        """INSERT INTO classifications (queue_id, category, reason, model, prompt_version)
           VALUES (?, ?, ?, ?, ?)""",
        (queue_id, category, reason, model, prompt_version),
    )
    conn.execute(
        "UPDATE work_queue SET status = 'classified' WHERE id = ?",
        (queue_id,),
    )


def is_cached(
    conn: sqlite3.Connection, domain_type: str, domain_id: str, data: dict
) -> bool:
    """Check if item has been classified and content hasn't changed."""
    row = conn.execute(
        """SELECT wq.content_hash FROM work_queue wq
           JOIN classifications c ON c.queue_id = wq.id
           WHERE wq.domain_type = ? AND wq.domain_id = ?
           ORDER BY c.created_at DESC LIMIT 1""",
        (domain_type, domain_id),
    ).fetchone()
    if not row:
        return False
    return row["content_hash"] == content_hash(data)


# -- Actions ------------------------------------------------------------------


def record_action(
    conn: sqlite3.Connection,
    queue_id: int,
    agent: str,
    action_type: str,
    external_ref: str | None = None,
    output_summary: str | None = None,
    status: str = "completed",
) -> int:
    """Record an agent action linked to a queue item."""
    cursor = conn.execute(
        """INSERT INTO actions (queue_id, agent, action_type, external_ref, output_summary, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (queue_id, agent, action_type, external_ref, output_summary, status),
    )
    return cursor.lastrowid


# -- Runs ---------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, layer: str, source: str | None = None) -> int:
    """Start a pipeline run, return run_id."""
    cursor = conn.execute(
        "INSERT INTO runs (layer, source) VALUES (?, ?)",
        (layer, source),
    )
    return cursor.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str = "completed",
    items_processed: int = 0,
    items_failed: int = 0,
    error: str | None = None,
    budget_used: float | None = None,
) -> None:
    """Complete a pipeline run."""
    conn.execute(
        """UPDATE runs SET
            finished_at = datetime('now'),
            status = ?,
            items_processed = ?,
            items_failed = ?,
            error = ?,
            budget_used = ?
           WHERE id = ?""",
        (status, items_processed, items_failed, error, budget_used, run_id),
    )


# -- Queries ------------------------------------------------------------------


def get_pending_items(
    conn: sqlite3.Connection, domain_type: str | None = None
) -> list[dict]:
    """Get pending work queue items, optionally filtered by type."""
    query = "SELECT * FROM work_queue WHERE status = 'pending'"
    params: list[Any] = []
    if domain_type:
        query += " AND domain_type = ?"
        params.append(domain_type)
    query += " ORDER BY priority, collected_at"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_today_briefing(conn: sqlite3.Connection) -> list[dict]:
    """Get today's briefing data from the view."""
    return [
        dict(row) for row in conn.execute("SELECT * FROM v_today_briefing").fetchall()
    ]


def get_active_queue(conn: sqlite3.Connection) -> list[dict]:
    """Get active (non-done, non-skipped) items from last 3 days."""
    return [
        dict(row) for row in conn.execute("SELECT * FROM v_active_queue").fetchall()
    ]
