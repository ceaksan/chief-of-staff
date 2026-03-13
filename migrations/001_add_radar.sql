-- Migration 001: Add radar domain type
-- Adds radar_entries table and updates work_queue CHECK constraint

-- Radar entries table
CREATE TABLE IF NOT EXISTS radar_entries (
    id TEXT PRIMARY KEY,              -- radar entry id (SHA256 hash)
    source TEXT NOT NULL,             -- reddit, feed, etc.
    title TEXT NOT NULL,
    url TEXT,
    radar_category TEXT NOT NULL,     -- opportunity, trend, hiring
    confidence REAL,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_radar_source ON radar_entries(source);
CREATE INDEX IF NOT EXISTS idx_radar_category ON radar_entries(radar_category);

-- Recreate work_queue with 'radar' in CHECK constraint
-- SQLite doesn't support ALTER CHECK, so we recreate
CREATE TABLE IF NOT EXISTS work_queue_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_type TEXT NOT NULL CHECK (domain_type IN ('email', 'event', 'task', 'health', 'feed', 'radar')),
    domain_id TEXT NOT NULL,
    priority TEXT CHECK (priority IN ('P1', 'P2', 'P3', 'P4')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'classified', 'approved', 'dispatched', 'done', 'skipped', 'failed')),
    content_hash TEXT,
    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    UNIQUE(domain_type, domain_id)
);

INSERT OR IGNORE INTO work_queue_new SELECT * FROM work_queue;
DROP TABLE work_queue;
ALTER TABLE work_queue_new RENAME TO work_queue;

CREATE INDEX IF NOT EXISTS idx_wq_status ON work_queue(status);
CREATE INDEX IF NOT EXISTS idx_wq_priority ON work_queue(priority);
CREATE INDEX IF NOT EXISTS idx_wq_collected ON work_queue(collected_at);
CREATE INDEX IF NOT EXISTS idx_wq_hash ON work_queue(content_hash);

-- Recreate views (they reference work_queue)
DROP VIEW IF EXISTS v_queue_enriched;
CREATE VIEW v_queue_enriched AS
    SELECT
        wq.id AS queue_id,
        wq.domain_type,
        wq.domain_id,
        wq.priority,
        wq.status,
        wq.content_hash,
        wq.collected_at,
        wq.processed_at,
        c.category,
        c.reason,
        CASE wq.domain_type
            WHEN 'email' THEN e.subject
            WHEN 'event' THEN ev.summary
            WHEN 'task' THEN t.content
            WHEN 'health' THEN h.project || ': ' || h.status
            WHEN 'feed' THEN f.title
            WHEN 'radar' THEN r.title
        END AS title,
        CASE wq.domain_type
            WHEN 'email' THEN e.sender
            WHEN 'event' THEN ev.calendar_id
            WHEN 'task' THEN t.project
            WHEN 'health' THEN h.project
            WHEN 'feed' THEN f.feed_title
            WHEN 'radar' THEN r.source
        END AS context,
        CASE wq.domain_type
            WHEN 'email' THEN e.snippet
            WHEN 'event' THEN ev.location
            WHEN 'task' THEN t.file_path
            WHEN 'health' THEN h.last_error
            WHEN 'feed' THEN substr(f.content, 1, 200)
            WHEN 'radar' THEN r.reason
        END AS detail,
        CASE wq.domain_type
            WHEN 'email' THEN e.thread_id
            WHEN 'event' THEN ev.start_time
            WHEN 'task' THEN t.due_date
            WHEN 'health' THEN h.checked_at
            WHEN 'feed' THEN f.url
            WHEN 'radar' THEN r.url
        END AS extra
    FROM work_queue wq
    LEFT JOIN classifications c ON c.id = (
        SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
    )
    LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
    LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
    LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
    LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
    LEFT JOIN feeds f ON wq.domain_type = 'feed' AND wq.domain_id = f.id
    LEFT JOIN radar_entries r ON wq.domain_type = 'radar' AND wq.domain_id = r.id;

DROP VIEW IF EXISTS v_active_queue;
CREATE VIEW v_active_queue AS
    SELECT queue_id, domain_type, domain_id, priority, status, collected_at,
           category AS classification, reason AS classification_reason
    FROM v_queue_enriched
    WHERE status NOT IN ('done', 'skipped')
    AND collected_at >= datetime('now', '-3 days');

DROP VIEW IF EXISTS v_today_briefing;
CREATE VIEW v_today_briefing AS
    SELECT queue_id, domain_type, domain_id, priority, status,
           category AS classification, reason, title, context
    FROM v_queue_enriched
    WHERE date(collected_at) = date('now');

DROP VIEW IF EXISTS v_today_classified;
CREATE VIEW v_today_classified AS
    SELECT category AS classification, COUNT(*) AS count,
           GROUP_CONCAT(title, ', ') AS titles
    FROM v_queue_enriched
    WHERE date(collected_at) = date('now')
    AND category IS NOT NULL
    GROUP BY category;
