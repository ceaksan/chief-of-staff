"""Per-feed noise audit.

Shows which feeds contribute mostly to auto_drop (candidates for unsubscribe)
versus which consistently produce high_keep items (worth keeping).

Usage:
    python collectors/taste_feed_audit.py
    python collectors/taste_feed_audit.py --min-items 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.db import connect, get_db_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--min-items", type=int, default=5, help="ignore feeds with fewer scored items"
    )
    args = parser.parse_args()

    with connect(get_db_path()) as conn:
        rows = conn.execute(
            """SELECT f.feed_title,
                      COUNT(*)                                         AS n,
                      SUM(s.bucket = 'high_keep')                      AS high,
                      SUM(s.bucket = 'auto_keep')                      AS keep,
                      SUM(s.bucket = 'borderline')                     AS border,
                      SUM(s.bucket = 'auto_drop')                      AS drop_,
                      AVG(s.score)                                     AS avg_score
               FROM feeds f
               JOIN taste_scores s ON s.feed_id = f.id
               WHERE f.language IN ('tr', 'en')
               GROUP BY f.feed_title
               HAVING n >= ?
               ORDER BY avg_score ASC""",
            (args.min_items,),
        ).fetchall()

    if not rows:
        print("no feeds meet min_items threshold")
        return 0

    worst = rows[:15]
    best = list(reversed(rows))[:15]

    def fmt(r):
        high_pct = (r["high"] / r["n"]) * 100
        drop_pct = (r["drop_"] / r["n"]) * 100
        return (
            f"  {r['avg_score']:+.4f}  n={r['n']:>4}  "
            f"high={r['high']:>3}({high_pct:>4.1f}%)  drop={r['drop_']:>3}({drop_pct:>4.1f}%)  "
            f"| {r['feed_title'][:50]}"
        )

    print(
        f"feeds scored (language in tr/en): {len(rows)}  (min_items={args.min_items})"
    )
    print()
    print("WORST 15 (noise candidates, consider unsubscribing):")
    print("  avg       n      high           drop           feed")
    for r in worst:
        print(fmt(r))
    print()
    print("BEST 15 (high-signal feeds):")
    print("  avg       n      high           drop           feed")
    for r in best:
        print(fmt(r))
    print()

    # summary of pure-noise feeds (0% high, >50% drop)
    noise = [r for r in rows if r["high"] == 0 and (r["drop_"] / r["n"]) > 0.5]
    if noise:
        print(f"pure-noise feeds ({len(noise)}): 0 high_keep AND >50% auto_drop")
        for r in noise[:20]:
            drop_pct = (r["drop_"] / r["n"]) * 100
            print(f"  n={r['n']:>4}  drop={drop_pct:.0f}%  {r['feed_title'][:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
