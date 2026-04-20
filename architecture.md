# Chief of Staff - Architecture

Local-first AI assistant that automates daily operational overhead for solo entrepreneurs.

<!--
Living Architecture Template v1.0
Source: https://github.com/ceaksan/living-architecture
Depth: L2
Last verified: 2026-04-21
-->

## Stack & Dependencies

### Runtime

| Package | Version | Purpose |
|---------|---------|---------|
| Python | 3.12+ | Core runtime |
| SQLite | 3.x (stdlib) | Local database, WAL mode |
| tomllib | stdlib (3.11+) | Config parsing |
| httpx | 0.27+ | Miniflux + Ollama HTTP calls |
| langdetect | 1.0+ | Feed language detection at ingestion |

### Infrastructure

| Layer | Technology | Detail |
|-------|-----------|--------|
| Database | SQLite (WAL) | Single file `cos.db`, no external DB |
| Scheduler | macOS launchd | Overnight pipeline via `.plist` |
| AI | Claude Code CLI | Prompts executed via `claude -p` with budget caps |
| Vault | Obsidian | Daily Notes output target |
| RSS | Miniflux | Self-hosted, REST API for feed collection |
| Health Monitoring | Cloudflare API + Coolify API | Workers, Pages, Apps, Services, Databases |
| MCP | Gmail, Google Calendar | Claude-native connectors for email/calendar |
| Embeddings | Ollama (Mac mini over Tailscale) | `multilingual-e5-large` (1024 dim) for taste filter; no API cost |

### Build & Test

| Package | Version | Purpose |
|---------|---------|---------|
| pytest | latest | Unit tests |
| tomli | latest | TOML fallback for Python < 3.11 |

## Module Map

```
chief-of-staff/
├── cos/                         # Core library (shared across all layers)
│   ├── config.py                # TOML config loader
│   ├── db.py                    # SQLite access layer, all insert/query/cleanup functions
│   ├── log.py                   # Structured JSON logging (daily rotation)
│   ├── language.py              # TR/EN detection at ingestion (langdetect + heuristic)
│   └── taste.py                 # Embedding, centroid, scoring, migration helper
│
├── collectors/                  # Data collection + pipeline stages
│   ├── calendar_collector.py    # Google Calendar MCP response -> events table
│   ├── gmail_collector.py       # Gmail MCP response -> emails table (filters non-actionable)
│   ├── feed_collector.py        # Miniflux REST API -> feeds table
│   ├── task_collector.py        # Obsidian vault grep -> tasks table
│   ├── health_collector.py      # User health scripts -> health_checks table
│   └── health_scripts/             # Platform-specific health collectors
│       ├── cloudflare_health.py    # Workers analytics (GraphQL) + Pages deployments (REST)
│       └── coolify_health.py       # Apps, services, databases via Coolify API
│   ├── orchestrator.py          # Parallel sweep orchestrator (asyncio, semaphore concurrency)
│   ├── radar_collector.py       # Opportunity Radar signal importer
│   ├── classifier.py            # Pending items -> classifications (export/import CLI)
│   ├── sweep.py                 # Morning sweep dispatcher (export/record/complete CLI)
│   ├── feed_backfill.py         # One-shot paginated Miniflux backfill (history import)
│   ├── taste_label.py           # Interactive terminal labeling TUI (active-learning order)
│   ├── taste_weekly.py          # Weekly check-in: uncertainty + sanity + rescue queues
│   ├── taste_starred_sync.py    # Miniflux starred -> relevant auto-label (nightly)
│   ├── vault_label.py           # Vault URL scan -> relevant auto-label (periodic)
│   ├── taste_feed_audit.py      # Per-feed noise/signal audit (unsubscribe candidates)
│   └── taste_report.py          # Full-corpus metrics + top-bucket samples
│
├── prompts/                     # Claude system prompts (executed via claude -p)
│   ├── collect.md               # MCP collection instructions
│   ├── classifier.md            # Classification rules + decision framework
│   ├── sweep.md                 # Morning sweep agent instructions
│   ├── brief.md                 # Turkish daily brief template
│   └── agents/                  # Domain-specific subagent prompts
│       ├── email-agent.md       # Email draft creation (Opus, $1.00)
│       ├── calendar-agent.md    # Meeting prep notes (Sonnet, $0.50)
│       ├── health-agent.md      # Error analysis (Sonnet, $0.50)
│       ├── task-agent.md        # Task notes (Sonnet, $0.50)
│       └── feed-agent.md        # Feed summaries (Sonnet, $0.50)
│
├── tests/                       # pytest unit tests
│   ├── test_db.py
│   ├── test_gmail_collector.py
│   ├── test_calendar_collector.py
│   ├── test_task_collector.py
│   ├── test_health_collector.py
│   ├── test_feed_collector.py
│   ├── test_classifier.py
│   ├── test_renderer.py
│   ├── test_sweep.py
│   └── test_orchestrator.py
│
├── schema.sql                   # Full SQLite schema (core tables + views)
├── migrations/                  # Schema migrations (taste tables, language, tiers)
│   ├── 001_add_radar.sql
│   ├── 002_add_taste.sql
│   ├── 003_add_language.sql
│   └── 004_taste_high_keep.sql
├── renderer.py                  # SQLite -> Obsidian Daily Note markdown
├── run.sh                       # Pipeline orchestrator (mutex, step routing)
├── cos-brief.sh                 # Unified CLI (brief, run, status, weekly, insights, taste subcommands)
├── setup_wizard.py              # Interactive setup (config + db + launchd)
├── config.toml                  # User config (gitignored)
├── config.example.toml          # Config template
└── com.chief-of-staff.overnight.plist  # launchd schedule (gitignored)
```

