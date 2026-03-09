# Chief of Staff - System Design

**Date**: 2026-03-07
**Status**: Approved
**Author**: User + Claude

## Problem Statement

Solo entrepreneurs spend 30-45 minutes every morning on operational overhead: email triage, task organization, calendar review, project status checks. This is necessary but not valuable work. The goal is to reduce this to single-digit minutes by automating information gathering, task classification, and routine execution.

## Context

### User Profile
- Solo entrepreneur: consulting + software development + content creation
- 15-30 actionable emails/day
- Multiple active projects across Vercel, Cloudflare, Hetzner/Coolify, Railway, Neon
- Consulting clients via Calendly, Slack (multi-workspace, client-owned), Zoho Desk (tickets)
- Obsidian vault for knowledge management and task tracking (Dataview + checkbox format)
- Existing Python health monitoring scripts for projects
- basic-memory MCP connected to Obsidian vault
- Claude Code MCP connectors: Gmail + Google Calendar (authenticated, tested)
- Infrastructure: Mac (always-on) + Hetzner server

### Google Accounts (verified)
- **Primary**: `user@example.com` (Gmail + Calendar owner)
- **Shared calendars**: `secondary@example.com` (reader), `other@example.com` (reader), `freebusy@example.com` (freeBusyReader), Luma (reader)
- All calendars accessible via single MCP connection through calendar sharing

### Design Inputs
- Jim Prosser's Claude Code Chief of Staff system (4-layer architecture)
- Claude Code Agent Teams documentation (experimental, not used - subagents sufficient)
- Daniel Miessler's Personal AI Infrastructure (TELOS concept, over-engineered for solo use)
- Critical review from Gemini Pro and Kimi K2.5

## Design Decisions

### 1. MCP-First for Google Services

**Decision**: Use Claude's first-party MCP connectors for Gmail and Calendar instead of custom Python collectors with direct API calls.

**Why**: MCP connectors are already authenticated and tested. No Google Cloud Console project needed, no OAuth credentials to manage, no API client libraries to maintain. Claude reads Gmail and Calendar data directly via MCP tools (`gmail_search_messages`, `gcal_list_events`, etc.) during `claude -p` sessions.

**What changes**:
- No `gmail_collector.py` or `calendar_collector.py` needed
- No `google-api-python-client` dependency
- Collection prompt (`prompts/collect.md`) instructs Claude to use MCP tools and write results to SQLite
- Email Agent in Morning Sweep can use `gmail_create_draft` directly via MCP
- Day Block can use `gcal_create_event` directly via MCP

**Trade-off**: Collection now requires Claude (Sonnet) instead of pure Python. Adds ~$0.50-1.00/day cost. Worth it for massive reduction in setup complexity and code to maintain.

### 2. Obsidian as View Layer, Not Database

**Decision**: SQLite stores all data. Obsidian renders it.

**Why**: Both Gemini and Kimi identified the "Golden File" anti-pattern. Writing to markdown from multiple parallel processes causes race conditions, file lock issues, and parsing fragility. A Dataview cache miss means the AI silently ignores tasks.

**Trade-off**: Extra complexity (SQLite + renderer) vs. reliability. Worth it.

### 3. Overnight Classification (Layer 1.5)

**Decision**: Classify tasks overnight with Sonnet, not interactively with Opus in the morning.

**Why**: Gemini identified the latency problem. Sitting at your desk waiting 2+ minutes for Opus to classify is bad UX. With overnight classification, the sorted plan is ready when you wake up.

**Trade-off**: Classification might be slightly less accurate without morning context (e.g., you woke up sick). Mitigation: Morning Sweep still allows adjustments before dispatch.

### 4. Separate AI Plan Calendar

**Decision**: Time blocks go to a dedicated Google Calendar, not the main one.

**Why**: No two-way sync risk. If you drag a [TB] block in GCal, the system doesn't know. A separate calendar can be toggled on/off as an overlay.

### 5. Slack and Zoho Excluded from Automation

**Decision**: No Slack or Zoho API integration.

**Why**:
- Slack: Multi-workspace, client-owned. Would need bot installation per workspace. Not practical.
- Zoho Desk: Ticket notifications already come to Gmail. Captured via Gmail MCP. Direct Zoho API is a future option.

### 6. Local-First Architecture

**Decision**: Mac launchd for orchestration. Hetzner as future migration path, not current dependency.

**Why**: MCP connectors work locally. No network dependency for core workflow beyond API calls. Mac is always-on.

**Migration path**: When moving to Hetzner, MCP connectors may need headless auth or switch to direct API calls. This is a future concern.

### 7. Subagents, Not Agent Teams

**Decision**: Use Claude Code subagents (Agent tool), not the experimental Agent Teams feature.

**Why**:
- Agent Teams are experimental and disabled by default
- Split panes don't work in Ghostty
- Agents don't need to communicate with each other
- Subagents are cheaper (results summarized back to main context)
- Each agent does independent work: email drafts, dev prep, content, calendar

