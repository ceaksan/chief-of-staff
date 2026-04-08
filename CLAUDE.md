# Project: Chief of Staff

Local-first AI assistant for solo entrepreneurs. Collects info overnight (Gmail, Calendar, RSS, infra health), classifies tasks, dispatches AI agents, renders morning brief to Obsidian Daily Notes.

## Stack

- **Language**: Python 3.11+
- **Database**: SQLite (WAL mode, `cos.db`)
- **Scheduler**: macOS launchd (`com.chief-of-staff.overnight.plist`)
- **AI Runtime**: Claude Code CLI (Sonnet for collectors/classifiers, top-tier model for sweep)
- **MCP Connectors**: Gmail, Google Calendar, Miniflux
- **Output**: Obsidian Daily Notes (via `renderer.py`)
- **Config**: TOML (`config.toml`, template: `config.example.toml`)
- **Monitoring**: healthchecks.io (optional)

## Key Paths

- CLI (pipeline + brief): `cos-brief.sh`
- Collectors: `collectors/`
- System prompts: `prompts/` (collect, classifier, sweep, brief, agents/)
- Core library: `cos/` (config, db, log)
- Renderer: `renderer.py`
- Schema: `schema.sql` (9 tables, 5 views)
- Setup: `setup_wizard.py`
- Architecture: `architecture.md`

## Commands

```bash
# Full pipeline
./cos-brief.sh run

# Individual steps
./cos-brief.sh run collect
./cos-brief.sh run classify
./cos-brief.sh run sweep
./cos-brief.sh run render

# Morning brief
./cos-brief.sh
./cos-brief.sh status
./cos-brief.sh weekly
```

## Rules

- This is a local-only system. No cloud deployment, no HTTP server.
- SQLite is the only database. Do not suggest PostgreSQL migration.
- Budget limits are configured per-layer in `config.toml [claude]`. Respect them.
- All prompts live in `prompts/`. Edit prompts there, not inline in Python.
- Collector outputs go to SQLite `items` table. Renderer reads from views.
- Tests: `python -m pytest tests/`
- Lint: `ruff check .`

## Infrastructure

| Component | Details |
|-----------|---------|
| Database | SQLite WAL, local file `cos.db` |
| Scheduler | macOS launchd (daily at configured time) |
| External APIs | Gmail MCP, Google Calendar MCP, Miniflux REST, Cloudflare API, Coolify API |
| Output | Obsidian vault Daily Notes |