## Data Flow

### Full Pipeline (collect -> classify -> render; sweep is manual)

```
                    ┌──────────────────────────────────────────────┐
                    │              run.sh (mutex lock)             │
                    └──────────────────────────────────────────────┘
                                        │
            ┌───────────────────────────┼───────────────────────────┐
            ▼                           ▼                           ▼
    ┌───────────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐
    │ Claude + MCP  │  │ Python HTTP  │  │ Python HTTP  │  │  Python grep  │
    │ Gmail/Calendar│  │ Miniflux API │  │ CF + Coolify │  │ Obsidian vault│
    └───────┬───────┘  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘
            │ .tmp/cos_*.json          │              │           │
            ▼                          ▼              ▼           ▼
    ┌──────────────────────────────────────────────────────────────┐
    │                     cos.db (SQLite WAL)                      │
    │  emails | events | tasks | health_checks | feeds             │
    │                     ┌──────────┐                             │
    │                     │work_queue│ (hub table)                 │
    │                     └──────────┘                             │
    └──────────────────────────────────────────────────────────────┘
            │                                        │
            ▼                                        ▼
    ┌───────────────┐                       ┌───────────────┐
    │  Classifier   │                       │   Renderer    │
    │ (Claude CLI)  │                       │ (Python)      │
    │ dispatch/prep │                       │ SQLite ->     │
    │ yours/skip    │                       │ Obsidian .md  │
    └───────┬───────┘                       └───────────────┘
            │
            ▼
    ┌───────────────────────────────────────┐
    │     Orchestrator (Python asyncio)      │
    │     collectors/orchestrator.py          │
    │     Semaphore: max_workers=2           │
    └───┬───────┬───────┬───────┬───────┬───┘
        │       │       │       │       │
        ▼       ▼       ▼       ▼       ▼
    ┌──────┐┌──────┐┌──────┐┌──────┐
    │cal   ││health││task  ││feed  │
    │agent ││agent ││agent ││agent │
    │(Son.)││(Son.)││(Son.)││(Son.)│
    └──┬───┘└──┬───┘└──┬───┘└──┬───┘
       │       │       │       │
       └───────┴───────┴───────┘
       (email agent exists but excluded from dispatch)
                       │
                       ▼
               .tmp/*_output.json
                       │
                       ▼
               apply_actions() + mark_done()
                       │
                       ▼
                    cos.db
```

### Collection Flow (per source)

```
MCP/API/grep  -->  .tmp/*.json (optional)  -->  collectors/*_collector.py
                                                     │
                                              domain table INSERT
                                              work_queue INSERT
                                              (INSERT OR IGNORE = idempotent)
```

### Classification Flow

