"""Renderer: reads cos.db and generates Obsidian Daily Note markdown.

Usage:
    python renderer.py
    python renderer.py --config /path/to/config.toml
    python renderer.py --date 2026-03-08
    python renderer.py --stdout  # print to stdout instead of writing file
"""

import re
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


def fetch_classified(
    conn, target_date: str, utc_range: tuple[str, str] | None = None
) -> dict[str, list[dict]]:
    """Fetch classified items grouped by category.

    Excludes feed items (they have their own Feed Highlights section).
    """
    if utc_range:
        where = "collected_at >= ? AND collected_at < ?"
        params = utc_range
    else:
        where = "date(collected_at) = ?"
        params = (target_date,)
    rows = conn.execute(
        f"""SELECT queue_id, domain_type, domain_id, priority,
               status AS queue_status, category, reason, title, context, detail
           FROM v_queue_enriched
           WHERE category IS NOT NULL
           AND domain_type != 'feed'
           AND status IN ('pending', 'classified', 'approved')
           AND {where}
           ORDER BY priority, collected_at""",
        params,
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


def fetch_feeds(
    conn, target_date: str, utc_range: tuple[str, str] | None = None
) -> list[dict]:
    """Fetch classified feed entries (non-skip) for target date."""
    if utc_range:
        where = "wq.collected_at >= ? AND wq.collected_at < ?"
        params = utc_range
    else:
        where = "date(wq.collected_at) = ?"
        params = (target_date,)
    rows = conn.execute(
        f"""SELECT f.title, f.url, f.feed_title, f.reading_time, c.reason, c.category
           FROM feeds f
           JOIN work_queue wq ON wq.domain_type = 'feed' AND wq.domain_id = f.id
           LEFT JOIN classifications c ON c.queue_id = wq.id
           WHERE {where}
           AND (c.category IS NULL OR c.category != 'skip')
           ORDER BY
               CASE c.category WHEN 'yours' THEN 0 WHEN 'prep' THEN 1 ELSE 2 END,
               wq.priority, wq.collected_at""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_radar(conn, target_date: str) -> list[dict]:
    """Fetch radar entries for target date."""
    rows = conn.execute(
        """SELECT r.title, r.url, r.source, r.radar_category, r.confidence, r.reason
           FROM radar_entries r
           JOIN work_queue wq ON wq.domain_type = 'radar' AND wq.domain_id = r.id
           WHERE date(wq.collected_at) = ?
           ORDER BY r.confidence DESC""",
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_scheduled_content(config: dict | None, target_date: str) -> list[dict]:
    """Read scheduled posts from vault for target date.

    Scans the configured scheduled directory for files starting with the
    target date prefix (YYYY-MM-DD). Extracts title from frontmatter or
    filename.
    """
    if not config:
        return []
    content_cfg = config.get("content", {})
    vault_path = content_cfg.get("vault_path", "")
    scheduled_dir = content_cfg.get("scheduled_dir", "post-scheduler/scheduled")
    if not vault_path:
        return []

    sched_path = Path(vault_path).expanduser() / scheduled_dir
    if not sched_path.exists():
        return []

    results = []
    for f in sorted(sched_path.glob(f"{target_date}-*.md")):
        title = None
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("# "):
                title = line[2:].strip()
                break
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        if not title:
            title = f.stem[11:]  # strip date prefix
            title = title.replace("-", " ").title()
        results.append({"title": title, "filename": f.name})
    return results


def fetch_carried_over(conn, target_date: str) -> list[dict]:
    """Fetch items pending from previous days (carried over).

    Excludes feeds (ephemeral, have their own section) and events (date-bound).
    """
    rows = conn.execute(
        """SELECT queue_id, domain_type, priority, collected_at, title, context, detail
           FROM v_queue_enriched
           WHERE status IN ('pending', 'classified')
           AND domain_type NOT IN ('event', 'feed')
           AND date(collected_at) < ?
           AND collected_at >= datetime(?, '-3 days')
           ORDER BY priority, collected_at""",
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


def _project_tag(item: dict, project_map: dict[str, str] | None = None) -> str:
    """Extract project name for task items.

    Uses the explicit project tag (context) first, then falls back to
    parsing the vault file path (detail) for directory-based project names.
    Skips generic tags like 'task', 'feed', 'email' that are domain types.
    """
    if item.get("domain_type") != "task":
        return ""
    domain_tags = {"task", "feed", "email", "calendar", "dev", "radar"}
    context = item.get("context") or ""
    if context and context.lower() not in domain_tags:
        return f"**{context}**: "
    detail = item.get("detail") or ""
    project = _project_from_path(detail, project_map or {})
    if project:
        return f"**{project}**: "
    return ""


def _project_from_path(file_path: str, project_map: dict[str, str]) -> str:
    """Extract project name from Obsidian vault file path.

    Uses a pre-built lowercase lookup dict from config [projects] section.
    """
    if not file_path:
        return ""
    parts = file_path.split("/")
    lookup = {k.lower(): v for k, v in project_map.items()}
    for part in parts:
        name = lookup.get(part.lower())
        if name:
            return name
    if parts[0] == "DNOMIA":
        return "DNOMIA"
    return ""


def _domain_tag(domain_type: str) -> str:
    tags = {
        "email": "#email",
        "task": "#task",
        "health": "#dev",
        "event": "#calendar",
        "feed": "#feed",
        "radar": "#radar",
    }
    return tags.get(domain_type, "")


def fetch_code_health(config: dict, target_date: str) -> dict | None:
    """Read DIGEST.md from daily-code-review output directory.

    Returns parsed dict with lens, repos, files, findings, critical, and
    per-repo table rows. Returns None if no report exists or code_review
    is not configured.
    """
    reports_dir = config.get("code_review", {}).get("reports_dir", "")
    if not reports_dir:
        return None

    digest_path = Path(reports_dir).expanduser() / target_date / "DIGEST.md"
    if not digest_path.exists():
        return None

    text = digest_path.read_text(encoding="utf-8")

    result: dict = {"repo_details": []}

    for key in ("Lens", "Repos", "Files", "Findings", "Critical"):
        match = re.search(rf"\*\*{key}\*\*:\s*(.+)", text)
        if match:
            val = match.group(1).strip()
            result[key.lower()] = int(val) if val.isdigit() else val

    table_started = False
    for line in text.splitlines():
        if line.startswith("| Repo"):
            table_started = True
            continue
        if table_started and line.startswith("| ---"):
            continue
        if table_started and line.startswith("|"):
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) >= 4:
                result["repo_details"].append(
                    {
                        "name": cols[0],
                        "findings": cols[1],
                        "critical": cols[2],
                        "files": cols[3],
                    }
                )
        elif table_started:
            break

    return result if "lens" in result else None


def _date_range_utc(target_date: str, utc_offset: int = 3) -> tuple[str, str]:
    """Convert local date to UTC range for querying.

    Istanbul is UTC+3, so local midnight = 21:00 UTC previous day.
    Returns (start_utc, end_utc) as ISO strings for WHERE clauses.
    """
    from datetime import timedelta

    local_start = datetime.strptime(target_date, "%Y-%m-%d")
    utc_start = local_start - timedelta(hours=utc_offset)
    utc_end = utc_start + timedelta(days=1)
    return utc_start.strftime("%Y-%m-%d %H:%M:%S"), utc_end.strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def render(conn, target_date: str, config: dict | None = None) -> str:
    """Generate Daily Note markdown from cos.db data."""
    utc_range = _date_range_utc(target_date)
    project_map = config.get("projects", {}) if config else {}
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

    # Scheduled Content
    scheduled = fetch_scheduled_content(config, target_date)
    if scheduled:
        lines.append("## Scheduled Content")
        for s in scheduled:
            lines.append(f"- {s['title']}")
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

    # Code Health (from daily-code-review / dnm-audit)
    if config:
        code_health = fetch_code_health(config, target_date)
        if code_health:
            lines.append("## Code Health")
            lens = code_health.get("lens", "?")
            findings = code_health.get("findings", 0)
            critical = code_health.get("critical", 0)
            files = code_health.get("files", 0)
            lines.append(
                f"- **Lens**: {lens} | Files: {files} | Findings: {findings} | Critical: {critical}"
            )
            for repo in code_health.get("repo_details", []):
                crit = (
                    f" ({repo['critical']} critical)"
                    if int(repo["critical"]) > 0
                    else ""
                )
                lines.append(f"- {repo['name']}: {repo['findings']} findings{crit}")
            lines.append("")

    # Feed Highlights (classified, non-skip only)
    feeds = fetch_feeds(conn, target_date, utc_range)
    if feeds:
        cap = 10
        lines.append("## Feed Highlights")
        for entry in feeds[:cap]:
            title = entry.get("title") or "Untitled"
            url = entry.get("url") or ""
            reason = entry.get("reason") or ""
            parts = f"- [{title}]({url})"
            if reason:
                parts += f" - {reason}"
            lines.append(parts)
        overflow = len(feeds) - cap
        if overflow > 0:
            lines.append(f"- +{overflow} more")
        lines.append("")

    # Radar Opportunities
    radar = fetch_radar(conn, target_date)
    if radar:
        lines.append("## Radar Opportunities")
        for r in radar:
            title = r.get("title") or "Untitled"
            url = r.get("url") or ""
            cat = r.get("radar_category", "")
            confidence = r.get("confidence")
            reason = r.get("reason", "")
            link = f"[{title}]({url})" if url else title
            conf_str = f" ({confidence:.0%})" if confidence else ""
            lines.append(f"- {link} [{cat}]{conf_str}")
            if reason:
                lines.append(f"  - {reason}")
        lines.append("")

    # Classified Tasks
    classified = fetch_classified(conn, target_date, utc_range)
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
                    project = _project_tag(item, project_map)
                    reason = f" - {item['reason']}" if item.get("reason") else ""
                    lines.append(f"- [ ] {pri}{project}{title}{reason} {tag}")
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
            project = _project_tag(item, project_map)
            days = _days_ago(item.get("collected_at"), target_date)
            tag = _domain_tag(item["domain_type"])
            lines.append(f"- [ ] {pri}{project}{title} - pending {days} {tag}")
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
        content = render(conn, target_date, config)

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
