"""Full-corpus taste filter report.

Run after embed_missing + score_all on the 10k backfill to see real-scale
behavior: label coverage, bucket distribution, precision/recall on the
labeled subset, language split, and representative samples from each bucket.
"""

from __future__ import annotations

import statistics as stats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.db import connect, get_db_path


def main() -> int:
    db_path = get_db_path()
    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(DISTINCT feed_id) FROM taste_embeddings"
        ).fetchone()[0]
        scored = conn.execute("SELECT COUNT(*) FROM taste_scores").fetchone()[0]

        labels = {
            r["label"]: r["n"]
            for r in conn.execute(
                "SELECT label, COUNT(*) n FROM taste_labels GROUP BY label"
            ).fetchall()
        }
        buckets = {
            r["bucket"]: r["n"]
            for r in conn.execute(
                "SELECT bucket, COUNT(*) n FROM taste_scores GROUP BY bucket"
            ).fetchall()
        }
        languages = [
            (r["language"], r["n"])
            for r in conn.execute(
                "SELECT COALESCE(language, 'null') language, COUNT(*) n FROM feeds GROUP BY language ORDER BY n DESC"
            ).fetchall()
        ]

        scores = [
            r["score"]
            for r in conn.execute("SELECT score FROM taste_scores").fetchall()
        ]

        conf = {
            (r["label"], r["bucket"]): r["n"]
            for r in conn.execute(
                """SELECT l.label, s.bucket, COUNT(*) n
                   FROM taste_labels l
                   JOIN taste_scores s ON s.feed_id = l.feed_id
                   JOIN feeds f ON f.id = l.feed_id
                   WHERE f.language IN ('tr','en')
                   GROUP BY l.label, s.bucket"""
            ).fetchall()
        }

        top_keep = conn.execute(
            """SELECT f.title, f.feed_title, s.score
               FROM taste_scores s JOIN feeds f ON f.id=s.feed_id
               LEFT JOIN taste_labels l ON l.feed_id=s.feed_id
               WHERE s.bucket='auto_keep' AND l.feed_id IS NULL
               ORDER BY s.score DESC LIMIT 10"""
        ).fetchall()

        top_drop = conn.execute(
            """SELECT f.title, f.feed_title, s.score
               FROM taste_scores s JOIN feeds f ON f.id=s.feed_id
               LEFT JOIN taste_labels l ON l.feed_id=s.feed_id
               WHERE s.bucket='auto_drop' AND l.feed_id IS NULL
               ORDER BY s.score ASC LIMIT 10"""
        ).fetchall()

        borderline = conn.execute(
            """SELECT f.title, f.feed_title, s.score
               FROM taste_scores s JOIN feeds f ON f.id=s.feed_id
               LEFT JOIN taste_labels l ON l.feed_id=s.feed_id
               WHERE s.bucket='borderline' AND l.feed_id IS NULL
               ORDER BY ABS(s.score) ASC LIMIT 10"""
        ).fetchall()

    def pr(label, bucket):
        tp = conf.get((label, bucket), 0)
        fp = sum(
            v
            for (l, b), v in conf.items()
            if b == bucket and l != label and l != "maybe"
        )
        fn = sum(v for (l, b), v in conf.items() if l == label and b != bucket)
        p = tp / (tp + fp) if tp + fp else 0
        r = tp / (tp + fn) if tp + fn else 0
        f = 2 * p * r / (p + r) if p + r else 0
        return p, r, f

    print("=" * 70)
    print("TASTE FILTER - FULL CORPUS REPORT")
    print("=" * 70)
    print()
    print(f"feeds total:    {total}")
    print(f"embedded:       {embedded}  ({embedded / total * 100:.1f}%)")
    print(f"scored:         {scored}  (tr+en only)")
    print()
    print("language split:")
    for lang, n in languages:
        print(f"  {lang:<8} {n:>6}  ({n / total * 100:.1f}%)")
    print()
    print("label totals:")
    for k in ("relevant", "not_relevant", "maybe"):
        print(f"  {k:<14} {labels.get(k, 0)}")
    print()
    print("bucket distribution:")
    for k in ("auto_keep", "borderline", "auto_drop"):
        print(
            f"  {k:<12} {buckets.get(k, 0):>5}  ({buckets.get(k, 0) / max(scored, 1) * 100:.1f}%)"
        )
    print()

    if scores:
        s_sorted = sorted(scores)
        print(f"score distribution: n={len(scores)}")
        print(f"  min    = {s_sorted[0]:+.4f}")
        print(f"  p10    = {s_sorted[len(s_sorted) // 10]:+.4f}")
        print(f"  p50    = {s_sorted[len(s_sorted) // 2]:+.4f}")
        print(f"  p90    = {s_sorted[len(s_sorted) * 9 // 10]:+.4f}")
        print(f"  max    = {s_sorted[-1]:+.4f}")
        print(f"  mean   = {stats.mean(scores):+.4f}")
        print(f"  stdev  = {stats.stdev(scores):+.4f}")
        print()

    p_k, r_k, f_k = pr("relevant", "auto_keep")
    p_d, r_d, f_d = pr("not_relevant", "auto_drop")
    print("validation metrics (on labeled tr+en subset):")
    print(
        f"  auto_keep  precision={p_k * 100:5.1f}%  recall={r_k * 100:5.1f}%  F1={f_k * 100:5.1f}%"
    )
    print(
        f"  auto_drop  precision={p_d * 100:5.1f}%  recall={r_d * 100:5.1f}%  F1={f_d * 100:5.1f}%"
    )
    print()

    print("top 10 auto_keep (unlabeled, highest confidence):")
    for r in top_keep:
        t = (r["title"] or "")[:72]
        print(f"  {r['score']:+.4f}  [{r['feed_title'][:20]:<20}] {t}")
    print()
    print("top 10 auto_drop (unlabeled, strongest reject):")
    for r in top_drop:
        t = (r["title"] or "")[:72]
        print(f"  {r['score']:+.4f}  [{r['feed_title'][:20]:<20}] {t}")
    print()
    print("top 10 borderline (most uncertain, best candidates to label next):")
    for r in borderline:
        t = (r["title"] or "")[:72]
        print(f"  {r['score']:+.4f}  [{r['feed_title'][:20]:<20}] {t}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
