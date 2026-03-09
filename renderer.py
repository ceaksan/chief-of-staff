"""Renderer: reads cos.db and generates Obsidian Daily Note markdown.

Usage:
    python renderer.py
    python renderer.py --config /path/to/config.toml
    python renderer.py --date 2026-03-08
    python renderer.py --stdout  # print to stdout instead of writing file
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cos.config import load_config
from cos.db import connect, get_db_path, init_db
from cos.log import get_logger, log_with_data
import logging

logger = get_logger("renderer")


def fetch_events(conn, target_date: str) -> list[dict]:
    """Fetch calendar events for target date, sorted by start time."""
    rows = conn.execute(
        """SELECT e.*, wq.priority
           FROM events e
           JOIN work_queue wq ON wq.domain_type = 'event' AND wq.domain_id = e.id
           WHERE date(e.start_time) = ?
           ORDER BY e.start_time""",
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_health(conn, target_date: str) -> list[dict]:
    """Fetch health checks for target date."""
    rows = conn.execute(
        """SELECT h.*
           FROM health_checks h
           WHERE date(h.checked_at) = ?
           ORDER BY
               CASE h.status
                   WHEN 'down' THEN 0
                   WHEN 'error' THEN 1
                   WHEN 'warning' THEN 2
                   WHEN 'ok' THEN 3
               END""",
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_classified(conn, target_date: str) -> dict[str, list[dict]]:
    """Fetch classified items grouped by category."""
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
               END AS context
           FROM work_queue wq
           JOIN classifications c ON c.id = (
               SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
           )
           LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
           LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
           LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
           LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
           WHERE date(wq.collected_at) = ?
           ORDER BY wq.priority, wq.collected_at""",
        (target_date,),
    ).fetchall()

    grouped: dict[str, list[dict]] = {
        "dispatch": [],
        "prep": [],
        "yours": [],
        "skip": [],
    }
    for row in rows:
        r = dict(row)
        cat = r.get("category", "skip")
        if cat in grouped:
            grouped[cat].append(r)
    return grouped


