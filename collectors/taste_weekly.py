"""Weekly taste check-in.

Presents three short queues, then rebuilds centroids and rescores:

    1. Top uncertainty:   N most-borderline unlabeled items  (primary signal)
    2. Auto-keep sanity:  M random auto_keep items from the last week
                          (reject wrong ones, catch model drift)
    3. Auto-drop rescue:  K random auto_drop items from the last week
                          (rescue mistakenly dropped items)

Runs non-destructively: existing labels are never overwritten unless the user
picks a new label on an already-labeled item.

Usage:
    python collectors/taste_weekly.py
    python collectors/taste_weekly.py --uncertainty 20 --sanity 10 --rescue 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.taste_label import (
    LABEL_MAP,
    VALID_KEYS,
    _clear,
    _open_url,
    _read_key,
    _render,
)
from cos.db import connect, get_db_path
from cos.taste import (
    add_label,
    bucket_counts,
    build_centroids,
    ensure_schema,
    label_counts,
    score_all,
)


def _fetch_uncertainty(limit: int, db_path: Path) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT f.id, f.title, f.feed_title, f.url, f.content, f.published_at,
                      s.score
               FROM feeds f
               JOIN taste_scores s ON s.feed_id = f.id
               LEFT JOIN taste_labels l ON l.feed_id = f.id
               WHERE l.feed_id IS NULL AND f.language IN ('tr', 'en')
               ORDER BY ABS(s.score) ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_bucket_sample(
    bucket: str, limit: int, days: int, db_path: Path
) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT f.id, f.title, f.feed_title, f.url, f.content, f.published_at,
                      s.score
               FROM feeds f
               JOIN taste_scores s ON s.feed_id = f.id
               LEFT JOIN taste_labels l ON l.feed_id = f.id
               WHERE s.bucket = ?
                 AND (l.feed_id IS NULL OR l.label = 'maybe')
                 AND f.language IN ('tr', 'en')
                 AND s.scored_at >= datetime('now', ?)
               ORDER BY RANDOM()
               LIMIT ?""",
            (bucket, f"-{days} days", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _run_queue(title: str, queue: list[dict], db_path: Path) -> int:
    if not queue:
        return 0
    labeled = 0
    idx = 0
    while idx < len(queue):
        item = queue[idx]
        counts = label_counts(db_path)
        _render(item, idx, len(queue), counts)
        print()
        print(f"  >>> section: {title}")
        key = _read_key()
        if key not in VALID_KEYS:
            continue
        if key == "q":
            return labeled
        if key == "o":
            _open_url(item["url"])
            continue
        if key == "s":
            idx += 1
            continue
        if key == "u":
            idx = max(0, idx - 1)
            continue
        add_label(item["id"], LABEL_MAP[key], db_path=db_path)
        labeled += 1
        idx += 1
    return labeled


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly taste check-in")
    parser.add_argument("--uncertainty", type=int, default=15)
    parser.add_argument("--sanity", type=int, default=8)
    parser.add_argument("--rescue", type=int, default=5)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--skip-rebuild", action="store_true")
    args = parser.parse_args()

    db_path = get_db_path()
    ensure_schema(db_path)

    total_sections = []
    total_sections.append(
        (
            "uncertainty (borderline unlabeled)",
            _fetch_uncertainty(args.uncertainty, db_path),
        )
    )
    total_sections.append(
        (
            "auto_keep sanity check",
            _fetch_bucket_sample("auto_keep", args.sanity, args.days, db_path),
        )
    )
    total_sections.append(
        (
            "auto_drop rescue check",
            _fetch_bucket_sample("auto_drop", args.rescue, args.days, db_path),
        )
    )

    total_planned = sum(len(q) for _, q in total_sections)
    if total_planned == 0:
        print("nothing to review this week")
        return 0

    print(
        f"weekly taste check-in: {total_planned} items across {len(total_sections)} sections"
    )
    print("press any key to begin, q to exit early")
    _read_key()

    labeled = 0
    for title, queue in total_sections:
        labeled += _run_queue(title, queue, db_path)

    _clear()
    print(f"labeled this session: {labeled}")
    print(f"totals: {label_counts(db_path)}")

    if args.skip_rebuild or labeled == 0:
        return 0

    print()
    print("rebuilding centroids and rescoring...")
    with connect(db_path) as conn:
        conn.execute("DELETE FROM taste_scores")
    cent = build_centroids()
    scored = score_all(cent)
    print(f"scored {scored} feeds -> {bucket_counts()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