```
work_queue (status=pending)
    │
    ├── force_yours keywords match?  --> classify as "yours"
    ├── force_dispatch keywords match? --> classify as "dispatch"
    └── remaining --> Claude Sonnet classifies
    │
    ▼
classifications table (audit trail: model, prompt_version, reason)
work_queue status -> "classified"
```

### Taste Filter Flow (Layer 1.7)

Runs as step 2b inside `run_collect`, after feeds land in `cos.db`.

```
new feeds  ->  language detection (tr/en kept, others parked)
                         │
                         ▼
    Miniflux starred sync  -->  taste_labels (implicit relevant)
                         │
                         ▼
    Ollama /api/embed (Mac mini over Tailscale)  -->  taste_embeddings
                         │
                         ▼
    build_centroids (mean of 'relevant' vs 'not_relevant' vectors)
                         │
                         ▼
    score_all: cos(item, rel) - cos(item, not_rel)
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼              ▼
    high_keep      auto_keep      borderline     auto_drop
  (daily brief) (weekly digest) (label queue)    (silent)
```

Explicit `not_relevant` always beats implicit positive signals (starred, vault). Non-TR/EN items never reach scoring — they're parked with their `language` tag and can be revisited with language-specific models later.

## Route / API Structure

No HTTP routes. CLI-based pipeline:

### run.sh Commands

| Command | Description | Claude Model |
|---------|-------------|-------------|
| `./run.sh full` | All 4 steps sequentially | Sonnet + Opus |
| `./run.sh collect` | Gmail + Calendar (MCP) + Feed + Health + Task | Sonnet |
| `./run.sh classify` | Classify pending work_queue items | Sonnet |
| `./run.sh sweep` | Execute dispatch/prep via parallel orchestrator | Opus |
| `./run.sh sweep-seq` | Sequential sweep (debugging) | Per-agent |
| `./run.sh weekly` | Weekly stats digest | None |
| `./run.sh insights` | Scheduling insights (volume by day) | None |
| `./run.sh render` | Regenerate Daily Note from cos.db | None |
| `./run.sh status` | Show pipeline stats | None |
| `./run.sh cleanup [days]` | Purge old records (default 30 days) | None |

### cos-brief.sh taste Subcommands

| Command | Description | Compute |
|---------|-------------|---------|
| `cos taste status` | Label + bucket summary, centroid stats | None |
| `cos taste weekly` | 3-section check-in (uncertainty + sanity + rescue), auto-rebuild | Ollama (on labeled set) |
| `cos taste label [--feed NAME] [--order recent|uncertainty]` | Interactive terminal labeling | None |
| `cos taste score` | Embed new feeds + rescore all | Ollama (batched) |
| `cos taste rebuild` | Recompute centroids + rescore (after threshold changes) | None (vectors cached) |
| `cos taste vault [--dry-run]` | Scan Obsidian vault URLs -> auto-label relevant | None |
| `cos taste starred [--dry-run]` | Sync Miniflux starred -> auto-label relevant, ingest older starred | None |
| `cos taste audit [--min-items N]` | Per-feed noise/signal audit | None |
| `cos taste report` | Full-corpus metrics + bucket samples | None |

### Collector CLIs

| Collector | Interface | Input |
|-----------|-----------|-------|
| `calendar_collector.py` | `--json <path>` | MCP response JSON |
| `gmail_collector.py` | `--json <path>` | MCP response JSON |
| `feed_collector.py` | (no args) | Miniflux API via config |
| `task_collector.py` | (no args) | Obsidian vault path via config |
| `health_collector.py` | (no args) | Health script paths via config |
| `health_scripts/cloudflare_health.py` | (no args) | Cloudflare API via config |
| `health_scripts/coolify_health.py` | (no args) | Coolify API via config |
| `orchestrator.py` | `[--sequential] [--dry-run]` | cos.db (via sweep.py) |
| `radar_collector.py` | (no args) | Opportunity Radar pending.json via config |
| `classifier.py` | `export` / `import --json <path>` | cos.db |
| `sweep.py` | `export` / `record --json <path>` / `complete --ids` | cos.db |

## Data Model

### Domain Tables

**emails**