### 8. Dev Prep Agent: Summarize + Fix Direction

**Decision**: Error log summary + fix direction suggestions. No code generation.

**Why**: Unattended code suggestions without a sandbox risk hallucination. "The error is a Neon connection timeout, likely caused by connection pool exhaustion. Fix direction: increase pool size in DATABASE_URL or add connection retry logic" is more useful than a possibly wrong code patch.

## Rejected Alternatives

| Alternative | Why Rejected |
|-------------|-------------|
| Workflow engine (Prefect/Dagster/n8n) | Overkill for solo use. Cron + lockfile sufficient. |
| Full event sourcing | SQLite `INSERT OR IGNORE` provides idempotency. Full event store unnecessary. |
| Schema versioning (v1/v2) | Single developer. Breaking changes are self-to-self. |
| Custom Python Gmail/Calendar collectors | MCP connectors eliminate OAuth setup and API library maintenance. |
| Todoist/TickTick migration | Obsidian tasks already work. File-based is fine with SQLite as structured layer. |
| Agent Teams | Experimental, Ghostty incompatible, agents don't need inter-communication. |

## Data Flow

```
Claude (claude -p) with MCP
  │
  ├─ gmail_search_messages → email items
  ├─ gcal_list_events      → calendar items
  │
  └──→ cos.db (SQLite) ←── health_collector.py
              │         ←── task_collector.py
              │
       Classifier (Sonnet, claude -p)
              │
       classification column updated
              │
       Renderer (Python)
              │
       Obsidian Daily Note
              │
       Morning Sweep (Opus, on-demand, claude -p)
         │  │  │  │
         ▼  ▼  ▼  ▼
       Email  Dev  Content  Calendar  (subagents)
         │     │      │        │
         ▼     ▼      ▼        ▼
       Gmail  Obsidian vault   Obsidian vault
       Drafts (via MCP)
              │
       Day Block (Sonnet, on-demand)
              │
       "AI Plan" Google Calendar (via MCP)
```

## SQLite Schema

```sql
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    type TEXT,
    payload JSON NOT NULL,
    priority TEXT,
    classification TEXT,
    status TEXT DEFAULT 'pending',
    collected_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE UNIQUE INDEX idx_source_id ON items(source, id);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    error TEXT,
    items_processed INTEGER DEFAULT 0
);

CREATE VIEW rolling_tasks AS
    SELECT * FROM items
    WHERE status IN ('pending', 'processed')
    AND collected_at >= date('now', '-3 days');

CREATE VIEW today_items AS
    SELECT * FROM items
    WHERE date(collected_at) = date('now');
```

### Payload Examples

**Gmail item** (collected via MCP `gmail_search_messages` + `gmail_read_message`):
```json
{
    "message_id": "18e1a2b3c4d5e6f7",
    "thread_id": "18e1a2b3c4d5e6f7",
    "subject": "Re: Hosting migration timeline",
    "from": "client@example.com",
    "snippet": "Can we move the migration to next week?",
    "action": "reply",
    "estimated_duration": 10,
    "labels": ["client", "hosting"]
}
```

**Calendar item** (collected via MCP `gcal_list_events`):
```json
{
    "event_id": "abc123def456",
    "calendar_id": "user@example.com",
    "summary": "Client X Consultation",
    "start": "2026-03-07T10:00:00+03:00",
    "end": "2026-03-07T11:00:00+03:00",
    "location": "Google Meet",
    "is_calendly": true,
    "prep_needed": true
}
```

**Health item**:
```json
{
    "project": "validough",
    "status": "warning",
    "uptime": 99.2,
    "errors_24h": 3,
    "last_error": "Neon connection timeout at 03:42",
    "last_deploy": "2026-03-06T14:30:00Z"
}
```

## Classification Rules

### Priority Framework
- **P1**: Hard consequence if missed (revenue, legal, client relationship, production down)
- **P2**: Time-sensitive with compounding delay cost
- **P3**: Important but flexible timing
- **P4**: Low urgency, do when space available

### Classification Framework
- **DISPATCH**: No ambiguity, Claude can complete fully. Meeting confirmations, research tasks, note updates, simple email replies.
- **PREP**: Needs human judgment for final step. Complex email drafts (tone-sensitive), error investigations (fix direction but not implementation), content outlines.
- **YOURS**: Requires human presence, judgment, or creativity. Live meetings, pricing decisions, strategic communications, active development work.
- **SKIP**: Not actionable today. Low priority, blocked by dependency, deadline > 5 days away.

### Override Rules (config.toml)
- `force_yours`: keywords that always classify as YOURS (pricing, strategy, contract)
- `force_dispatch`: keywords that always classify as DISPATCH (meeting confirmation, calendar update)

## Agent Specifications

### Email Agent
- **Input**: Classified email items from cos.db
- **Output**: Gmail drafts via MCP `gmail_create_draft` (never sends)
- **Context**: Sender history (via MCP `gmail_read_thread`), tone guidelines
- **Safety**: MCP tool only supports draft creation, no send capability in this context