def fetch_feeds(conn, target_date: str) -> list[dict]:
    """Fetch feed entries for target date, ordered by priority."""
    rows = conn.execute(
        """SELECT f.title, f.url, f.feed_title, f.reading_time
           FROM feeds f
           JOIN work_queue wq ON wq.domain_type = 'feed' AND wq.domain_id = f.id
           WHERE date(f.collected_at) = ?
           ORDER BY wq.priority, f.collected_at""",
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_carried_over(conn, target_date: str) -> list[dict]:
    """Fetch items pending from previous days (carried over)."""
    rows = conn.execute(
        """SELECT
               wq.id AS queue_id,
               wq.domain_type,
               wq.priority,
               wq.collected_at,
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
           LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
           LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
           LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
           LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
           WHERE wq.status IN ('pending', 'classified')
           AND wq.domain_type != 'event'
           AND date(wq.collected_at) < ?
           AND wq.collected_at >= datetime(?, '-3 days')
           ORDER BY wq.priority, wq.collected_at""",
        (target_date, target_date),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_actions(conn, target_date: str) -> list[dict]:
    """Fetch agent actions for target date."""
    rows = conn.execute(
        """SELECT a.agent, a.action_type, a.output_summary, a.status, a.external_ref
           FROM actions a
           WHERE date(a.created_at) = ?
           ORDER BY a.created_at""",
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_run_warnings(conn, target_date: str) -> list[dict]:
    """Fetch failed or partial runs for target date."""
    rows = conn.execute(
        """SELECT layer, source, status, error, items_failed
           FROM runs
           WHERE date(started_at) = ?
           AND status IN ('failed', 'partial')
           ORDER BY started_at""",
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_time(iso_str: str | None) -> str:
    """Extract HH:MM from ISO datetime string."""
    if not iso_str:
        return "?"
    try:
        if "T" in iso_str:
            time_part = iso_str.split("T")[1]
            return time_part[:5]
        # All-day event: date only (YYYY-MM-DD), no time component
        if len(iso_str) == 10:
            return "all-day"
    except (IndexError, ValueError):
        pass
    return iso_str[:5] if len(iso_str) >= 5 else iso_str


def _priority_tag(priority: str | None) -> str:
    if priority:
        return f"[{priority}] "
    return ""


def _domain_tag(domain_type: str) -> str:
    tags = {"email": "#email", "task": "#task", "health": "#dev", "event": "#calendar"}
    return tags.get(domain_type, "")


def render(conn, target_date: str) -> str:
    """Generate Daily Note markdown from cos.db data."""
    lines: list[str] = []
    lines.append(f"# {target_date}")
    lines.append("")

    # Warnings
    warnings = fetch_run_warnings(conn, target_date)
    if warnings:
        lines.append("> [!warning] Collection Issues")
        for w in warnings:
            source = f" ({w['source']})" if w["source"] else ""
            error = f": {w['error']}" if w["error"] else ""
            lines.append(f"> - {w['layer']}{source} {w['status']}{error}")
        lines.append("")

    # Calendar
    events = fetch_events(conn, target_date)
    lines.append("## Calendar")
    if events:
        for ev in events:
            start = _format_time(ev["start_time"])
            end = _format_time(ev["end_time"])
            summary = ev.get("summary", "Untitled")
            parts = [f"- {start}-{end} {summary}"]
            if ev.get("location"):
                parts[0] += f" ({ev['location']})"
            if ev.get("is_calendly"):
                parts[0] += " - Calendly"
            if ev.get("prep_needed"):
                parts[0] += " **prep needed**"
            lines.append(parts[0])
    else:
        lines.append("- No events")
    lines.append("")

    # Project Status
    health = fetch_health(conn, target_date)
    lines.append("## Project Status")
    if health:
        ok_projects = [h["project"] for h in health if h["status"] == "ok"]
        problem_projects = [h for h in health if h["status"] != "ok"]

        if ok_projects:
            lines.append(f"- OK: {', '.join(ok_projects)}")
        for h in problem_projects:
            detail = ""
            if h.get("errors_24h"):
                detail += f"{h['errors_24h']} errors"
            if h.get("last_error"):
                if detail:
                    detail += " - "
                detail += h["last_error"]
            if detail:
                lines.append(f"- {h['project']}: {h['status']} ({detail})")
            else:
                lines.append(f"- {h['project']}: {h['status']}")
    else:
        lines.append("- No health data collected")
    lines.append("")

    # Feed Highlights
    feeds = fetch_feeds(conn, target_date)
    if feeds:
        cap = 15
        lines.append("## Feed Highlights")
        for entry in feeds[:cap]:
            title = entry.get("title") or "Untitled"
            url = entry.get("url") or ""
            feed_name = entry.get("feed_title") or ""
            reading_time = entry.get("reading_time")
            parts = f"- [{title}]({url})"
            if feed_name:
                parts += f" ({feed_name})"
            if reading_time:
                parts += f" ~{reading_time}min"
            lines.append(parts)
        overflow = len(feeds) - cap
        if overflow > 0:
            lines.append(f"- +{overflow} more")
        lines.append("")

    # Classified Tasks
    classified = fetch_classified(conn, target_date)
    has_classified = any(items for items in classified.values())

    lines.append("## Classified Tasks")
    if has_classified:
        section_names = {
            "dispatch": "DISPATCH (AI handles)",
            "prep": "PREP (80% ready, you finish)",
            "yours": "YOURS (your brain needed)",
            "skip": "SKIP (not today)",
        }
        for cat, label in section_names.items():
            items = classified[cat]
            if items:
                lines.append(f"### {label}")
                for item in items:
                    title = item.get("title") or "Untitled"
                    tag = _domain_tag(item["domain_type"])
                    pri = _priority_tag(item.get("priority"))
                    reason = f" - {item['reason']}" if item.get("reason") else ""
                    lines.append(f"- [ ] {pri}{title}{reason} {tag}")
                lines.append("")
    else:
        lines.append("- Not yet classified")
        lines.append("")

    # Carried Over
    carried = fetch_carried_over(conn, target_date)
    if carried:
        lines.append("## Carried Over")
        for item in carried:
            title = item.get("title") or "Untitled"
            pri = _priority_tag(item.get("priority"))
            days = _days_ago(item.get("collected_at"), target_date)
            tag = _domain_tag(item["domain_type"])
            lines.append(f"- [ ] {pri}{title} - pending {days} {tag}")
        lines.append("")

    # Agent Actions
    actions_list = fetch_actions(conn, target_date)
    if actions_list:
        lines.append("## Agent Actions")
        for a in actions_list:
            status_icon = "x" if a["status"] == "completed" else " "
            summary = a.get("output_summary") or a["action_type"]
            lines.append(f"- [{status_icon}] **{a['agent']}**: {summary}")
        lines.append("")

    return "\n".join(lines)


def _days_ago(collected_at: str | None, target_date: str) -> str:
    if not collected_at:
        return "? days"
    try:
        collected_date = collected_at[:10]  # extract YYYY-MM-DD from any format
        delta = (
            datetime.strptime(target_date, "%Y-%m-%d")
            - datetime.strptime(collected_date, "%Y-%m-%d")
        ).days
        if delta == 1:
            return "1 day"
        return f"{delta} days"
    except (ValueError, TypeError):
        return "? days"


def write_daily_note(config: dict, content: str, target_date: str) -> Path:
    """Write rendered content to Obsidian vault as daily note."""
    vault = Path(config["paths"]["obsidian_vault"]).expanduser()
    daily_dir = vault / config["paths"].get("daily_notes_dir", "Daily")
    daily_dir.mkdir(parents=True, exist_ok=True)

    note_path = daily_dir / f"{target_date}.md"

    if note_path.exists():
        existing = note_path.read_text(encoding="utf-8")
        marker_start = "<!-- cos:start -->"
        marker_end = "<!-- cos:end -->"
        if marker_start in existing:
            before = existing.split(marker_start)[0]
            after = existing.split(marker_end)[1] if marker_end in existing else "\n"
            content = f"{before}{marker_start}\n{content}\n{marker_end}{after}"
        else:
            content = f"{existing}\n\n{marker_start}\n{content}\n{marker_end}\n"
    else:
        content = f"<!-- cos:start -->\n{content}\n<!-- cos:end -->\n"

    note_path.write_text(content, encoding="utf-8")
    return note_path


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Render cos.db to Obsidian Daily Note")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--date", type=str, help="Target date (YYYY-MM-DD), defaults to today"
    )
    parser.add_argument(
        "--stdout", action="store_true", help="Print to stdout instead of writing file"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = get_db_path(config)
    init_db(db_path)

    target_date = args.date or datetime.now().strftime("%Y-%m-%d")

    with connect(db_path) as conn:
        content = render(conn, target_date)

    if args.stdout:
        print(content)
    else:
        note_path = write_daily_note(config, content, target_date)
        log_with_data(
            logger,
            logging.INFO,
            f"Daily note written to {note_path}",
            {"date": target_date},
        )
        print(f"Written: {note_path}")


if __name__ == "__main__":
    main()
