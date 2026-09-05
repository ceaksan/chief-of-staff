"""Per-domain taste centroids from the user's own content.

The single-centroid taste filter blurs multi-modal interests: analytics/AI
(ceaksan), macro photography / creative (arsiterans) and ecology / slow living
(ecodiurnal) sit far apart in embedding space, so their mean matches nothing.

This module builds one centroid per interest domain from the user's own
published content (configured under [taste.domains] in config.toml), then
scores every feed as:

    score  = max(cos(item, domain_i)) - cos(item, not_relevant)
    domain = argmax domain_i           (stored in taste_scores.domain)

The labeled 'relevant' centroid from cos.taste joins the pool as the
'starred' domain, so explicit labels keep contributing signal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cos.db import connect, get_db_path
from cos.log import get_logger
from cos.taste import (
    DEFAULT_DROP_THRESHOLD,
    DEFAULT_HIGH_KEEP_THRESHOLD,
    DEFAULT_KEEP_THRESHOLD,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    _cosine,
    _mean,
    _model_prefix,
    _taste_config,
    build_centroids,
    embed_texts,
)

logger = get_logger("taste_domains")

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r'^title:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
_SKIP_NAMES = {"README.md", "DEPLOY.md", "DESIGN.md"}
_SKIP_PARTS = {"node_modules", "tasks", "_drafts", ".obsidian"}


def _read_doc(path: Path) -> dict | None:
    """Extract {title, content} from a markdown file, or None if empty."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    title = ""
    m = _FRONTMATTER_RE.match(text)
    if m:
        tm = _TITLE_RE.search(m.group(1))
        if tm:
            title = tm.group(1).strip()
        text = text[m.end() :]
    if not title:
        h = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = h.group(1).strip() if h else path.stem.replace("-", " ")
    body = re.sub(r"\s+", " ", text).strip()
    if len(body) < 80:  # skip stubs / empty notes
        return None
    return {"title": title, "content": body}


def load_domain_docs(paths: list[str]) -> list[dict]:
    docs = []
    for root in paths:
        rootp = Path(root).expanduser()
        if not rootp.exists():
            logger.warning("domain path missing: %s", rootp)
            continue
        files = (
            [rootp]
            if rootp.is_file()
            else sorted(list(rootp.rglob("*.md")) + list(rootp.rglob("*.mdx")))
        )
        for f in files:
            if f.name in _SKIP_NAMES or _SKIP_PARTS & set(f.parts):
                continue
            doc = _read_doc(f)
            if doc:
                docs.append(doc)
    return docs


def configured_domains() -> dict[str, list[str]]:
    return _taste_config().get("domains", {})


def build_domain_centroids(
    domains: dict[str, list[str]] | None = None,
    model_name: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    batch_size: int = 64,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Embed each domain's corpus and persist one centroid per domain."""
    from cos.taste import _prepare_input  # late import: shares truncation logic

    domains = domains or configured_domains()
    if not domains:
        raise RuntimeError("no [taste.domains] configured")
    ensure_domain_schema(db_path)
    prefix = _model_prefix(model_name)
    counts: dict[str, int] = {}
    for domain, paths in domains.items():
        docs = load_domain_docs(paths)
        if not docs:
            logger.warning("domain %s: no docs found, skipping", domain)
            continue
        vectors: list[list[float]] = []
        for start in range(0, len(docs), batch_size):
            batch = docs[start : start + batch_size]
            texts = [_prepare_input(d, prefix) for d in batch]
            vectors.extend(embed_texts(texts, model_name, base_url=base_url))
        centroid = _mean(vectors)
        with connect(db_path or get_db_path()) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO taste_domain_centroids
                   (domain, model, dim, vector, n_docs, built_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (domain, model_name, len(centroid), json.dumps(centroid), len(docs)),
            )
        counts[domain] = len(docs)
        logger.info("domain %s: %d docs -> centroid", domain, len(docs))
    return counts


def load_domain_centroids(
    model_name: str = DEFAULT_MODEL, db_path: Path | None = None
) -> dict[str, list[float]]:
    with connect(db_path or get_db_path()) as conn:
        rows = conn.execute(
            "SELECT domain, vector FROM taste_domain_centroids WHERE model = ?",
            (model_name,),
        ).fetchall()
    return {r["domain"]: json.loads(r["vector"]) for r in rows}


def score_all_domains(
    model_name: str = DEFAULT_MODEL,
    high_keep_threshold: float = DEFAULT_HIGH_KEEP_THRESHOLD,
    keep_threshold: float = DEFAULT_KEEP_THRESHOLD,
    drop_threshold: float = DEFAULT_DROP_THRESHOLD,
    db_path: Path | None = None,
) -> int:
    """Score every embedded feed against domain centroids + labeled centroids."""
    db_path = db_path or get_db_path()
    ensure_domain_schema(db_path)
    domains = load_domain_centroids(model_name, db_path)
    labeled = build_centroids(model_name, db_path)
    if labeled.relevant is not None:
        domains.setdefault("starred", labeled.relevant)
    if not domains:
        raise RuntimeError("no domain centroids; run build_domain_centroids first")

    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT e.feed_id, e.vector FROM taste_embeddings e
               JOIN feeds f ON f.id = e.feed_id
               WHERE e.model = ? AND f.language IN ('tr', 'en')""",
            (model_name,),
        ).fetchall()

    updates = []
    for r in rows:
        v = json.loads(r["vector"])
        sims = {d: _cosine(v, c) for d, c in domains.items()}
        best_domain, best_sim = max(sims.items(), key=lambda kv: kv[1])
        non_sim = _cosine(v, labeled.not_relevant) if labeled.not_relevant else 0.0
        score = best_sim - non_sim
        if score >= high_keep_threshold:
            bucket = "high_keep"
        elif score >= keep_threshold:
            bucket = "auto_keep"
        elif score <= drop_threshold:
            bucket = "auto_drop"
        else:
            bucket = "borderline"
        updates.append((r["feed_id"], model_name, score, bucket, best_domain))

    with connect(db_path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO taste_scores
               (feed_id, model, score, bucket, domain)
               VALUES (?, ?, ?, ?, ?)""",
            updates,
        )
    logger.info("domain-scored %d feeds across %d domains", len(updates), len(domains))
    return len(updates)


def ensure_domain_schema(db_path: Path | None = None) -> None:
    """Apply migrations/005_taste_domains.sql if not yet applied."""
    db_path = db_path or get_db_path()
    migration = Path(__file__).parent.parent / "migrations" / "005_taste_domains.sql"
    with connect(db_path) as conn:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='taste_domain_centroids'"
        ).fetchone()
        has_column = any(
            r[1] == "domain" for r in conn.execute("PRAGMA table_info(taste_scores)")
        )
        if has_table and has_column:
            return
        if has_table and not has_column:
            conn.execute("ALTER TABLE taste_scores ADD COLUMN domain TEXT")
        else:
            conn.executescript(migration.read_text())
        logger.info("applied migration: %s", migration.name)
