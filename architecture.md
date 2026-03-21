# Chief of Staff - Architecture

Local-first AI assistant that automates daily operational overhead for solo entrepreneurs.

<!--
Living Architecture Template v1.0
Source: https://github.com/ceaksan/living-architecture
Depth: L2
Last verified: 2026-03-21
-->

## Stack & Dependencies

### Runtime

| Package | Version | Purpose |
|---------|---------|---------|
| Python | 3.12+ | Core runtime |
| SQLite | 3.x (stdlib) | Local database, WAL mode |
| tomllib | stdlib (3.11+) | Config parsing |

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
│   └── log.py                   # Structured JSON logging (daily rotation)
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
│   └── sweep.py                 # Morning sweep dispatcher (export/record/complete CLI)
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
├── schema.sql                   # Full SQLite schema (9 tables, 5 views)
├── renderer.py                  # SQLite -> Obsidian Daily Note markdown
├── run.sh                       # Pipeline orchestrator (mutex, step routing)
├── cos-brief.sh                 # CLI brief generator
├── config.toml                  # User config (gitignored)
├── config.example.toml          # Config template
└── com.chief-of-staff.overnight.plist  # launchd schedule (gitignored)
```

## Data Flow

### Full Pipeline (collect -> classify -> sweep -> render)

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
    ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐
    │email ││cal   ││health││task  ││feed  │
    │agent ││agent ││agent ││agent ││agent │
    │(Opus)││(Son.)││(Son.)││(Son.)││(Son.)│
    └──┬───┘└──┬───┘└──┬───┘└──┬───┘└──┬───┘
       │       │       │       │       │
       └───────┴───────┴───────┴───────┘
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
- No config validation/schema beyond TOML parsing
- Cleanup only purges done/skipped/failed, not stale pending items
- No diff-based review for health checks (re-processes unchanged data)

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
| Full pipeline | launchd, daily at configured time | Collect + classify + sweep + render | No auto-retry, logs to `logs/` | Per-step Claude budget cap |
| Collection | Manual via `./run.sh collect` | Data gathering only | Source failure isolated, continues with others | Budget cap per source |
| Classification | Manual or part of full pipeline | Classify pending items | Idempotent, safe to re-run | Classifier budget cap |
| Morning Sweep | Manual or part of full pipeline | Execute dispatch/prep items | Failed actions logged, item stays in queue | Sweep budget cap |
| Cleanup | Manual via `./run.sh cleanup [days]` | Purge old records | CASCADE deletes related records | N/A (fast SQL) |

**Schedule**: `com.chief-of-staff.overnight.plist` (launchd)
- Runs `./run.sh full` daily
- Requires Mac to be awake at trigger time
- Mutex lock prevents overlapping runs
