-- Detected language per feed item. Populated at ingestion or by a backfill pass.
-- Used to drop items outside the user's reading languages (tr, en) before embedding.
ALTER TABLE feeds ADD COLUMN language TEXT;

CREATE INDEX IF NOT EXISTS idx_feeds_language ON feeds(language);
