# Feed Collector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Miniflux RSS feed collection to the chief-of-staff overnight pipeline so feeds are collected, classified, and rendered alongside emails/calendar/tasks in the Daily Note.

**Architecture:** Python collector calls Miniflux REST API (not MCP) to fetch unread entries, writes to SQLite `feeds` table + `work_queue`, renderer adds "Feed Highlights" section to Daily Note. Miniflux handles read/unread state natively, so no dedup logic needed on our side.

**Tech Stack:** Python 3, httpx (already used by other collectors), SQLite, Miniflux v2 REST API

---

### Task 1: Add `feeds` table to schema

**Files:**
- Modify: `schema.sql:~line 65` (after health_checks table)
- Modify: `cos.db` (apply migration)

**Step 1: Add feeds table to schema.sql**

Add after the `health_checks` table definition:

```sql
CREATE TABLE IF NOT EXISTS feeds (
    id          TEXT PRIMARY KEY,  -- miniflux entry id
    feed_id     INTEGER NOT NULL,
    feed_title  TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    author      TEXT,
    content     TEXT,              -- html content for summarization
    published_at TEXT NOT NULL,
    reading_time INTEGER DEFAULT 0,
    tags        TEXT DEFAULT '[]', -- json array
    collected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feeds_published ON feeds(published_at);
```

**Step 2: Add 'feed' to work_queue domain_type CHECK constraint**

Update the `domain_type` CHECK in work_queue from:
```sql
CHECK (domain_type IN ('email', 'event', 'task', 'health'))
```
to:
```sql
CHECK (domain_type IN ('email', 'event', 'task', 'health', 'feed'))
```

**Step 3: Apply migration to cos.db**

Run:
```bash
cd /path/to/chief-of-staff
sqlite3 cos.db "CREATE TABLE IF NOT EXISTS feeds (id TEXT PRIMARY KEY, feed_id INTEGER NOT NULL, feed_title TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, author TEXT, content TEXT, published_at TEXT NOT NULL, reading_time INTEGER DEFAULT 0, tags TEXT DEFAULT '[]', collected_at TEXT NOT NULL DEFAULT (datetime('now')));"
sqlite3 cos.db "CREATE INDEX IF NOT EXISTS idx_feeds_published ON feeds(published_at);"
```

Note: SQLite CHECK constraints on existing tables can't be altered in-place. The work_queue INSERT logic in Python already handles validation, so the CHECK is documentation-only. No migration needed for that.

**Step 4: Commit**

```bash
git add schema.sql
git commit -m "feat: add feeds table to schema for RSS feed collection"
```

---

### Task 2: Add feed config to config.toml

**Files:**
- Modify: `config.toml` (add [miniflux] section)

**Step 1: Add miniflux config section**

Add after `[health]`:

```toml
[miniflux]
base_url = "https://your-miniflux-instance.example.com"
api_token = "your-miniflux-api-token"
max_entries = 50          # max entries per collection run
lookback_hours = 24       # only fetch entries published in last N hours
mark_read = false         # mark fetched entries as read in miniflux (let Reeder handle this)
```

**Step 2: Commit**

```bash
git add config.toml
git commit -m "feat: add miniflux config section"
```

---

### Task 3: Write feed_collector.py

**Files:**
- Create: `collectors/feed_collector.py`
- Reference: `collectors/gmail_collector.py` (follow same patterns: db helpers, stats, logging)

**Step 1: Write the collector**

