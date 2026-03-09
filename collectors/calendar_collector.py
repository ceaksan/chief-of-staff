"""Calendar Collector: fetches events from Google Calendar and writes to cos.db.

Two modes:
  1. Direct: pass events as JSON (from MCP output or test data)
  2. Prompt: used via `claude -p prompts/collect_calendar.md`

Usage:
    # Direct mode (pipe MCP JSON):
    python collectors/calendar_collector.py --json events.json

    # From another Python script:
    from collectors.calendar_collector import collect_events
    collect_events(config, events_by_calendar)
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, finish_run, get_db_path, init_db, insert_event, start_run
from cos.log import get_logger, log_with_data

logger = get_logger("calendar_collector")

CALENDLY_KEYWORDS = ["calendly", "superpeer"]


def parse_event(event: dict, calendar_id: str) -> dict:
    """Parse a Google Calendar MCP event into our schema."""
    start = event.get("start", {})
    end = event.get("end", {})

    start_time = start.get("dateTime") or start.get("date", "")
    end_time = end.get("dateTime") or end.get("date", "")

    summary = event.get("summary", "")
    description = (event.get("description") or "").lower()
    location = event.get("location", "")

    is_calendly = any(
        kw in description or kw in location.lower() or kw in summary.lower()
        for kw in CALENDLY_KEYWORDS
    )

    # Events with external attendees or Calendly likely need prep
    num_attendees = event.get("numAttendees", 0)
    prep_needed = is_calendly or num_attendees > 1

    return {
        "id": event["id"],
        "calendar_id": calendar_id,
        "summary": summary,
        "start_time": start_time,
        "end_time": end_time,
        "location": location,
        "is_calendly": is_calendly,
        "prep_needed": prep_needed,
    }


def deduplicate_events(all_events: list[dict]) -> list[dict]:
    """Remove duplicate events across calendars (same summary + same start time)."""
    seen = {}
    for ev in all_events:
        key = f"{ev['summary']}_{ev['start_time']}"
        if key not in seen or not ev["summary"]:
            seen[ev["id"] if not ev["summary"] else key] = ev
    return list(seen.values())


def collect_events(config: dict, events_by_calendar: dict[str, list[dict]]) -> dict:
    """Write calendar events to cos.db.

    Args:
        config: loaded config.toml
        events_by_calendar: {calendar_id: [raw MCP event dicts]}
    """
    db_path = get_db_path(config)
    init_db(db_path)

    stats = {"processed": 0, "skipped": 0, "failed": 0}

    all_events = []
    for calendar_id, events in events_by_calendar.items():
        for event in events:
            if event.get("status") == "cancelled":
                continue
            parsed = parse_event(event, calendar_id)
            all_events.append(parsed)

    deduped = deduplicate_events(all_events)
    log_with_data(
        logger,
        logging.INFO,
        f"Parsed {len(all_events)} events, {len(deduped)} after dedup",
        {"calendars": list(events_by_calendar.keys())},
    )

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="calendar")

        for event in deduped:
            queue_id = insert_event(conn, event)
            if queue_id is not None:
                stats["processed"] += 1
                log_with_data(
                    logger,
                    logging.INFO,
                    f"Collected: {event['summary']}",
                    {"start": event["start_time"], "calendar": event["calendar_id"]},
                )
            else:
                stats["skipped"] += 1

        finish_run(
            conn,
            run_id,
            status="completed",
            items_processed=stats["processed"],
        )

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Collect calendar events")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--json", type=Path, help="Path to JSON file with events by calendar"
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.json:
        events_by_calendar = json.loads(args.json.read_text())
    else:
        print("Reading events from stdin (JSON)...")
        events_by_calendar = json.load(sys.stdin)

    stats = collect_events(config, events_by_calendar)
    log_with_data(logger, logging.INFO, "Calendar collection complete", stats)
    print(f"Processed: {stats['processed']}, Skipped: {stats['skipped']}")


if __name__ == "__main__":
    main()
