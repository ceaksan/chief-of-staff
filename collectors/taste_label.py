"""Interactive terminal labeling tool for the taste filter.

Shows one unlabeled feed item at a time. Single-key input:
    r = relevant
    n = not_relevant
    m = maybe
    s = skip (leave unlabeled, move on)
    u = undo last label
    o = open url in default browser
    q = quit

Resume-friendly: already-labeled items are filtered out automatically.

Usage:
    python collectors/taste_label.py
    python collectors/taste_label.py --limit 100
    python collectors/taste_label.py --since 2026-01-01
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import termios
import textwrap
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.db import connect, get_db_path
from cos.taste import add_label, ensure_schema, label_counts


VALID_KEYS = {"r", "n", "m", "s", "u", "o", "q"}
LABEL_MAP = {"r": "relevant", "n": "not_relevant", "m": "maybe"}


def _read_key() -> str:
    """Read one keypress without requiring Enter."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch.lower()


def _clear() -> None:
    os.system("clear" if os.name != "nt" else "cls")


def _strip_html(text: str, limit: int = 500) -> str:
    import re

    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("..." if len(t) > limit else "")


def _load_queue(
    limit: int,
    since: str | None,
    db_path: Path,
    order: str,
    feed: str | None = None,
) -> list[dict]:
    """order: 'recent' = newest first; 'uncertainty' = most borderline first
    (active learning: label where the model is least confident).
    feed: substring match on feed_title to focus on one source."""
    params: list = []
    where = "WHERE l.feed_id IS NULL"
    if since:
        where += " AND f.published_at >= ?"
        params.append(since)
    if feed:
        where += " AND f.feed_title LIKE ?"
        params.append(f"%{feed}%")

    if order == "uncertainty":
        sql = f"""
            SELECT f.id, f.title, f.feed_title, f.url, f.content, f.published_at,
                   COALESCE(s.score, 0.0) AS score
            FROM feeds f
            LEFT JOIN taste_labels l ON l.feed_id = f.id
            LEFT JOIN taste_scores s ON s.feed_id = f.id
            {where}
            ORDER BY ABS(COALESCE(s.score, 999)) ASC
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT f.id, f.title, f.feed_title, f.url, f.content, f.published_at,
                   NULL AS score
            FROM feeds f
            LEFT JOIN taste_labels l ON l.feed_id = f.id
            {where}
            ORDER BY f.published_at DESC
            LIMIT ?
        """
    params.append(limit)
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _remove_label(feed_id: str, db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM taste_labels WHERE feed_id = ?", (feed_id,))


def _render(item: dict, idx: int, total: int, counts: dict[str, int]) -> None:
    _clear()
    counts_line = "  ".join(
        f"{k}: {counts.get(k, 0)}" for k in ("relevant", "not_relevant", "maybe")
    )
    score = item.get("score")
    score_str = f"  model_score={score:+.4f}" if score is not None else ""
    print(f"[{idx + 1}/{total}]  labeled so far  {counts_line}{score_str}")
    print("-" * 80)
    print(f"FEED: {item['feed_title']}")
    print(f"DATE: {item['published_at']}")
    print(f"URL:  {item['url']}")
    print()
    title = item["title"] or "(no title)"
    print(
        "\n".join(
            textwrap.wrap(title, width=78, initial_indent="  ", subsequent_indent="  ")
        )
    )
    print()
    snippet = _strip_html(item["content"] or "", 600)
    if snippet:
        print(
            "\n".join(
                textwrap.wrap(
                    snippet, width=78, initial_indent="    ", subsequent_indent="    "
                )
            )
        )
    print()
    print("-" * 80)
    print("  [r] relevant   [n] not_relevant   [m] maybe   [s] skip")
    print("  [u] undo last  [o] open url       [q] quit")


def _open_url(url: str) -> None:
    try:
        subprocess.run(["open", url], check=False)
    except FileNotFoundError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive taste labeling")
    parser.add_argument("--limit", type=int, default=200, help="max items to load")
    parser.add_argument(
        "--since", type=str, default=None, help="ISO date filter for published_at"
    )
    parser.add_argument(
        "--order",
        choices=["recent", "uncertainty"],
        default="uncertainty",
        help="recent=newest first; uncertainty=most borderline first (active learning, default)",
    )
    parser.add_argument(
        "--feed",
        type=str,
        default=None,
        help="substring match on feed_title (e.g. 'AAAS' to only label Science items)",
    )
    args = parser.parse_args()

    db_path = get_db_path()
    ensure_schema(db_path)

    queue = _load_queue(args.limit, args.since, db_path, args.order, args.feed)
    if not queue:
        print("no unlabeled feeds in the queue. try --limit N or --since YYYY-MM-DD")
        return 0

    print(f"loaded {len(queue)} unlabeled items. press any key to begin...")
    _read_key()

    last_labeled_id: str | None = None
    idx = 0
    while idx < len(queue):
        item = queue[idx]
        counts = label_counts(db_path)
        _render(item, idx, len(queue), counts)
        key = _read_key()
        if key not in VALID_KEYS:
            continue
        if key == "q":
            print()
            print("bye")
            return 0
        if key == "o":
            _open_url(item["url"])
            continue
        if key == "u":
            if last_labeled_id is None:
                continue
            _remove_label(last_labeled_id, db_path)
            last_labeled_id = None
            idx = max(0, idx - 1)
            continue
        if key == "s":
            idx += 1
            continue
        label = LABEL_MAP[key]
        add_label(item["id"], label, db_path=db_path)
        last_labeled_id = item["id"]
        idx += 1

    _clear()
    final = label_counts(db_path)
    print("queue finished.")
    print(f"labels: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
