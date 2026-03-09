-- Chief of Staff - SQLite Schema v2
-- Domain tables + work queue + classifications + actions
-- Run with: sqlite3 cos.db < schema.sql

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- Domain Tables (source-specific, indexable columns)
-- ============================================================

CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,              -- gmail message_id
    thread_id TEXT,
    subject TEXT,
    sender TEXT,
    snippet TEXT,
    labels TEXT,                      -- JSON array
    received_at TEXT NOT NULL,
    raw_payload JSON                  -- full MCP response for reference
);

CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_sender ON emails(sender);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,              -- gcal event_id
    calendar_id TEXT NOT NULL,
    summary TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    location TEXT,
    is_calendly INTEGER DEFAULT 0,
    prep_needed INTEGER DEFAULT 0,
    raw_payload JSON
);

CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
CREATE INDEX IF NOT EXISTS idx_events_calendar ON events(calendar_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,              -- hash of file_path + line content
    file_path TEXT NOT NULL,          -- obsidian vault path
    line_number INTEGER,
    content TEXT NOT NULL,
    project TEXT,                     -- extracted project tag
    due_date TEXT,
    raw_payload JSON
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

CREATE TABLE IF NOT EXISTS health_checks (
    id TEXT PRIMARY KEY,              -- project_name + date
    project TEXT NOT NULL,
    status TEXT NOT NULL,             -- ok, warning, error, down
    uptime REAL,
    errors_24h INTEGER DEFAULT 0,
    last_error TEXT,
    last_deploy TEXT,
    checked_at TEXT NOT NULL,
    raw_payload JSON
);

CREATE INDEX IF NOT EXISTS idx_health_project ON health_checks(project);
CREATE INDEX IF NOT EXISTS idx_health_status ON health_checks(status);

-- ============================================================
-- Feeds (RSS/Atom entries from Miniflux)
-- ============================================================

CREATE TABLE IF NOT EXISTS feeds (
    id          TEXT PRIMARY KEY,      -- miniflux entry id
    feed_id     INTEGER NOT NULL,
    feed_title  TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    author      TEXT,
    content     TEXT,                  -- truncated html for summarization
    published_at TEXT NOT NULL,
    reading_time INTEGER DEFAULT 0,
    tags        TEXT DEFAULT '[]',     -- json array
    collected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feeds_published ON feeds(published_at);

-- ============================================================
-- Work Queue (links domain items to pipeline lifecycle)
-- ============================================================

CREATE TABLE IF NOT EXISTS work_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_type TEXT NOT NULL CHECK (domain_type IN ('email', 'event', 'task', 'health', 'feed')),
    domain_id TEXT NOT NULL,          -- FK to domain table (emails.id, events.id, etc.)
    priority TEXT CHECK (priority IN ('P1', 'P2', 'P3', 'P4')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'classified', 'approved', 'dispatched', 'done', 'skipped', 'failed')),
    content_hash TEXT,                -- SHA256 of payload for dedup/cache
    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    UNIQUE(domain_type, domain_id)
);

CREATE INDEX IF NOT EXISTS idx_wq_status ON work_queue(status);
CREATE INDEX IF NOT EXISTS idx_wq_priority ON work_queue(priority);
CREATE INDEX IF NOT EXISTS idx_wq_collected ON work_queue(collected_at);
CREATE INDEX IF NOT EXISTS idx_wq_hash ON work_queue(content_hash);

-- ============================================================
-- Classifications (audit trail, model provenance)
-- ============================================================

CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('dispatch', 'prep', 'yours', 'skip')),
    reason TEXT,                       -- one-line explanation from classifier
    model TEXT,                        -- e.g. claude-sonnet-4-20250514
    prompt_version TEXT,               -- git short hash of classifier prompt
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (queue_id) REFERENCES work_queue(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cls_queue ON classifications(queue_id);
CREATE INDEX IF NOT EXISTS idx_cls_category ON classifications(category);

-- ============================================================
-- Actions (what agents did, linked back to queue items)
-- ============================================================

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    agent TEXT NOT NULL,               -- email, dev_prep, content, calendar
    action_type TEXT NOT NULL,         -- draft_created, note_written, summary_generated
    external_ref TEXT,                 -- gmail draft ID, obsidian file path, etc.
    output_summary TEXT,               -- brief description of what was done
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('completed', 'failed', 'needs_review')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (queue_id) REFERENCES work_queue(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_actions_queue ON actions(queue_id);
CREATE INDEX IF NOT EXISTS idx_actions_agent ON actions(agent);

-- ============================================================
-- Pipeline Runs (execution log)
-- ============================================================

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL,               -- collector, classifier, sweep, dayblock
    source TEXT,                       -- gmail, calendar, health, task (for collectors)
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'partial')),
    items_processed INTEGER DEFAULT 0,
    items_failed INTEGER DEFAULT 0,
    error TEXT,
    budget_used REAL                   -- claude budget consumed
);

CREATE INDEX IF NOT EXISTS idx_runs_layer ON runs(layer);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

-- ============================================================
-- Views
-- ============================================================

-- Active items: pending or in-progress, last 3 days
CREATE VIEW IF NOT EXISTS v_active_queue AS
    SELECT
        wq.id AS queue_id,
        wq.domain_type,
        wq.domain_id,
        wq.priority,
        wq.status,
        wq.collected_at,
        c.category AS classification,
        c.reason AS classification_reason
    FROM work_queue wq
    LEFT JOIN classifications c ON c.id = (
        SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
    )
    WHERE wq.status NOT IN ('done', 'skipped')
    AND wq.collected_at >= datetime('now', '-3 days');

-- Today's briefing data
CREATE VIEW IF NOT EXISTS v_today_briefing AS
    SELECT
        wq.id AS queue_id,
        wq.domain_type,
        wq.domain_id,
        wq.priority,
        wq.status,
        c.category AS classification,
        c.reason,
        CASE wq.domain_type
            WHEN 'email' THEN e.subject
            WHEN 'event' THEN ev.summary
            WHEN 'task' THEN t.content
            WHEN 'health' THEN h.project || ': ' || h.status
        END AS title,
        CASE wq.domain_type
            WHEN 'email' THEN e.sender
            WHEN 'event' THEN ev.calendar_id
            WHEN 'task' THEN t.project
            WHEN 'health' THEN h.project
        END AS context
    FROM work_queue wq
    LEFT JOIN classifications c ON c.id = (
        SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
    )
    LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
    LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
    LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
    LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
    WHERE date(wq.collected_at) = date('now');

-- Today's classified summary
CREATE VIEW IF NOT EXISTS v_today_classified AS
    SELECT
        c.category AS classification,
        COUNT(*) AS count,
        GROUP_CONCAT(
            CASE wq.domain_type
                WHEN 'email' THEN e.subject
                WHEN 'event' THEN ev.summary
                WHEN 'task' THEN t.content
                WHEN 'health' THEN h.project
            END,
            ', '
        ) AS titles
    FROM work_queue wq
    JOIN classifications c ON c.id = (
        SELECT id FROM classifications WHERE queue_id = wq.id ORDER BY created_at DESC LIMIT 1
    )
    LEFT JOIN emails e ON wq.domain_type = 'email' AND wq.domain_id = e.id
    LEFT JOIN events ev ON wq.domain_type = 'event' AND wq.domain_id = ev.id
    LEFT JOIN tasks t ON wq.domain_type = 'task' AND wq.domain_id = t.id
    LEFT JOIN health_checks h ON wq.domain_type = 'health' AND wq.domain_id = h.id
    WHERE date(wq.collected_at) = date('now')
    GROUP BY c.category;