| Column | Type | Detail |
|--------|------|--------|
| id | TEXT PK | Gmail message_id |
| thread_id | TEXT | For thread grouping |
| subject | TEXT | Email subject |
| sender | TEXT | From address |
| snippet | TEXT | Preview text |
| labels | TEXT | JSON array of Gmail labels |
| received_at | TEXT | ISO datetime |
| raw_payload | JSON | Full MCP response |

**events**

| Column | Type | Detail |
|--------|------|--------|
| id | TEXT PK | Google Calendar event_id |
| calendar_id | TEXT | Which calendar |
| summary | TEXT | Event title |
| start_time, end_time | TEXT | ISO datetime |
| location | TEXT | Venue |
| is_calendly | INTEGER | 0/1 flag |
| prep_needed | INTEGER | 0/1 flag |

**tasks**

| Column | Type | Detail |
|--------|------|--------|
| id | TEXT PK | SHA hash of file_path + content |
| file_path | TEXT | Obsidian vault path |
| content | TEXT | Task text |
| project | TEXT | Extracted `#tag` |
| due_date | TEXT | From `@due(YYYY-MM-DD)` |

**health_checks**

| Column | Type | Detail |
|--------|------|--------|
| id | TEXT PK | project_name + date |
| project | TEXT | Project identifier |
| status | TEXT | ok / warning / error / down |
| uptime | REAL | Percentage |
| errors_24h | INTEGER | Error count |
| last_error | TEXT | Most recent error message |

**feeds**

| Column | Type | Detail |
|--------|------|--------|
| id | TEXT PK | Miniflux entry ID |
| feed_id | INTEGER | Miniflux feed ID |
| feed_title | TEXT | Source feed name |
| title | TEXT | Entry title |
| url | TEXT | Entry URL |
| reading_time | INTEGER | Minutes |
| tags | TEXT | JSON array |
| language | TEXT | Detected language: `tr`, `en`, `other`, `und` |

**radar_entries**

| Column | Type | Detail |
|--------|------|--------|
| id | TEXT PK | SHA256 hash |
| source | TEXT | reddit / feed / etc. |
| title | TEXT | Signal title |
| url | TEXT | Source URL |
| radar_category | TEXT | opportunity / trend / hiring |
| confidence | REAL | Score 0-1 |
| reason | TEXT | Why flagged |

### Pipeline Tables

**work_queue** (hub: links all domain items to pipeline lifecycle)

| Column | Type | Detail |
|--------|------|--------|
| id | INTEGER PK | Auto-increment |
| domain_type | TEXT | email / event / task / health / feed / radar |
| domain_id | TEXT | FK to domain table |
| priority | TEXT | P1 / P2 / P3 / P4 |
| status | TEXT | pending -> classified -> approved -> dispatched -> done / skipped / failed |
| content_hash | TEXT | SHA256 for dedup/cache |
| UNIQUE | | (domain_type, domain_id) |

**classifications** (audit trail)

| Column | Type | Detail |
|--------|------|--------|
| queue_id | INTEGER FK | References work_queue.id (CASCADE) |
| category | TEXT | dispatch / prep / yours / skip |
| reason | TEXT | One-line explanation |
| model | TEXT | e.g., claude-sonnet |
| prompt_version | TEXT | Git short hash |

**actions** (agent work log)

| Column | Type | Detail |
|--------|------|--------|
| queue_id | INTEGER FK | References work_queue.id (CASCADE) |
| agent | TEXT | email / dev_prep / content / calendar |
| action_type | TEXT | draft_created / note_written / summary_generated / acknowledged |
| external_ref | TEXT | Gmail draft ID, Obsidian path, etc. |
| status | TEXT | completed / failed / needs_review |

**runs** (execution log)

| Column | Type | Detail |
|--------|------|--------|
| layer | TEXT | collector / classifier / sweep / dayblock |
| source | TEXT | gmail / calendar / health / task / feed |
| status | TEXT | running / completed / failed / partial |
| items_processed | INTEGER | Count |
| budget_used | REAL | Claude API cost in USD |

### Taste Filter Tables

**taste_labels** (explicit and implicit labels on feed items)

| Column | Type | Detail |
|--------|------|--------|
| feed_id | TEXT PK FK | References feeds.id (CASCADE) |
| label | TEXT | `relevant` / `not_relevant` / `maybe` |
| labeled_at | TEXT | ISO datetime |
| notes | TEXT | Free-form note (e.g. `auto: miniflux starred`) |

