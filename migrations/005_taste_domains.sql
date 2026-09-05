-- Per-domain interest centroids built from the user's own published content
-- (ceaksan, ecodiurnal, arsiterans). Scoring picks the best-matching domain,
-- so multi-modal interests stop blurring into a single averaged vector.

CREATE TABLE IF NOT EXISTS taste_domain_centroids (
    domain    TEXT NOT NULL,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    TEXT NOT NULL,
    n_docs    INTEGER NOT NULL,
    built_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (domain, model)
);

ALTER TABLE taste_scores ADD COLUMN domain TEXT;