```python
"""Miniflux feed collector for chief-of-staff pipeline."""

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import tomllib

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.toml"
DB = ROOT / "cos.db"


def load_config():
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


def get_db():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def fetch_unread_entries(cfg: dict) -> list[dict]:
    """Fetch unread entries from Miniflux API."""
    base = cfg["miniflux"]["base_url"].rstrip("/")
    token = cfg["miniflux"]["api_token"]
    max_entries = cfg["miniflux"].get("max_entries", 50)
    lookback = cfg["miniflux"].get("lookback_hours", 24)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
    cutoff_unix = int(cutoff.timestamp())

    url = f"{base}/v1/entries"
    params = {
        "status": "unread",
        "order": "published_at",
        "direction": "desc",
        "limit": max_entries,
        "published_after": cutoff_unix,
    }
    headers = {"X-Auth-Token": token}

    resp = httpx.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("entries", [])


def estimate_priority(entry: dict) -> str:
    """Estimate priority based on feed and reading time."""
    reading_time = entry.get("reading_time", 0)
    tags = entry.get("tags", [])

    # Short, high-signal content is higher priority
    if reading_time <= 3:
        return "P3"
    return "P4"


def collect(dry_run: bool = False):
    """Main collection function."""
    cfg = load_config()
    db = get_db()

    started = datetime.now(timezone.utc).isoformat()
    stats = {"fetched": 0, "new": 0, "skipped": 0, "failed": 0}

    try:
        entries = fetch_unread_entries(cfg)
        stats["fetched"] = len(entries)
    except httpx.HTTPError as e:
        print(f"ERROR: Miniflux API failed: {e}", file=sys.stderr)
        db.execute(
            "INSERT INTO runs (layer, source, started_at, finished_at, status, items_processed, items_failed, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("collector", "feed", started, datetime.now(timezone.utc).isoformat(), "failed", 0, 0, str(e)),
        )
        db.commit()
        db.close()
        return stats

    for entry in entries:
        entry_id = str(entry["id"])

        # Skip if already collected
        existing = db.execute("SELECT id FROM feeds WHERE id = ?", (entry_id,)).fetchone()
        if existing:
            stats["skipped"] += 1
            continue

        try:
            # Truncate content to avoid bloating db (keep first 2000 chars for summarization)
            content = entry.get("content", "") or ""
            if len(content) > 2000:
                content = content[:2000]

            feed = entry.get("feed", {})
            tags = json.dumps(entry.get("tags", []))
            published = entry.get("published_at", "")

            if dry_run:
                print(f"  [{feed.get('title', '?')}] {entry.get('title', '?')}")
                stats["new"] += 1
                continue

            db.execute(
                "INSERT INTO feeds (id, feed_id, feed_title, title, url, author, content, published_at, reading_time, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    entry.get("feed_id", 0),
                    feed.get("title", ""),
                    entry.get("title", ""),
                    entry.get("url", ""),
                    entry.get("author", ""),
                    content,
                    published,
                    entry.get("reading_time", 0),
                    tags,
                ),
            )

            content_hash = hashlib.sha256(f"feed:{entry_id}".encode()).hexdigest()[:16]
            priority = estimate_priority(entry)

            db.execute(
                "INSERT INTO work_queue (domain_type, domain_id, priority, status, content_hash, collected_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("feed", entry_id, priority, "pending", content_hash, datetime.now(timezone.utc).isoformat()),
            )

            stats["new"] += 1
        except Exception as e:
            print(f"ERROR: Failed to process entry {entry_id}: {e}", file=sys.stderr)
            stats["failed"] += 1

    if not dry_run:
        db.execute(
            "INSERT INTO runs (layer, source, started_at, finished_at, status, items_processed, items_failed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("collector", "feed", started, datetime.now(timezone.utc).isoformat(), "ok", stats["new"], stats["failed"]),
        )
        db.commit()

    db.close()
    return stats


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = collect(dry_run=dry)
    print(f"Feed collection: {result['fetched']} fetched, {result['new']} new, {result['skipped']} skipped, {result['failed']} failed")
```

**Step 2: Test manually**

Run:
```bash
cd /path/to/chief-of-staff
source .venv/bin/activate 2>/dev/null || true
python collectors/feed_collector.py --dry-run
```
Expected: List of feed entries with titles, no db writes.

**Step 3: Test real collection**

Run:
```bash
python collectors/feed_collector.py
```
Expected: "Feed collection: N fetched, N new, 0 skipped, 0 failed"

Verify:
```bash
sqlite3 cos.db "SELECT count(*) FROM feeds;"
sqlite3 cos.db "SELECT count(*) FROM work_queue WHERE domain_type='feed';"
```

**Step 4: Commit**