**taste_embeddings** (cached vectors per model)

| Column | Type | Detail |
|--------|------|--------|
| feed_id | TEXT FK | References feeds.id (CASCADE) |
| model | TEXT | Model identifier with `#vN` suffix (e.g. `zylonai/multilingual-e5-large:latest#v2`) |
| dim | INTEGER | Vector dimension (1024 for e5-large) |
| vector | TEXT | JSON float array (L2-normalized at write) |
| created_at | TEXT | ISO datetime |
| PK | | (feed_id, model) |

**taste_scores** (centroid scoring output)

| Column | Type | Detail |
|--------|------|--------|
| feed_id | TEXT PK FK | References feeds.id (CASCADE) |
| model | TEXT | Same identifier as embeddings row |
| score | REAL | `cos(item, relevant) - cos(item, not_relevant)` |
| bucket | TEXT | `high_keep` / `auto_keep` / `borderline` / `auto_drop` |
| scored_at | TEXT | ISO datetime |

### Views

| View | Purpose |
|------|---------|
| `v_queue_enriched` | Work queue joined with all domain tables + latest classification. Base view for all queries. |
| `v_active_queue` | Non-done/skipped items from last 3 days |
| `v_today_briefing` | Today's items with classification + title + context |
| `v_today_classified` | Category summary with counts and grouped titles |

### Relationships

```
emails ──┐
events ──┤
tasks  ──┤
health ──┼── work_queue ──┬── classifications (CASCADE)
feeds  ──┤                └── actions (CASCADE)
radar  ──┘

runs (independent, execution log only)
```

## Configuration & Environment

| Variable | Purpose | Secret |
|----------|---------|--------|
| `config.toml` | All configuration | Yes (gitignored) |
| `cos.db` | SQLite database | Yes (gitignored) |

### config.toml Sections

| Section | Keys | Purpose |
|---------|------|---------|
| `[paths]` | obsidian_vault, daily_notes_dir, health_scripts_dir, cos_dir | File system paths |
| `[calendars]` | ids, ai_plan_calendar_id | Google Calendar IDs to scan |
| `[gmail]` | exclude_labels, lookback_hours | Email filtering |
| `[claude]` | collector/classifier/sweep/dayblock _budget + _model | Per-layer budget caps and model selection |
| `[schedule]` | collector_time | launchd trigger time |
| `[classification]` | force_yours, force_dispatch | Keyword-based classification overrides |
| `[dayblock]` | deep_work/content/admin times, gym_days/time/duration | Time block preferences |
| `[health]` | projects (map of name -> script path) | Health monitoring scripts |
| `[miniflux]` | base_url, api_token, max_entries, lookback_hours, mark_read | RSS reader connection |
| `[agents]` | content_write_folders, calendar_write_folders, max_workers | Vault write permissions + concurrency limit |
| `[agents.*]` | budget, model, timeout | Per-agent Claude budget ($), model, timeout (seconds) |
| `[cloudflare]` | api_token, account_id, workers, pages | Cloudflare API for Workers analytics + Pages deployments |
| `[coolify]` | base_url, api_token, exclude | Coolify API for app/service/database monitoring |
| `[healthchecks]` | pipeline_url, feed_url, sweep_url, weekly_url | Healthchecks.io ping URLs for monitoring |
| `[radar]` | pending_json | Path to Opportunity Radar export |
| `[code_review]` | reports_dir | Path to daily-code-review DIGEST.md output |

### Environment Differences

| Context | Database | Vault | Claude |
|---------|----------|-------|--------|
| Production | `cos.db` in cos_dir | Real Obsidian vault | Real MCP + budget |
| Test | In-memory SQLite | Temp directory | No Claude calls |

## Security

- Budget caps per Claude CLI invocation prevent runaway costs
- Mutex lock (`shlock`) prevents parallel pipeline runs
- Email agent creates Gmail **drafts** only, never sends
- Calendar agent reads only, never modifies events
- Content agent writes only to configured Obsidian folders
- `INSERT OR IGNORE` on unique constraints ensures idempotency
- No HTTP server, no network exposure, no auth needed
- Config file with secrets is gitignored
- Content hash (SHA256) prevents duplicate processing

