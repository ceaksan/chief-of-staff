# Chief of Staff

A local-first AI assistant system that automates daily operational overhead for solo entrepreneurs. Built with Claude Code, Python, and SQLite.

Chief of Staff collects information from your tools overnight, classifies your tasks, and dispatches AI agents to handle routine work, so you start each morning with decisions instead of assembly.

## How It Works

```
09:00  Claude collects data (MCP)  →  SQLite
09:02  Renderer creates briefing   →  Obsidian Daily Note
09:04  Classifier sorts tasks      →  dispatch / prep / yours / skip
08:00  You review, approve, go     →  Subagents execute in parallel
08:05  Day Block plans your time   →  "AI Plan" calendar
```

Three layers, each independent. Build one at a time. Each layer adds value on its own.

## Architecture

```
              Claude Code (claude -p)
              ┌──────────────────────────────────┐
              │  MCP Tools (first-party)         │
              │  ┌──────────┐  ┌──────────────┐  │
              │  │  Gmail   │  │  Google       │  │
              │  │  MCP     │  │  Calendar MCP │  │
              │  └────┬─────┘  └──────┬────────┘  │
              └───────┼───────────────┼───────────┘
                      │               │
    ┌─────────────────┼───────────────┼──────────────────┐
    │                 ▼               ▼                   │
    │  ┌──────────────────────────────────────────────┐  │
    │  │              cos.db (SQLite)                  │  │
    │  │              Single source of truth           │  │
    │  └──────────────────────┬───────────────────────┘  │
    │                         │                          │
    │  ┌──────────┐  ┌───────▼────────┐  ┌───────────┐  │
    │  │  Health  │  │   Renderer     │  │  Task     │  │
    │  │Collector │  │  SQLite → MD   │  │ Collector │  │
    │  │ (Python) │  │  (Python)      │  │ (Python)  │  │
    │  └──────────┘  └───────┬────────┘  └───────────┘  │
    └────────────────────────┼───────────────────────────┘
                             │
                  ┌──────────▼──────────┐
                  │ Classifier (Sonnet) │  Overnight task sorting
                  └──────────┬──────────┘
                             │
                  ┌──────────▼──────────┐
                  │  Morning Sweep      │  Review → approve → agents
                  │  (Opus + subagents) │
                  └──────────┬──────────┘
                             │
                  ┌──────────▼──────────┐
                  │  Day Block (Sonnet) │  Time-blocked calendar
                  └─────────────────────┘
```

### Key Design Decisions

- **MCP-first for Google services**: Gmail and Calendar data collected via Claude's first-party MCP connectors. No custom OAuth setup, no Google Cloud Console, no API key management. Claude authenticates directly.
- **SQLite as intermediate layer**: All collected data goes to SQLite. A separate renderer generates Obsidian markdown. Obsidian is the view layer, not the database.
- **Overnight classification**: Tasks are sorted while you sleep. No waiting for Opus when you sit down in the morning.
- **Separate AI calendar**: Time blocks go to a dedicated "AI Plan" Google Calendar, not your main calendar. Overlay it visually, toggle it off when you want.
- **Local-first**: Runs on your Mac via launchd. No cloud dependency for orchestration. Migration to a server is possible but not required.
- **Semi-autonomous**: Creates drafts, tasks, and schedules automatically. Never sends emails. Critical decisions stay human.

## Data Sources

### Via MCP (Claude's built-in connectors)

| Source | MCP Server | What it captures |
|--------|-----------|-----------------|
| Gmail | `claude.ai Gmail` | Actionable emails, Zoho ticket notifications. Priority P1-P4, duration estimate, sender. |
| Google Calendar | `claude.ai Google Calendar` | Today + tomorrow events across all calendars, Calendly slots flagged for prep, free blocks. |

MCP handles authentication. Connect once via Claude Code (`/mcp`), no API keys to manage.

### Via Python (custom collectors)

| Source | Script | What it captures |
|--------|--------|-----------------|
| Feeds | `collectors/feed_collector.py` | Unread RSS/Atom entries from Miniflux (self-hosted). Fetches via REST API, no MCP needed. |
| Health | `collectors/health_collector.py` | Per-project status from existing monitoring scripts: up/down, error count, last deploy. |
| Tasks | `collectors/task_collector.py` | Open tasks from Obsidian vault (Dataview checkbox format), synced to SQLite. |

