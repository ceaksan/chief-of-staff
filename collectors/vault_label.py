"""Harvest implicit 'relevant' signal from the Obsidian vault.

If a feed URL appears anywhere in the vault, the user has (at some point) cared
enough to save it. We treat that as an implicit 'relevant' label.

Strategy:
    1. Walk the vault and extract all HTTP(S) URLs from markdown files.
    2. Normalize them (strip tracking params, www, trailing slash, fragment).
    3. Build the same normalized form for every feed URL.
    4. For each feed that matches AND isn't already labeled 'relevant',
       insert an auto-label. Existing 'not_relevant' is respected; the
       user's explicit 'no' beats the implicit 'yes'.

Usage:
    python collectors/vault_label.py
    python collectors/vault_label.py --vault /path/to/vault --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, get_db_path
from cos.taste import add_label, ensure_schema

URL_RE = re.compile(r"https?://[^\s\]\)\>\"']+", re.IGNORECASE)
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "ref_src",
    "ref_url",
    "mc_cid",
    "mc_eid",
    "fbclid",
    "gclid",
}


def normalize_url(url: str) -> str:
    """Canonicalize a URL so that small formatting differences still match."""
    url = url.strip().strip(".,;)}]'\"")
    try:
        p = urlparse(url)
    except ValueError:
        return url.lower()

    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    path = p.path.rstrip("/")

    # drop common tracking params, keep everything else
    if p.query:
        keep = [
            kv
            for kv in p.query.split("&")
            if kv and kv.split("=", 1)[0].lower() not in TRACKING_PARAMS
        ]
        query = "&".join(keep)
    else:
        query = ""

    scheme = "https"  # treat http/https as equivalent for matching
    return urlunparse((scheme, host, path, "", query, ""))


def walk_vault(root: Path) -> set[str]:
    """Return all normalized URLs found in .md files under root."""
    found: set[str] = set()
    for md in root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for raw in URL_RE.findall(text):
            found.add(normalize_url(raw))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-label feeds from vault URLs")
    parser.add_argument(
        "--vault", type=Path, default=None, help="override vault path from config"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    vault = args.vault or Path(config["paths"]["obsidian_vault"]).expanduser()
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 1

    db_path = get_db_path(config)
    ensure_schema(db_path)

    print(f"scanning vault: {vault}")
    vault_urls = walk_vault(vault)
    print(f"found {len(vault_urls)} distinct urls in vault")

    with connect(db_path) as conn:
        feeds = conn.execute(
            "SELECT id, url, title FROM feeds WHERE url != ''"
        ).fetchall()
        existing = {
            r["feed_id"]: r["label"]
            for r in conn.execute("SELECT feed_id, label FROM taste_labels").fetchall()
        }

    matched: list[tuple[str, str, str]] = []  # (feed_id, current_label, title)
    for f in feeds:
        norm = normalize_url(f["url"])
        if norm in vault_urls:
            current = existing.get(f["id"])
            matched.append((f["id"], current or "unlabeled", f["title"][:80]))

    if not matched:
        print("no feed urls found in vault")
        return 0

    # categorize
    to_label: list[tuple[str, str]] = []  # (feed_id, title)
    to_upgrade: list[tuple[str, str]] = []
    respected: list[tuple[str, str]] = []
    already_rel: list[tuple[str, str]] = []

    for fid, current, title in matched:
        if current == "relevant":
            already_rel.append((fid, title))
        elif current == "not_relevant":
            respected.append((fid, title))
        elif current == "maybe":
            to_upgrade.append((fid, title))
        else:
            to_label.append((fid, title))

    print()
    print(f"matched {len(matched)} vault urls against feeds:")
    print(f"  already relevant: {len(already_rel)}")
    print(f"  will auto-label relevant: {len(to_label)}")
    print(f"  will upgrade maybe -> relevant: {len(to_upgrade)}")
    print(f"  respected (explicit not_relevant): {len(respected)}")

    if args.dry_run:
        print()
        print("dry run, no writes. sample upgrades:")
        for fid, t in (to_label + to_upgrade)[:10]:
            print(f"  {fid}  {t}")
        return 0

    for fid, _ in to_label:
        add_label(fid, "relevant", notes="auto: found in vault", db_path=db_path)
    for fid, _ in to_upgrade:
        add_label(
            fid,
            "relevant",
            notes="auto: upgraded from maybe (vault match)",
            db_path=db_path,
        )

    print()
    print(f"wrote {len(to_label) + len(to_upgrade)} labels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