## Constraints & Trade-offs

| Decision | Reason | Trade-off | Rejected Alternative |
|----------|--------|-----------|---------------------|
| SQLite over Postgres | Zero ops, single-machine, no network | No concurrent writes, no remote access | Postgres (overkill for single user) |
| Claude CLI over API | Inherits MCP connectors (Gmail, Calendar) | Subprocess overhead, text parsing | Direct API calls (no MCP access) |
| Prompt files over code | Non-dev can edit classification rules | No type safety, harder testing | Hardcoded Python logic |
| Flat work_queue | Single pipeline hub, simple status tracking | JOIN-heavy queries for domain details | Separate queues per domain type |
| WAL mode | Safe concurrent reads during writes | Slightly more disk usage | Default journal mode |
| Overnight batch | Predictable costs, no real-time pressure | Stale data until next run | Webhook/streaming (complex, costly) |
| launchd over cron | Native macOS, survives sleep/wake | macOS-only, `.plist` XML format | cron (simpler but less reliable on macOS) |
| Ollama over API embeddings | Zero per-call cost, no data egress | Depends on Mac mini + Tailscale uptime | Voyage / OpenAI embeddings (cost + privacy) |
| Centroid taste filter over kNN / LLM | O(1) scoring, works from few hundred labels | Can't model multi-modal taste cleanly | kNN (per-query cost), Sonnet-per-item ($50+/mo) |
| E5 `query:` prefix + `#vN` model id | Matches model card usage; safe re-embedding on model upgrade | Extra string bookkeeping | Bare model name (breaks on upgrade) |

## Known Tech Debt

### High Priority

### Medium Priority
- Day Block layer (Layer 3) not implemented
- Pre-existing test failures: 8 tests in renderer + task_collector need fixes
- No prompt version tracking (should use git short hash)
- Gmail/Calendar collectors depend on Claude MCP intermediate JSON files (fragile)
- No retry logic for failed Claude CLI calls

### Low Priority
- No TypeScript/JavaScript static analysis in health checks
- No historical trend tracking for classifications
- Config validation limited to `setup_wizard.py --validate` (no schema enforcement at runtime)
- Cleanup only purges done/skipped/failed, not stale pending items
- No diff-based review for health checks (re-processes unchanged data)
- Taste thresholds are hardcoded constants in `cos/taste.py`; should live in `config.toml [taste]` section
- Renderer does not yet consume `taste_scores.bucket`; daily brief surfaces all feeds equally rather than only `high_keep`
- Feed-level audit is a manual script; should auto-warn in weekly digest when a feed stays pure-noise for N weeks

## Code Hotspots

| File | Changes | Risk | Why |
|------|---------|------|-----|
| `cos/db.py` | High | Medium | All data access, schema changes ripple here |
| `schema.sql` | High | High | Any column/view change affects all queries |
| `renderer.py` | High | Low | Output formatting, additive changes only |
| `run.sh` | High | Medium | Pipeline orchestration, step ordering |
| `prompts/classifier.md` | Medium | High | Classification quality depends on prompt wording |

---

## Optional Modules

### Background Jobs

| Job | Trigger | Purpose | Retry | Timeout |
|-----|---------|---------|-------|---------|
| Full pipeline | launchd, daily at configured time | Collect + classify + render (sweep excluded, manual trigger) | No auto-retry, logs to `logs/` | Per-step Claude budget cap |
| Collection | Manual via `./run.sh collect` | Data gathering only | Source failure isolated, continues with others | Budget cap per source |
| Classification | Manual or part of full pipeline | Classify pending items | Idempotent, safe to re-run | Classifier budget cap |
| Morning Sweep | Manual or part of full pipeline | Execute dispatch/prep items | Failed actions logged, item stays in queue | Sweep budget cap |
| Cleanup | Manual via `./run.sh cleanup [days]` | Purge old records | CASCADE deletes related records | N/A (fast SQL) |

**Schedule**: `com.chief-of-staff.overnight.plist` (launchd)
- Runs `./run.sh full` daily
- Requires Mac to be awake at trigger time
- Mutex lock prevents overlapping runs
