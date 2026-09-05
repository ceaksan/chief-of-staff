"""Shortlist: reads cos.db and prints what the classifier flagged as worth reading.

This is the read end of the opportunity lane. Collection and classification write
to cos.db; nothing surfaces them, so without this the pipeline's output is only
reachable through SQL.

Usage:
    python shortlist.py
    python shortlist.py --days 7
    python shortlist.py --category yours
    python shortlist.py --all          # include prep and dispatch
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cos.config import load_config
from cos.db import connect, get_db_path

DEFAULT_DAYS = 3
DEFAULT_CATEGORIES = ("yours", "prep")

# Ordered by how much they demand of him, so the top of the output is the part
# he has to decide about himself.
CATEGORY_ORDER = {"yours": 0, "prep": 1, "dispatch": 2, "skip": 3}

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def fetch(conn, days: int, categories: tuple[str, ...], limit: int) -> list[dict]:
    placeholders = ",".join("?" for _ in categories)
    rows = conn.execute(
        f"""SELECT c.category, c.reason, c.created_at,
                   q.title, q.context, q.domain_id,
                   f.url, f.feed_title
            FROM classifications c
            JOIN v_queue_enriched q ON q.queue_id = c.queue_id
            LEFT JOIN feeds f ON f.id = q.domain_id
            WHERE c.category IN ({placeholders})
              AND c.created_at >= datetime('now', ?)
            GROUP BY q.domain_id
            ORDER BY c.created_at DESC
            LIMIT ?""",
        (*categories, f"-{days} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def render(items: list[dict], days: int, use_color: bool) -> str:
    b = BOLD if use_color else ""
    d = DIM if use_color else ""
    r = RESET if use_color else ""

    if not items:
        return f"Nothing flagged in the last {days} days. Run `cos run` to collect."

    items.sort(key=lambda i: (CATEGORY_ORDER.get(i["category"], 9), i["title"] or ""))

    lines = [f"{b}Shortlist{r} {d}(last {days} days, {len(items)} items){r}", ""]
    current = None
    for item in items:
        if item["category"] != current:
            current = item["category"]
            lines.append(f"{b}{current.upper()}{r}")
        source = item["feed_title"] or item["context"] or "?"
        lines.append(f"  {item['title']}")
        lines.append(f"    {d}{source}{r}")
        if item["reason"]:
            lines.append(f"    {d}{item['reason']}{r}")
        if item["url"]:
            lines.append(f"    {item['url']}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Print the classified shortlist")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="How far back to look"
    )
    parser.add_argument("--category", help="Show only this category")
    parser.add_argument(
        "--all", action="store_true", help="Include prep and dispatch as well"
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum items")
    parser.add_argument("--no-color", action="store_true", help="Plain output")
    args = parser.parse_args()

    if args.category:
        categories = (args.category,)
    elif args.all:
        categories = ("yours", "prep", "dispatch")
    else:
        categories = DEFAULT_CATEGORIES

    config = load_config(args.config) if args.config else load_config()
    with connect(get_db_path(config)) as conn:
        items = fetch(conn, args.days, categories, args.limit)

    use_color = sys.stdout.isatty() and not args.no_color
    print(render(items, args.days, use_color))


if __name__ == "__main__":
    main()
