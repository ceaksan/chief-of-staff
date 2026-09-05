"""Taste filter: learns personal interest from user labels via embeddings.

Pipeline:
    1. User labels a sample of feeds (relevant / not_relevant / maybe).
    2. Each feed gets embedded (title + snippet) once, cached in taste_embeddings.
    3. Centroids for relevant and not_relevant are computed from labeled items.
    4. Every feed is scored: cos(item, relevant) - cos(item, not_relevant).
    5. Scores bucket into auto_keep / borderline / auto_drop using config thresholds.

The label set grows over time. Rebuild centroids + rescore on demand.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import httpx

from cos.db import connect, get_db_path
from cos.log import get_logger

logger = get_logger("taste")


def _taste_config() -> dict:
    """[taste] section from config.toml; empty dict if missing."""
    try:
        from cos.config import load_config

        return load_config().get("taste", {})
    except Exception:
        return {}


_cfg = _taste_config()
DEFAULT_MODEL = _cfg.get("model", "zylonai/multilingual-e5-large:latest#v2")
DEFAULT_OLLAMA_URL = _cfg.get("ollama_url", "http://localhost:11434")
DEFAULT_HIGH_KEEP_THRESHOLD = _cfg.get("high_keep_threshold", 0.010)
DEFAULT_KEEP_THRESHOLD = _cfg.get("keep_threshold", 0.002)
DEFAULT_DROP_THRESHOLD = _cfg.get("drop_threshold", -0.001)


# ---------------------------------------------------------------------------
# Embedding backend: Ollama HTTP API. Host comes from config.toml [taste].ollama_url.
# ---------------------------------------------------------------------------


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        return v
    return [x / n for x in v]


def _ollama_model(model_name: str) -> str:
    """Strip our internal version suffix (#vN) before talking to Ollama."""
    return model_name.split("#", 1)[0]


def embed_texts(
    texts: Sequence[str],
    model_name: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
) -> list[list[float]]:
    """Embed a batch via Ollama /api/embed. Returns L2-normalized float vectors."""
    if not texts:
        return []
    resp = httpx.post(
        f"{base_url.rstrip('/')}/api/embed",
        json={"model": _ollama_model(model_name), "input": list(texts)},
        timeout=120,
    )
    resp.raise_for_status()
    raw = resp.json().get("embeddings") or []
    return [_normalize(v) for v in raw]


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------


def add_label(
    feed_id: str, label: str, notes: str | None = None, db_path: Path | None = None
) -> None:
    """Insert or replace a label for a feed item."""
    assert label in {"relevant", "not_relevant", "maybe"}, f"bad label: {label}"
    with connect(db_path or get_db_path()) as conn:
        conn.execute(
            """INSERT INTO taste_labels (feed_id, label, notes)
               VALUES (?, ?, ?)
               ON CONFLICT(feed_id) DO UPDATE SET
                   label = excluded.label,
                   notes = excluded.notes,
                   labeled_at = datetime('now')""",
            (feed_id, label, notes),
        )


def export_unlabeled(limit: int = 100, db_path: Path | None = None) -> list[dict]:
    """Return candidate feeds that need a label. Used by labeling UI."""
    with connect(db_path or get_db_path()) as conn:
        rows = conn.execute(
            """SELECT f.id, f.title, f.feed_title, f.url, f.content, f.published_at
               FROM feeds f
               LEFT JOIN taste_labels l ON l.feed_id = f.id
               WHERE l.feed_id IS NULL
               ORDER BY f.published_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def label_counts(db_path: Path | None = None) -> dict[str, int]:
    with connect(db_path or get_db_path()) as conn:
        rows = conn.execute(
            "SELECT label, COUNT(*) as n FROM taste_labels GROUP BY label"
        ).fetchall()
    return {r["label"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Embedding persistence
# ---------------------------------------------------------------------------


def _prepare_input(row: sqlite3.Row | dict, prefix: str = "query: ") -> str:
    """Build the text we embed. E5 requires a 'query:' prefix for both sides
    of a symmetric similarity comparison, per the model card. Other models
    (bge-m3, nomic) take the raw text."""
    title = (row["title"] or "").strip()
    content = (row["content"] or "").strip()
    if len(content) > 600:
        content = content[:600]
    body = f"{title}\n\n{content}".strip()
    return f"{prefix}{body}"


def _model_prefix(model_name: str) -> str:
    return "query: " if "e5" in model_name.lower() else ""


def embed_missing(
    model_name: str = DEFAULT_MODEL,
    batch_size: int = 32,
    base_url: str = DEFAULT_OLLAMA_URL,
    db_path: Path | None = None,
) -> int:
    """Compute and persist embeddings for feeds that don't yet have one for this model."""
    db_path = db_path or get_db_path()
    with connect(db_path) as conn:
        pending = conn.execute(
            """SELECT f.id, f.title, f.content
               FROM feeds f
               LEFT JOIN taste_embeddings e
                   ON e.feed_id = f.id AND e.model = ?
               WHERE e.feed_id IS NULL""",
            (model_name,),
        ).fetchall()

    if not pending:
        logger.info("no feeds need embedding")
        return 0

    total = 0
    prefix = _model_prefix(model_name)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        texts = [_prepare_input(r, prefix) for r in batch]
        vectors = embed_texts(texts, model_name, base_url=base_url)
        dim = len(vectors[0]) if vectors else 0
        with connect(db_path) as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO taste_embeddings (feed_id, model, dim, vector)
                   VALUES (?, ?, ?, ?)""",
                [
                    (r["id"], model_name, dim, json.dumps(v))
                    for r, v in zip(batch, vectors)
                ],
            )
        total += len(batch)
        logger.info("embedded %d/%d", total, len(pending))
    return total


# ---------------------------------------------------------------------------
# Taste vector + scoring
# ---------------------------------------------------------------------------


@dataclass
class TasteCentroids:
    relevant: list[float] | None
    not_relevant: list[float] | None
    dim: int
    model: str
    n_relevant: int
    n_not_relevant: int


def _load_vectors(
    conn: sqlite3.Connection, label: str, model: str
) -> list[list[float]]:
    rows = conn.execute(
        """SELECT e.vector FROM taste_embeddings e
           JOIN taste_labels l ON l.feed_id = e.feed_id
           JOIN feeds f ON f.id = e.feed_id
           WHERE l.label = ? AND e.model = ?
             AND f.language IN ('tr', 'en')""",
        (label, model),
    ).fetchall()
    return [json.loads(r["vector"]) for r in rows]


def _mean(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            out[i] += v[i]
    n = len(vectors)
    return [x / n for x in out]


def _cosine(a: list[float], b: list[float]) -> float:
    # Vectors are normalized at embed time, but recompute to be safe.
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def build_centroids(
    model: str = DEFAULT_MODEL, db_path: Path | None = None
) -> TasteCentroids:
    with connect(db_path or get_db_path()) as conn:
        rel = _load_vectors(conn, "relevant", model)
        nonrel = _load_vectors(conn, "not_relevant", model)
    rel_c = _mean(rel)
    non_c = _mean(nonrel)
    dim = len(rel_c) if rel_c else len(non_c) if non_c else 0
    return TasteCentroids(
        relevant=rel_c,
        not_relevant=non_c,
        dim=dim,
        model=model,
        n_relevant=len(rel),
        n_not_relevant=len(nonrel),
    )


def score_all(
    centroids: TasteCentroids,
    keep_threshold: float = DEFAULT_KEEP_THRESHOLD,
    drop_threshold: float = DEFAULT_DROP_THRESHOLD,
    high_keep_threshold: float = DEFAULT_HIGH_KEEP_THRESHOLD,
    db_path: Path | None = None,
) -> int:
    """Score every embedded feed against the centroids. Returns count scored.

    Buckets, from strong positive to strong negative:
        high_keep  >= high_keep_threshold  (surfaces in daily brief)
        auto_keep  >= keep_threshold       (weekly digest)
        borderline (in between)            (labeling queue)
        auto_drop  <= drop_threshold       (silent)
    """
    if centroids.relevant is None:
        raise RuntimeError("no relevant labels yet; label some feeds first")

    db_path = db_path or get_db_path()
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT e.feed_id, e.vector FROM taste_embeddings e
               JOIN feeds f ON f.id = e.feed_id
               WHERE e.model = ? AND f.language IN ('tr', 'en')""",
            (centroids.model,),
        ).fetchall()

    updates: list[tuple[str, str, float, str]] = []
    for r in rows:
        v = json.loads(r["vector"])
        rel_sim = _cosine(v, centroids.relevant)
        non_sim = _cosine(v, centroids.not_relevant) if centroids.not_relevant else 0.0
        score = rel_sim - non_sim
        if score >= high_keep_threshold:
            bucket = "high_keep"
        elif score >= keep_threshold:
            bucket = "auto_keep"
        elif score <= drop_threshold:
            bucket = "auto_drop"
        else:
            bucket = "borderline"
        updates.append((r["feed_id"], centroids.model, score, bucket))

    with connect(db_path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO taste_scores (feed_id, model, score, bucket)
               VALUES (?, ?, ?, ?)""",
            updates,
        )
    logger.info("scored %d feeds", len(updates))
    return len(updates)


def bucket_counts(db_path: Path | None = None) -> dict[str, int]:
    with connect(db_path or get_db_path()) as conn:
        rows = conn.execute(
            "SELECT bucket, COUNT(*) as n FROM taste_scores GROUP BY bucket"
        ).fetchall()
    return {r["bucket"]: r["n"] for r in rows}


def borderline_queue(limit: int = 30, db_path: Path | None = None) -> list[dict]:
    """Return borderline items that still need a human label."""
    with connect(db_path or get_db_path()) as conn:
        rows = conn.execute(
            """SELECT f.id, f.title, f.feed_title, f.url, s.score
               FROM taste_scores s
               JOIN feeds f ON f.id = s.feed_id
               LEFT JOIN taste_labels l ON l.feed_id = f.id
               WHERE s.bucket = 'borderline' AND l.feed_id IS NULL
               ORDER BY ABS(s.score) ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Migration helper: apply taste schema to existing DB
# ---------------------------------------------------------------------------


def ensure_schema(db_path: Path | None = None) -> None:
    """Apply migrations/002_add_taste.sql if taste tables are missing."""
    db_path = db_path or get_db_path()
    migration = Path(__file__).parent.parent / "migrations" / "002_add_taste.sql"
    with connect(db_path) as conn:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='taste_labels'"
        ).fetchone()
        if not has:
            conn.executescript(migration.read_text())
            logger.info("applied migration: %s", migration.name)

        # 004: high_keep tier. Detect by CHECK constraint via inserting a probe.
        try:
            conn.execute(
                "INSERT INTO taste_scores (feed_id, model, score, bucket) "
                "VALUES ('__migration_probe__', 'probe', 0, 'high_keep')"
            )
            conn.execute(
                "DELETE FROM taste_scores WHERE feed_id = '__migration_probe__'"
            )
        except Exception:
            m4 = migration.parent / "004_taste_high_keep.sql"
            conn.executescript(m4.read_text())
            logger.info("applied migration: 004_taste_high_keep.sql")
