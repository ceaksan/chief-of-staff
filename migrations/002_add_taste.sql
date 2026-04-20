-- Taste filter: personal interest model built from user labels + embeddings.
-- Feeds get scored by semantic similarity to a user-defined "relevant" centroid.

-- User-provided labels on feed items. One label per item.
CREATE TABLE IF NOT EXISTS taste_labels (
    feed_id     TEXT PRIMARY KEY REFERENCES feeds(id) ON DELETE CASCADE,
    label       TEXT NOT NULL CHECK (label IN ('relevant', 'not_relevant', 'maybe')),
    labeled_at  TEXT NOT NULL DEFAULT (datetime('now')),
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_taste_labels_label ON taste_labels(label);

-- Per-item embeddings. Model column lets us re-embed with a new model without loss.
-- Vector stored as JSON array of floats (sqlite-vec not assumed; kept simple).
CREATE TABLE IF NOT EXISTS taste_embeddings (
    feed_id     TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      TEXT NOT NULL,        -- json float array
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feed_id, model)
);

-- Scores produced by scoring a feed item against the taste centroid.
-- Recomputed whenever labels change enough to shift the centroid.
CREATE TABLE IF NOT EXISTS taste_scores (
    feed_id         TEXT PRIMARY KEY REFERENCES feeds(id) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    score           REAL NOT NULL,    -- cos(item, relevant_centroid) - cos(item, not_relevant_centroid)
    bucket          TEXT NOT NULL CHECK (bucket IN ('auto_keep', 'borderline', 'auto_drop')),
    scored_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_taste_scores_bucket ON taste_scores(bucket);
CREATE INDEX IF NOT EXISTS idx_taste_scores_score  ON taste_scores(score);