### Multi-Calendar Support

All calendars accessible through a single MCP connection via calendar sharing:

| Calendar | Access | Purpose |
|----------|--------|---------|
| `user@example.com` | owner (primary) | Personal |
| `shared-reader@example.com` | reader | Project A |
| `other-reader@example.com` | reader | Project B |
| `freebusy-reader@example.com` | freeBusyReader | Project C |
| Luma | reader | Events |

## Layers

### Layer 1: Overnight Collection

Runs at 06:00 via launchd. A single `claude -p` session with MCP access collects Gmail and Calendar data, writes to `cos.db`. Then Python scripts collect Health and Task data.

```bash
# Overnight pipeline (or manual: ./run.sh)
claude -p prompts/collect.md --budget 2.00    # Gmail + Calendar via MCP → cos.db
python collectors/feed_collector.py            # Miniflux feeds → cos.db
python collectors/health_collector.py          # Health scripts → cos.db
python collectors/task_collector.py            # Obsidian tasks → cos.db
python renderer.py                             # cos.db → Obsidian Daily Note
```

**Failure handling**: If a source fails, others continue. The Daily Note shows a warning for the failed source.

### Layer 1.5: Overnight Classifier

Runs after collection. Uses Claude Sonnet (`claude -p`, non-interactive) with a $1.50 budget cap.

Reads pending items from `cos.db` and classifies each:

| Class | Meaning | Example |
|-------|---------|---------|
| **DISPATCH** | AI handles fully | Meeting confirmation reply, research task, note update |
| **PREP** | AI does 80%, you finish | Complex email draft, error investigation summary + fix direction |
| **YOURS** | Needs your brain | Strategy decisions, pricing, live meetings |
| **SKIP** | Not today | Low priority, blocked, deadline far away |

Classification is written back to `cos.db` and rendered into the Daily Note. When you wake up, the sorted plan is already waiting.

### Layer 2: Morning Sweep

On-demand. You trigger it when ready. Uses Claude Opus with a $3.00 budget cap.

1. Reads the classified Daily Note
2. Shows the plan, you approve or adjust
3. Fires subagents in parallel for approved DISPATCH + PREP tasks

| Agent | Scope | Safety |
|-------|-------|--------|
| **Email Agent** | Creates Gmail drafts via MCP | Never sends |
| **Dev Prep Agent** | Error log summary + fix direction (no code) | Read-only |
| **Content Agent** | Blog post drafts, research notes | Writes to specific Obsidian folders only |
| **Calendar Agent** | Meeting prep notes (client context, agenda) | Read-only |

Completion report appends to the Daily Note. Task statuses update in `cos.db`.

### Layer 3: Day Block

On-demand. Triggered after the Morning Sweep. Uses Claude Sonnet.

Takes remaining YOURS + PREP tasks and fits them into calendar free blocks:

- Calendly slots are untouchable
- Dev work in the morning (deep work)
- Content in the afternoon
- Email/admin in gaps and end of day
- P1 tasks always first

Writes `[TB]` prefixed events to a dedicated **"AI Plan"** Google Calendar via MCP. Supports `--dry-run` for preview before writing.

## Daily Note Output

The overnight process produces a ready-to-review briefing:

```markdown
# 2026-03-07

## Calendar
- 10:00-11:00 Client X meeting (Calendly) - prep needed
- 14:00-14:30 Deploy review
- Free: 07:00-10:00, 11:00-14:00, 14:30-18:00

## Project Status
- OK: project-a, project-b, project-c
- project-d: 3 errors (connection timeout)

## Classified Tasks

### DISPATCH (AI handles)
- [ ] Client A email reply - meeting confirmation (#email)
- [ ] Blog post research - framework migration (#content)

### PREP (80% ready, you finish)
- [ ] Client Y hosting migration reply - draft ready (#email)
- [ ] project-d timeout - summary + fix direction (#dev)

### YOURS (your brain needed)
- [ ] Client X meeting prep
- [ ] project-e checkout flow fix (#dev)

### SKIP (not today)
- [ ] project-d onboarding wizard - P3, deadline far

## Carried Over
- [ ] [P2] Blog post publish - pending 2 days
```

## Project Structure

