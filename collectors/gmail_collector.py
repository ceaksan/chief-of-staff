"""Gmail Collector: processes Gmail search results and writes to cos.db.

Two modes:
  1. Direct: pass email data as list of dicts (from MCP output)
  2. Prompt: used via `claude -p prompts/collect.md`

Usage:
    from collectors.gmail_collector import collect_emails
    collect_emails(config, emails)
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, finish_run, get_db_path, init_db, insert_email, start_run
from cos.log import get_logger, log_with_data

logger = get_logger("gmail_collector")

# Patterns that indicate non-actionable emails
SKIP_PATTERNS = [
    r"^(accepted|declined|tentative):",
    r"invitation:.*updated",
    r"^out of office",
    r"^automatic reply",
    r"^undeliverable:",
    r"^delivery status notification",
]
SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.IGNORECASE)

# Zoho ticket pattern
ZOHO_PATTERN = re.compile(r"#(\w+-\d+)|ticket.*#\d+|zoho.*desk", re.IGNORECASE)


def parse_email(msg: dict) -> dict:
    """Parse a Gmail MCP message into our schema."""
    headers = msg.get("headers", {})
    subject = headers.get("Subject", msg.get("subject", ""))
    sender = headers.get("From", msg.get("from", ""))
    date_str = headers.get("Date", "")

    # Extract clean sender email
    sender_clean = sender
    email_match = re.search(r"<(.+?)>", sender)
    if email_match:
        sender_clean = email_match.group(1)

    # Parse received date
    received_at = msg.get("internalDate")
    if received_at and str(received_at).isdigit():
        received_at = datetime.fromtimestamp(
            int(str(received_at)) / 1000, tz=timezone.utc
        ).isoformat()
    elif date_str:
        received_at = date_str
    else:
        received_at = datetime.now(timezone.utc).isoformat()

    labels = msg.get("labelIds", [])
    snippet = msg.get("snippet", "")

    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", msg["id"]),
        "subject": subject,
        "sender": sender_clean,
        "snippet": snippet,
        "labels": labels,
        "received_at": received_at,
    }


def estimate_priority(email: dict) -> str | None:
    """Estimate priority based on signals."""
    subject = email.get("subject", "").lower()
    labels = email.get("labels", [])

    if "IMPORTANT" in labels or "STARRED" in labels:
        return "P1"

    if ZOHO_PATTERN.search(subject) or ZOHO_PATTERN.search(email.get("snippet", "")):
        return "P2"

    if any(w in subject for w in ["urgent", "asap", "critical", "down", "broken"]):
        return "P1"

    if any(w in subject for w in ["invoice", "payment", "deadline"]):
        return "P2"

    return None


def is_actionable(email: dict) -> bool:
    """Filter out non-actionable emails (calendar responses, auto-replies, etc.)."""
    subject = email.get("subject", "")
    return not SKIP_RE.search(subject)


def collect_emails(config: dict, messages: list[dict]) -> dict:
    """Process email messages and write to cos.db.

    Args:
        config: loaded config.toml
        messages: list of raw Gmail MCP message dicts
    """
    db_path = get_db_path(config)
    init_db(db_path)

    stats = {"processed": 0, "skipped": 0, "filtered": 0, "failed": 0}

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="gmail")

        for msg in messages:
            email = parse_email(msg)

            if not is_actionable(email):
                stats["filtered"] += 1
                log_with_data(
                    logger,
                    logging.DEBUG,
                    f"Filtered non-actionable: {email['subject']}",
                )
                continue

            email["priority"] = estimate_priority(email)

            queue_id = insert_email(conn, email)
            if queue_id is not None:
                stats["processed"] += 1
                log_with_data(
                    logger,
                    logging.INFO,
                    f"Collected: {email['subject'][:60]}",
                    {"sender": email["sender"], "priority": email.get("priority")},
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

    parser = argparse.ArgumentParser(description="Collect Gmail messages")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument("--json", type=Path, help="Path to JSON file with messages")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.json:
        messages = json.loads(args.json.read_text())
    else:
        print("Reading messages from stdin (JSON)...")
        messages = json.load(sys.stdin)

    stats = collect_emails(config, messages)
    log_with_data(logger, logging.INFO, "Gmail collection complete", stats)
    print(
        f"Processed: {stats['processed']}, Filtered: {stats['filtered']}, Skipped: {stats['skipped']}"
    )


if __name__ == "__main__":
    main()
