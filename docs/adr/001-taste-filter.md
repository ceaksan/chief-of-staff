# ADR-001: Personal Taste Filter via Embeddings

**Date:** 2026-04-21
**Status:** Accepted

## Context

The feed layer pulls 500-1000 RSS/Atom entries per day from Miniflux. Manual triage is impossible at that volume; Claude Sonnet classification per item would cost ~$50-100/month and still not reflect personal taste nuance (language, topic, context dependence). We needed a filter that:

- Learns from user labels rather than hardcoded keywords
- Handles Turkish + English simultaneously
- Costs near-zero at steady-state (already-running infrastructure)
- Produces a graded verdict, not just keep/drop, so the daily brief can show only the top signal

User labeling sessions are expensive (minutes of human time). The filter must be data-efficient: good results from the first few hundred labels, improving with implicit feedback afterward.

## Decision

Build a centroid-based semantic filter on top of Ollama embeddings, running on the Mac mini over Tailscale.

**Pipeline per feed item:**

1. Detect language at ingestion (`langdetect`); non-TR/EN items get tagged but excluded from centroid math.
2. Embed title + snippet via Ollama `zylonai/multilingual-e5-large` (1024 dim, "query:" prefix per E5 model card).
3. Score against two centroids (mean vector of `relevant` labels, mean of `not_relevant`): `score = cos(item, relevant) - cos(item, not_relevant)`.
4. Bucket into 4 tiers using empirically calibrated thresholds:
   - `high_keep` >= +0.010  (daily brief)
   - `auto_keep` >= +0.002  (weekly digest)
   - `borderline` (in between, labeling queue)
   - `auto_drop` <= -0.001  (silent)

**Label sources (priority order):**

1. Explicit labels via `collectors/taste_label.py` (interactive terminal, active-learning order).
2. Miniflux starred entries via `collectors/taste_starred_sync.py` (nightly, strong positive signal).
3. Vault URL matches via `collectors/vault_label.py` (periodic, URLs saved by the user are implicitly relevant).

Explicit `not_relevant` always beats any implicit positive signal.

**Evaluation on 10.8k feeds, 159 relevant + 84 not_relevant labels:**

| Tier | Precision | Recall | F1 |
|------|-----------|--------|-----|
| auto_keep | 94.8% | 79.1% | 86.3% |
| auto_drop | 78.1% | 75.8% | 76.9% |

Bucket split on corpus: 12% high_keep, 46% auto_keep, 10% borderline, 30% auto_drop — ~90% auto-decided, 10% reserved for the weekly review queue.

## Consequences

### Positive

- Noise sources identified at feed level (9 pure-noise Miniflux feeds accounting for ~20% of volume) — unsubscribing upstream is more effective than filtering downstream.
- Stable metrics 15x scale-up (742 → 10.5k) with no overfitting to the small labeled subset.
- No per-item LLM cost; Ollama on Mac mini handles embeddings for free.
- Self-reinforcing: nightly starred sync grows the label set without active user effort.
- Additive — coexists with the existing Sonnet classifier, which handles a different dimension (urgency/dispatch).

### Negative

- Centroid approach averages the user's interests into a single direction, so genuinely multi-modal tastes (AI-coding + biology + sociology) pull against each other. Mitigation: feed-level labeling sessions (e.g. `--feed "AAAS"`) force under-represented topics into the centroid.
- Non-TR/EN articles never get scored, only filtered. Acceptable because the user's actual reading is TR/EN.
- E5 embeddings produce dense clusters (stdev 0.007 on a 10k corpus), so thresholds are narrow and sensitive to model changes. Schema tags model identity with a `#vN` suffix so re-embedding with a new version is safe.

### Alternatives Considered

- **Per-item LLM classification (Sonnet or Kimi):** $50-100/month ongoing, slow (seconds per item), and no better on semantic nuance than embeddings. Rejected.
- **TF-IDF / keyword rules:** Can't capture cross-language semantic equivalence ("fog harvesting" ≈ "sis toplama"). Rejected.
- **sentence-transformers on the MacBook Air:** Adds ~400MB torch + model weights to a memory-constrained machine. Rejected in favor of Ollama on the Mac mini over Tailscale.
- **kNN over embeddings (instead of centroid):** Technically better for multi-modal tastes, but O(N) scoring per query. Deferred; centroid is the cheaper baseline and metrics are already usable. Revisit if the weekly check-in shows F1 plateauing.
- **Pseudo-label loop (model's own high-confidence outputs become training data):** Known confirmation-bias failure mode in active learning literature. Rejected.

## Files Changed

| File | Change |
|------|--------|
| `cos/taste.py` | New module: embedding, centroid, scoring |
| `cos/language.py` | New module: TR/EN detection (langdetect + heuristic fallback) |
| `cos/db.py` | `insert_feed` writes `language` column |
| `collectors/feed_collector.py` | `parse_entry` calls language detection |
| `collectors/feed_backfill.py` | New: paginated one-shot Miniflux backfill |
| `collectors/taste_label.py` | New: interactive labeling TUI, active-learning order, `--feed` filter |
| `collectors/taste_weekly.py` | New: 3-section weekly check-in (uncertainty, sanity, rescue) |
| `collectors/taste_starred_sync.py` | New: nightly Miniflux starred → relevant auto-label |
| `collectors/vault_label.py` | New: vault URL scan → relevant auto-label |
| `collectors/taste_feed_audit.py` | New: per-feed noise/signal audit |
| `collectors/taste_report.py` | New: full-corpus metrics report |
| `schema.sql` | `feeds.language` column + index |
| `migrations/002_add_taste.sql` | `taste_labels`, `taste_embeddings`, `taste_scores` tables |
| `migrations/003_add_language.sql` | `feeds.language` backfill migration |
| `migrations/004_taste_high_keep.sql` | Adds `high_keep` bucket tier |
| `cos-brief.sh` | `taste` subcommand (status/weekly/label/score/rebuild/vault/starred/audit/report); nightly pipeline adds `run_collect` step 2b (starred sync + embed + rescore) |
| `requirements.txt` | Adds `langdetect` |