### Dev Prep Agent
- **Input**: Health collector warnings, GitHub notifications
- **Output**: Obsidian note with error summary + fix direction
- **Scope**: Read-only. Summarize the issue context, suggest fix direction, list affected files. No code generation.
- **Safety**: No write access to code repositories

### Content Agent
- **Input**: Content tasks from Obsidian
- **Output**: Draft posts, research notes in designated vault folders
- **Scope**: Write only to `Content/Drafts/` in vault
- **Safety**: Cannot modify existing published content

### Calendar Agent
- **Input**: Calendly events flagged for prep
- **Output**: Meeting prep note in Obsidian (client context from basic-memory, last interaction, suggested agenda)
- **Scope**: Read-only on calendar (via MCP), read basic-memory, write to `Daily/` folder
- **Safety**: Cannot create or modify calendar events

## Failure Modes

| Failure | Impact | Mitigation |
|---------|--------|------------|
| MCP auth expired | No Gmail/Calendar data | Warning in Daily Note, manual re-auth via `/mcp` |
| Health script fails | No project status | Warning, other sources continue |
| Mac sleeps during collection | Delayed or missed run | caffeinate wrapper, launchd KeepAlive |
| Budget cap hit mid-collection | Partial data | Unprocessed sources flagged in Daily Note |
| Obsidian vault locked (sync) | Renderer can't write | Retry with backoff, max 3 attempts |
| SQLite locked | Collector can't write | flock mutex prevents parallel access |

## Build Phases

### Phase 1: Calendar Collection + Renderer
- SQLite schema creation (`schema.sql`)
- Collection prompt: Claude uses `gcal_list_events` MCP tool across all calendars → writes to cos.db
- Renderer: cos.db → Obsidian Daily Note
- launchd plist for scheduling
- **Deliverable**: Daily calendar briefing in Obsidian

### Phase 2: Task Collector
- Python script greps open tasks from Obsidian vault
- Parse Dataview checkbox format
- Insert to cos.db
- Renderer update: add Tasks section
- **Deliverable**: Tasks appear in Daily Note alongside calendar

### Phase 3: Gmail Collection
- Collection prompt update: Claude uses `gmail_search_messages` + `gmail_read_message` MCP tools
- Actionable item extraction, priority estimation (P1-P4), duration estimation
- Zoho ticket notification detection
- **Deliverable**: Email triage in Daily Note

### Phase 4: Health Collector
- Python integration wrapper for existing monitoring scripts
- Standardized output format
- Renderer update: add Project Status section
- **Deliverable**: Project health in Daily Note

### Phase 5: Overnight Classifier
- Classifier prompt (`prompts/classifier.md`)
- Claude Sonnet reads cos.db, classifies each pending item
- Classification → cos.db update
- Renderer update: grouped by classification
- **Deliverable**: Wake up to sorted task plan

### Phase 6: Morning Sweep + Subagents
- Sweep prompt (`prompts/sweep.md`)
- Interactive review and approval flow
- Email Agent: drafts via MCP `gmail_create_draft`
- Dev Prep Agent: error summary + fix direction to Obsidian
- Content Agent: drafts + research to Obsidian
- Calendar Agent: meeting prep notes to Obsidian
- Completion report
- **Deliverable**: Semi-autonomous task execution

### Phase 7: Day Block
- Day Block prompt (`prompts/dayblock.md`)
- "AI Plan" Google Calendar creation via MCP `gcal_create_event`
- Time-blocking rules engine
- Dry-run mode
- **Deliverable**: Automated day planning

## MCP Tools Reference

Tools available via Claude's first-party MCP connectors:

### Gmail MCP
| Tool | Used in | Purpose |
|------|---------|---------|
| `gmail_search_messages` | Collection | Search yesterday's actionable emails |
| `gmail_read_message` | Collection | Read full message content |
| `gmail_read_thread` | Sweep (Email Agent) | Thread context for draft replies |
| `gmail_create_draft` | Sweep (Email Agent) | Create reply drafts |
| `gmail_get_profile` | Setup verification | Confirm auth works |

### Google Calendar MCP
| Tool | Used in | Purpose |
|------|---------|---------|
| `gcal_list_calendars` | Setup verification | Discover calendar IDs |
| `gcal_list_events` | Collection | Fetch events across all calendars |
| `gcal_create_event` | Day Block | Create [TB] time blocks in AI Plan calendar |
| `gcal_find_my_free_time` | Day Block | Calculate available slots |

## Future Considerations

- **Hetzner migration**: Dockerize Python parts, handle MCP auth in headless mode (may need to switch Gmail/Calendar to direct API)
- **Rolling context**: Multi-day task tracking with escalation
- **Feedback loop**: Compare email drafts with final sent versions to learn tone
- **Weekly review**: Automated summary of completed/skipped/carried tasks
- **Zoho Desk API**: Direct integration when email parsing becomes fragile
- **Voice notifications**: macOS `say` or ntfy for critical alerts