```
~/.chief-of-staff/
├── cos.db                      # SQLite database
├── config.toml                 # Paths, rules, schedule
├── lockfile                    # Mutex for collectors
├── logs/                       # Structured JSON logs
│   └── YYYY-MM-DD.json
├── collectors/
│   ├── health_collector.py     # Calls existing monitoring scripts
│   └── task_collector.py       # Greps Obsidian vault for open tasks
├── renderer.py                 # SQLite → Obsidian Daily Note
├── prompts/
│   ├── collect.md              # MCP collection prompt (Gmail + Calendar)
│   ├── classifier.md           # Sonnet classification prompt
│   ├── sweep.md                # Opus sweep prompt
│   └── dayblock.md             # Sonnet day block prompt
└── agents/
    ├── email_agent.md          # Subagent instructions
    ├── dev_agent.md
    ├── content_agent.md
    └── calendar_agent.md
```

## Setup

### Prerequisites

- macOS (launchd for scheduling)
- Python 3.11+
- Claude Code with Max/Pro subscription
- Claude Code MCP connectors authenticated:
  - `claude.ai Gmail` (connect via `/mcp` in Claude Code)
  - `claude.ai Google Calendar` (connect via `/mcp` in Claude Code)
- Obsidian vault with Dataview plugin
- Existing project health monitoring scripts (optional, for Health Collector)

### Installation

```bash
git clone https://github.com/ceaksan/chief-of-staff.git ~/.chief-of-staff
cd ~/.chief-of-staff
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml
# Edit config.toml with your paths and rules
```

### MCP Authentication

No API keys or Google Cloud Console needed. Claude Code handles OAuth via its built-in MCP connectors.

```bash
# In Claude Code, authenticate both services:
/mcp
# Select "claude.ai Gmail" → Authenticate
# Select "claude.ai Google Calendar" → Authenticate

# Verify:
# Gmail: gmail_get_profile should return your email
# Calendar: gcal_list_calendars should show your calendars
```

For multi-calendar access, share other Google account calendars to your primary account (Google Calendar > Settings > Share with specific people > your primary email).

### Configuration

Edit `config.toml`:

```toml
[paths]
obsidian_vault = "/path/to/your/vault"
daily_notes_dir = "Daily"
health_scripts_dir = "/path/to/your/monitoring/scripts"

[calendars]
# Calendar IDs to scan (from gcal_list_calendars output)
ids = [
    "primary",
    "shared-reader@example.com",
    "other-reader@example.com",
]
ai_plan_calendar_id = ""  # created during setup

[claude]
collector_budget = 2.00
classifier_budget = 1.50
sweep_budget = 3.00
dayblock_budget = 1.00

[schedule]
collector_time = "06:00"

[classification]
force_yours = ["pricing", "strategy", "contract"]
force_dispatch = ["meeting confirmation", "calendar update"]
```

### Architecture Documentation (optional)

