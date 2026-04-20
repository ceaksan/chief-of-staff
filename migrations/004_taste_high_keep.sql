-- Add a 'high_keep' tier above the existing 'auto_keep'.
-- high_keep surfaces to the daily brief; auto_keep stays for the weekly digest.
-- SQLite can't ALTER a CHECK constraint in place, so we rebuild the table.

CREATE TABLE IF NOT EXISTS taste_scores_new (
    feed_id     TEXT PRIMARY KEY REFERENCES feeds(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    score       REAL NOT NULL,
    bucket      TEXT NOT NULL CHECK (bucket IN ('high_keep', 'auto_keep', 'borderline', 'auto_drop')),
    scored_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO taste_scores_new (feed_id, model, score, bucket, scored_at)
SELECT feed_id, model, score, bucket, scored_at FROM taste_scores;

DROP TABLE taste_scores;
ALTER TABLE taste_scores_new RENAME TO taste_scores;

CREATE INDEX IF NOT EXISTS idx_taste_scores_bucket ON taste_scores(bucket);
CREATE INDEX IF NOT EXISTS idx_taste_scores_score  ON taste_scores(score);