```bash
git add collectors/feed_collector.py
git commit -m "feat: add feed collector using Miniflux API"
```

---

### Task 4: Add feed section to renderer

**Files:**
- Modify: `renderer.py` (add fetch_feeds + render section)

**Step 1: Add fetch_feeds function**

Add after `fetch_health()` (~line 60):

```python
def fetch_feeds(db, target_date):
    """Fetch collected feed entries for target date."""
    return db.execute(
        """
        SELECT f.title, f.url, f.feed_title, f.reading_time, f.author,
               wq.priority, c.category
        FROM feeds f
        JOIN work_queue wq ON wq.domain_type = 'feed' AND wq.domain_id = f.id
        LEFT JOIN classifications c ON c.queue_id = wq.id
        WHERE date(f.collected_at) = ?
        ORDER BY wq.priority ASC, f.published_at DESC
        """,
        (target_date,),
    ).fetchall()
```

**Step 2: Add feed rendering block**

In the `render()` function, add after the health section and before classified tasks:

```python
# Feed highlights
feeds = fetch_feeds(db, target_date)
if feeds:
    lines.append("## Feed Highlights")
    lines.append("")
    for f in feeds[:15]:  # cap at 15 items
        reading = f"~{f['reading_time']}min" if f['reading_time'] else ""
        lines.append(f"- [{f['title']}]({f['url']}) ({f['feed_title']}) {reading}")
    if len(feeds) > 15:
        lines.append(f"- ... +{len(feeds) - 15} more")
    lines.append("")
```

**Step 3: Test rendering**

Run:
```bash
python renderer.py --stdout
```
Expected: Daily note with "Feed Highlights" section listing feed entries.

**Step 4: Commit**

```bash
git add renderer.py
git commit -m "feat: add feed highlights section to daily note renderer"
```

---

### Task 5: Update collect.md prompt

**Files:**
- Modify: `prompts/collect.md` (add feed collection step)

**Step 1: Add feed collection step**

Add as a new step (between task collection and render):

```markdown
### Step 3b: Feed Collection

Run the feed collector to pull unread RSS entries from Miniflux:

```bash
python collectors/feed_collector.py
```

This fetches unread entries from the last 24 hours via Miniflux API and writes them to the `feeds` table + `work_queue`. No MCP needed - direct API call.
```

**Step 2: Update expected output counts**

Add feed count to the summary output format.

**Step 3: Commit**

```bash
git add prompts/collect.md
git commit -m "feat: add feed collection step to overnight collect prompt"
```

---

### Task 6: Update classifier to handle feeds

**Files:**
- Modify: `collectors/classifier.py` (add feed to export_pending join)

**Step 1: Add feed join to export_pending**

In `export_pending()`, add a case for `domain_type = 'feed'`:

```python
elif row["domain_type"] == "feed":
    feed = db.execute(
        "SELECT title, url, feed_title, content FROM feeds WHERE id = ?",
        (row["domain_id"],),
    ).fetchone()
    if feed:
        item["title"] = feed["title"]
        item["url"] = feed["url"]
        item["source"] = feed["feed_title"]
        item["snippet"] = (feed["content"] or "")[:200]
```

**Step 2: Add force rules for feeds**

In config.toml, optionally add:

```toml
[classification]
force_skip_feeds = ["Upwork"]  # auto-skip certain feed sources from classification
```

**Step 3: Commit**

```bash
git add collectors/classifier.py
git commit -m "feat: add feed support to classifier export"
```

---

### Task 7: Verify full pipeline end-to-end

**Step 1: Reset and re-collect**

```bash
python collectors/feed_collector.py
python renderer.py --stdout
```

**Step 2: Verify database state**

```bash
sqlite3 cos.db "SELECT domain_type, count(*) FROM work_queue GROUP BY domain_type;"
sqlite3 cos.db "SELECT feed_title, count(*) FROM feeds GROUP BY feed_title ORDER BY count(*) DESC LIMIT 10;"
```

Expected: `feed` entries in work_queue, feeds grouped by source.

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat: complete feed collection pipeline integration"
```