Generate a structured architecture document for AI tools (Claude, Gemini, Cursor) using the [Living Architecture](https://github.com/ceaksan/living-architecture) template:

```bash
curl -sL https://raw.githubusercontent.com/ceaksan/living-architecture/main/TEMPLATE.md -o architecture.md
# Fill sections based on your project. See DEPTH_GUIDE.md for L1/L2/L3 detail levels.
```

The `architecture.md` file is gitignored. Each user generates their own based on their deployment.

### Code Health Integration (optional)

Chief of Staff can include daily code audit results in the Daily Note. This requires [daily-code-review](https://github.com/ceaksan/daily-code-review) (dnm-audit) to be set up and scheduled separately.

1. Install and configure daily-code-review with its own schedule (cron/launchd)
2. Add the reports directory to `config.toml`:

```toml
[code_review]
reports_dir = "/path/to/code-review-reports"
```

The renderer reads `{reports_dir}/{YYYY-MM-DD}/DIGEST.md` and adds a **Code Health** section to the Daily Note. If no report exists for the day (weekends, holidays), the section is silently omitted.

### Manual Usage

```bash
cd /path/to/chief-of-staff

./run.sh              # full pipeline (collect + classify + sweep + render)
./run.sh collect      # collection only (Gmail, Calendar, Feeds, Tasks, Health)
./run.sh classify     # classification only
./run.sh sweep        # morning sweep only
./run.sh render       # re-render daily note only
./run.sh status       # show pipeline status
```

### Schedule Setup (optional)

LaunchAgent runs the full pipeline at 09:00. Requires Mac to be awake.

```bash
cp com.chief-of-staff.overnight.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chief-of-staff.overnight.plist
```

### Shell Aliases

Add to your `.zshrc`:

```bash
alias cos-collect="claude -p ~/.chief-of-staff/prompts/collect.md --budget 2.00"
alias cos-health="source ~/.chief-of-staff/.venv/bin/activate && python ~/.chief-of-staff/collectors/health_collector.py"
alias cos-tasks="source ~/.chief-of-staff/.venv/bin/activate && python ~/.chief-of-staff/collectors/task_collector.py"
alias cos-render="source ~/.chief-of-staff/.venv/bin/activate && python ~/.chief-of-staff/renderer.py"
alias cos-classify="claude -p ~/.chief-of-staff/prompts/classifier.md --budget 1.50"
alias cos-sweep="claude -p ~/.chief-of-staff/prompts/sweep.md --budget 3.00"
alias cos-dayblock="claude -p ~/.chief-of-staff/prompts/dayblock.md --budget 1.00"
```

## Build Order

Build incrementally. Each phase adds standalone value.

| Phase | What | Needs |
|-------|------|-------|
| 1 | SQLite schema + Calendar collection (MCP) + Renderer | Claude (Sonnet) |
| 2 | Task Collector (Obsidian grep) | Python only |
| 3 | Gmail collection (MCP) | Claude (Sonnet) |
| 4 | Health Collector (integrate existing scripts) | Python only |
| 5 | Overnight Classifier | Claude (Sonnet) |
| 6 | Morning Sweep + subagents | Claude (Opus) |
| 7 | Day Block + AI Plan calendar | Claude (Sonnet) |

## SQLite Schema

```sql
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,          -- gmail, health, calendar, task
    type TEXT,                     -- email, ticket, error, event, slot, task
    payload JSON NOT NULL,
    priority TEXT,                 -- P1, P2, P3, P4
    classification TEXT,           -- dispatch, prep, yours, skip
    status TEXT DEFAULT 'pending', -- pending, processed, done, skipped
    collected_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE UNIQUE INDEX idx_source_id ON items(source, id);

CREATE VIEW rolling_tasks AS
    SELECT * FROM items
    WHERE status IN ('pending', 'processed')
    AND collected_at >= date('now', '-3 days');
```

## Safety Model

| Rule | Implementation |
|------|---------------|
| Never send emails | Email Agent creates drafts only via MCP `gmail_create_draft`. |
| Budget caps | Each Claude invocation has a `--budget` flag. |
| Mutex | `flock` lockfile prevents parallel runs. |
| Idempotency | `INSERT OR IGNORE` on unique source+id index. |
| Dry run | `--dry-run` flag on Day Block previews without writing. |
| Failure isolation | Source failure doesn't block others. Warning in Daily Note. |
| Human approval | Morning Sweep shows classification before dispatching agents. |
| Scoped writes | Content Agent writes to specific vault folders only. |

## Cost

| Component | Cost |
|-----------|------|
| Claude Max subscription | $100/month (required) |
| Overnight Collection + Classifier (Sonnet) | ~$1.00-3.00/day |
| Morning Sweep (Opus) | ~$1.00-3.00/day |
| Day Block (Sonnet) | ~$0.25-1.00/day |
| Google APIs | Free (MCP handles auth) |
| **Total beyond subscription** | **~$5-15/month** |

## Inspiration

This project was inspired by [Jim Prosser's Claude Code Chief of Staff](https://github.com/jimprosser/claude-code-cos) system. Key differences:

- **MCP-first**: Gmail and Calendar via Claude's built-in MCP connectors, no custom OAuth
- **SQLite intermediate layer** instead of direct file manipulation (addresses parsing fragility)
- **Overnight classification** instead of interactive morning wait
- **Separate AI calendar** instead of mixing time blocks with real events
- **Obsidian as view layer** instead of read-write database
- **Health monitoring** integrated (development workflow support)
- **Local-first** with optional server migration path

## Roadmap

- [ ] Phase 1-4: Core collectors and renderer
- [ ] Phase 5: Overnight classifier
- [ ] Phase 6: Morning Sweep with subagents
- [ ] Phase 7: Day Block with AI Plan calendar
- [ ] Hetzner migration option (Docker + sync)
- [ ] Rolling context (multi-day task tracking)
- [ ] Feedback loop (learn from email draft rewrites)
- [ ] Weekly review summary

## License

MIT
