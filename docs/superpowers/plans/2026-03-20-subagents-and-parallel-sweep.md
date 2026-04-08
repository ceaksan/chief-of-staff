# Subagents & Parallel Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the monolithic sweep prompt into domain-specific subagent prompts and add a Python asyncio orchestrator for parallel execution with failure isolation, per-agent logging, and budget tracking.

**Architecture:** The current single `claude -p prompts/sweep.md` call becomes a Python orchestrator (`collectors/orchestrator.py`) that exports items from cos.db, groups them by domain_type, launches parallel `claude -p` subprocesses (one per agent), collects JSON results, and imports them back to cos.db in a single transaction. Each agent gets its own prompt in `prompts/agents/`, its own budget cap, its own timeout, and its own log file.

**Tech Stack:** Python 3.12+ asyncio, subprocess, existing cos.db + collectors/sweep.py infrastructure

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `prompts/agents/email-agent.md` | Email dispatch/prep: read threads, create drafts |
| Create | `prompts/agents/calendar-agent.md` | Calendar dispatch/prep: meeting prep notes |
| Create | `prompts/agents/health-agent.md` | Health dispatch/prep: status summaries, error analysis |
| Create | `prompts/agents/task-agent.md` | Task dispatch/prep: completion notes, research outlines |
| Create | `prompts/agents/feed-agent.md` | Feed dispatch/prep: actionable feed summaries |
| Create | `collectors/orchestrator.py` | Async orchestrator: export, parallel dispatch, import |
| Modify | `run.sh:77-84` | Replace `run_sweep()` to call orchestrator |
| Modify | `config.example.toml:88-93` | Add `[agents]` budget/model/timeout per agent |
| Create | `tests/test_orchestrator.py` | Orchestrator unit tests |

---

## Task 1: Create Agent Prompt Files

These are the subagent system prompts. Each follows the same contract: read items from stdin (JSON), process them, output actions JSON to stdout.

**Files:**
- Create: `prompts/agents/email-agent.md`
- Create: `prompts/agents/calendar-agent.md`
- Create: `prompts/agents/health-agent.md`
- Create: `prompts/agents/task-agent.md`
- Create: `prompts/agents/feed-agent.md`

### Shared Agent Contract

All agents receive JSON on stdin:
```json
{
  "items": [
    {
      "queue_id": 1,
      "domain_type": "email",
      "title": "Re: Project update",
      "context": "alice@example.com",
      "detail": "Latest sprint summary...",
      "extra": "thread_abc123",
      "priority": "P2",
      "category": "dispatch"
    }
  ],
  "today": "2026-03-20",
  "vault_path": "/path/to/vault"
}
```

All agents output JSON to stdout:
```json
[
  {
    "queue_id": 1,
    "agent": "email",
    "action_type": "draft_created",
    "external_ref": "draft_abc123",
    "output_summary": "Reply draft for project update",
    "status": "completed"
  }
]
```

- [ ] **Step 1: Create email-agent.md**

```markdown
# Email Agent

You process email items for a solo entrepreneur. For each item:

## Safety Rules
- NEVER send emails. Only create DRAFTS via `gmail_create_draft`.
- If unsure about tone or content, mark status as "needs_review".

## Instructions

You receive JSON on stdin with email items to process.

### For DISPATCH items:
1. Use `gmail_read_thread` with the `extra` field (thread_id) to get full context
2. Use `gmail_create_draft` to create an appropriate reply
3. Record action with agent="email", action_type="draft_created"

### For PREP items:
1. Use `gmail_read_thread` for full thread context
2. Use `gmail_create_draft` with a detailed reply draft
3. Mark areas needing human review with [REVIEW: ...]
4. Record action with agent="email", action_type="draft_created"

## Output
Print a JSON array of action records to stdout. Each record:
{queue_id, agent: "email", action_type, external_ref, output_summary, status}
```

- [ ] **Step 2: Create calendar-agent.md**

```markdown
# Calendar Agent

You process calendar event items for a solo entrepreneur.

## Safety Rules
- NEVER modify or delete calendar events.
- Only write notes to Daily/ folder in the vault.

## Instructions

### For DISPATCH items:
- Simple confirmations: acknowledge, record action
- Record: agent="calendar", action_type="acknowledged"

### For PREP items (prep_needed events):
1. Read event details (summary, attendees, time)
2. Write meeting prep note to vault Daily/ folder
3. Record: agent="calendar", action_type="note_written", external_ref=<note_path>

## Output
Print a JSON array of action records to stdout.
```

- [ ] **Step 3: Create health-agent.md**

```markdown
# Health Agent

You process project health check items.

## Safety Rules
- NEVER modify source code or deploy anything.
- Only write analysis notes to Daily/ folder.

## Instructions

### For DISPATCH items:
- Generate brief status summary
- Record: agent="dev_prep", action_type="summary_generated"

### For PREP items (warnings/errors):
1. Analyze error details from `detail` field
2. Write Obsidian note to Daily/ with: error summary, likely root cause, suggested fix direction
3. Record: agent="dev_prep", action_type="note_written", external_ref=<note_path>

## Output
Print a JSON array of action records to stdout.
```

- [ ] **Step 4: Create task-agent.md**

```markdown
# Task Agent

You process Obsidian task items.

## Safety Rules
- Only write to allowed vault folders: Daily/, Ideas/Digital/, Business/, Personal/

## Instructions

### For DISPATCH items:
- Simple update/note tasks: write completion note
- Record: agent="content", action_type="note_written"

### For PREP items:
- Research/outline to appropriate folder
- Record: agent="content", action_type="note_written", external_ref=<path>

## Output
Print a JSON array of action records to stdout.
```

- [ ] **Step 5: Create feed-agent.md**

```markdown
# Feed Agent

You process RSS feed items that passed classification.

## Safety Rules
- Only write to allowed vault folders: Ideas/Digital/, Business/

## Instructions

### For DISPATCH items:
- Write brief actionable summary note
- Record: agent="content", action_type="summary_generated", external_ref=<feed_url>

### For PREP items:
- Write detailed analysis note with action items
- Record: agent="content", action_type="note_written", external_ref=<note_path>

## Output
Print a JSON array of action records to stdout.
```

- [ ] **Step 6: Verify all prompt files exist**

Run: `ls -la prompts/agents/`
Expected: 5 .md files

- [ ] **Step 7: Commit**

```bash
git add prompts/agents/
git commit -m "feat: add domain-specific subagent prompt files"
```

---

## Task 2: Add Agent Configuration to config.toml

**Files:**
- Modify: `config.example.toml:88-93`

- [ ] **Step 1: Read current agents section**

Run: `grep -n "agents" config.example.toml`

- [ ] **Step 2: Update config.example.toml agents section**

Replace the `[agents]` section with per-agent configuration:

```toml
[agents]
# Folders the Content Agent can write to (relative to vault root)
content_write_folders = ["Content/Drafts", "Research"]
# Folders the Calendar Agent can write to
calendar_write_folders = ["Daily"]
# Maximum concurrent agent processes (start with 2, increase after testing)
max_workers = 2

[agents.email]
budget = 1.00
model = "opus"
timeout = 300

[agents.calendar]
budget = 0.50
model = "sonnet"
timeout = 180

[agents.health]
budget = 0.50
model = "sonnet"
timeout = 120

[agents.task]
budget = 0.50
model = "sonnet"
timeout = 180

[agents.feed]
budget = 0.50
model = "sonnet"
timeout = 180
```

- [ ] **Step 3: Update config.toml with same structure**

- [ ] **Step 4: Commit**

```bash
git add config.example.toml
git commit -m "feat: add per-agent budget/model/timeout configuration"
```

---

## Task 3: Implement the Orchestrator

**Files:**
- Create: `collectors/orchestrator.py`

- [ ] **Step 1: Write failing test for orchestrator item grouping**

Create `tests/test_orchestrator.py`:

```python
"""Tests for collectors.orchestrator module."""

import json
from pathlib import Path

import pytest


def test_group_items_by_agent():
    from collectors.orchestrator import group_items_by_agent

    items = {
        "dispatch": [
            {"queue_id": 1, "domain_type": "email", "title": "Test email"},
            {"queue_id": 2, "domain_type": "health", "title": "Leetty: ok"},
            {"queue_id": 3, "domain_type": "email", "title": "Another email"},
        ],
        "prep": [
            {"queue_id": 4, "domain_type": "event", "title": "Meeting"},
        ],
    }
    grouped = group_items_by_agent(items)
    assert len(grouped["email"]) == 2
    assert len(grouped["health"]) == 1
    assert len(grouped["calendar"]) == 1  # event -> calendar agent
    assert "feed" not in grouped  # no feed items


def test_group_items_empty():
    from collectors.orchestrator import group_items_by_agent

    items = {"dispatch": [], "prep": []}
    grouped = group_items_by_agent(items)
    assert grouped == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ceair/Documents/DNM_Projects/chief-of-staff && source .venv/bin/activate && pytest tests/test_orchestrator.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Write the orchestrator module**

Create `collectors/orchestrator.py`:

```python
"""Sweep orchestrator: parallel subagent dispatch via asyncio.

Replaces the monolithic sweep prompt with domain-specific agents
running in parallel. Each agent gets its own budget, timeout, and
log stream.

Usage:
    python collectors/orchestrator.py              # run sweep
    python collectors/orchestrator.py --dry-run    # export only, no agents
    python collectors/orchestrator.py --sequential # force sequential execution
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import connect, get_db_path, init_db
from cos.log import get_logger
from collectors.sweep import apply_actions, export_sweep_items, export_yours_items, mark_done

logger = get_logger("orchestrator")

# Map domain_type to agent name
DOMAIN_TO_AGENT = {
    "email": "email",
    "event": "calendar",
    "health": "health",
    "task": "task",
    "feed": "feed",
    "radar": "feed",  # radar items handled by feed agent
}

DEFAULT_AGENT_CONFIG = {
    "budget": 0.50,
    "model": "sonnet",
    "timeout": 180,
}


def get_agent_config(config: dict, agent_name: str) -> dict:
    """Get agent-specific config with defaults."""
    agents_cfg = config.get("agents", {})
    agent_cfg = agents_cfg.get(agent_name, {})
    return {
        "budget": agent_cfg.get("budget", DEFAULT_AGENT_CONFIG["budget"]),
        "model": agent_cfg.get("model", DEFAULT_AGENT_CONFIG["model"]),
        "timeout": agent_cfg.get("timeout", DEFAULT_AGENT_CONFIG["timeout"]),
    }


def group_items_by_agent(items: dict) -> dict[str, list[dict]]:
    """Group dispatch + prep items by target agent.

    Args:
        items: {"dispatch": [...], "prep": [...]} from export_sweep_items()

    Returns:
        {"email": [...], "calendar": [...], ...} with items tagged with category
    """
    grouped: dict[str, list[dict]] = {}

    for category in ("dispatch", "prep"):
        for item in items.get(category, []):
            item_with_cat = {**item, "category": category}
            agent = DOMAIN_TO_AGENT.get(item["domain_type"])
            if agent:
                grouped.setdefault(agent, []).append(item_with_cat)

    return grouped


async def run_agent(
    agent_name: str,
    items: list[dict],
    config: dict,
    tmp_dir: Path,
) -> dict:
    """Run a single agent subprocess with timeout and logging.

    Returns:
        {"agent": str, "status": str, "actions": list, "duration": float, "error": str|None}
    """
    agent_cfg = get_agent_config(config, agent_name)
    prompt_path = Path(f"prompts/agents/{agent_name}-agent.md")

    if not prompt_path.exists():
        return {
            "agent": agent_name,
            "status": "skipped",
            "actions": [],
            "duration": 0,
            "error": f"Prompt file not found: {prompt_path}",
        }

    # Write isolated input file
    input_file = tmp_dir / f"input_{agent_name}.json"
    output_file = tmp_dir / f"output_{agent_name}.json"
    log_file = tmp_dir / f"log_{agent_name}.txt"

    vault_path = config.get("paths", {}).get("obsidian_vault", "")
    input_data = {
        "items": items,
        "today": datetime.now().strftime("%Y-%m-%d"),
        "vault_path": vault_path,
    }
    input_file.write_text(json.dumps(input_data, ensure_ascii=False, indent=2))

    cmd = [
        "claude",
        "-p", str(prompt_path),
        "--budget", str(agent_cfg["budget"]),
        "--model", agent_cfg["model"],
    ]

    logger.info(
        f"Starting {agent_name} agent: {len(items)} items, "
        f"budget=${agent_cfg['budget']}, model={agent_cfg['model']}"
    )

    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_file.read_bytes()),
            timeout=agent_cfg["timeout"],
        )

        duration = time.monotonic() - start

        # Save raw output for debugging
        log_file.write_text(
            f"EXIT: {proc.returncode}\nSTDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}"
        )

        if proc.returncode != 0:
            logger.error(f"{agent_name} failed (exit {proc.returncode}): {stderr.decode()[:200]}")
            return {
                "agent": agent_name,
                "status": "failed",
                "actions": [],
                "duration": duration,
                "error": stderr.decode()[:500],
            }

        # Parse actions from stdout
        try:
            actions = json.loads(stdout.decode())
            if not isinstance(actions, list):
                actions = actions.get("actions", []) if isinstance(actions, dict) else []
            output_file.write_text(json.dumps(actions, indent=2))
        except json.JSONDecodeError:
            logger.warning(f"{agent_name} returned non-JSON output, attempting extraction")
            actions = _extract_json_from_output(stdout.decode())
            if actions:
                output_file.write_text(json.dumps(actions, indent=2))

        logger.info(f"{agent_name} completed: {len(actions)} actions in {duration:.1f}s")
        return {
            "agent": agent_name,
            "status": "success",
            "actions": actions,
            "duration": duration,
            "error": None,
        }

    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        logger.error(f"{agent_name} timed out after {agent_cfg['timeout']}s")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {
            "agent": agent_name,
            "status": "timeout",
            "actions": [],
            "duration": duration,
            "error": f"Timeout after {agent_cfg['timeout']}s",
        }


def _extract_json_from_output(output: str) -> list[dict]:
    """Try to extract JSON array from Claude output that may contain extra text."""
    import re
    match = re.search(r'\[[\s\S]*\]', output)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return []


async def orchestrate(config: dict, sequential: bool = False) -> dict:
    """Main orchestration: export, dispatch agents, import results.

    Returns:
        Summary dict with counts and per-agent results.
    """
    tmp_dir = Path(".tmp")
    tmp_dir.mkdir(exist_ok=True)

    # Export items
    items = export_sweep_items(config)
    yours_items = export_yours_items(config)
    grouped = group_items_by_agent(items)

    total_items = sum(len(v) for v in grouped.values())
    if total_items == 0:
        logger.info("No items to sweep")
        return {"status": "empty", "agents": {}}

    logger.info(f"Dispatching {total_items} items across {len(grouped)} agents")

    max_workers = config.get("agents", {}).get("max_workers", 2)

    if sequential:
        max_workers = 1

    # Run agents with concurrency limit
    semaphore = asyncio.Semaphore(max_workers)

    async def limited_run(name, agent_items):
        async with semaphore:
            return await run_agent(name, agent_items, config, tmp_dir)

    tasks = [
        limited_run(agent_name, agent_items)
        for agent_name, agent_items in grouped.items()
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    all_actions = []
    agent_summaries = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Unexpected error: {result}")
            continue
        agent_summaries[result["agent"]] = {
            "status": result["status"],
            "actions_count": len(result["actions"]),
            "duration": result["duration"],
            "error": result["error"],
        }
        all_actions.extend(result["actions"])

    # Import all actions to cos.db
    if all_actions:
        stats = apply_actions(config, all_actions)
        logger.info(f"Imported {stats['recorded']} actions ({stats['failed']} failed)")

        # Mark successfully processed items as done
        done_ids = [a["queue_id"] for a in all_actions if a.get("status") == "completed"]
        if done_ids:
            mark_done(config, done_ids)

    # Write sweep manifest
    manifest = {
        "timestamp": datetime.now().isoformat(),
        "total_items": total_items,
        "total_actions": len(all_actions),
        "agents": agent_summaries,
    }
    (tmp_dir / "sweep_manifest.json").write_text(json.dumps(manifest, indent=2))

    # Print summary
    success_count = sum(1 for s in agent_summaries.values() if s["status"] == "success")
    print(f"Morning Sweep complete:")
    print(f"- Agents: {success_count}/{len(agent_summaries)} succeeded")
    print(f"- Actions: {len(all_actions)} recorded")
    print(f"- Yours: {len(yours_items)} items (context in Daily Note)")
    for name, summary in agent_summaries.items():
        status_icon = "OK" if summary["status"] == "success" else "FAIL"
        print(f"  {status_icon} {name}: {summary['actions_count']} actions ({summary['duration']:.1f}s)")

    return manifest


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sweep orchestrator")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument("--sequential", action="store_true", help="Run agents sequentially")
    parser.add_argument("--dry-run", action="store_true", help="Export only, don't run agents")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.dry_run:
        items = export_sweep_items(config)
        grouped = group_items_by_agent(items)
        print(json.dumps({k: len(v) for k, v in grouped.items()}, indent=2))
        return

    result = asyncio.run(orchestrate(config, sequential=args.sequential))
    if not result.get("agents"):
        sys.exit(0)

    # Exit 1 only if ALL agents failed
    all_failed = all(
        s["status"] != "success"
        for s in result.get("agents", {}).values()
    )
    sys.exit(1 if all_failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ceair/Documents/DNM_Projects/chief-of-staff && source .venv/bin/activate && pytest tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Write agent execution tests**

Add to `tests/test_orchestrator.py`:

```python
def test_get_agent_config_with_defaults():
    from collectors.orchestrator import get_agent_config

    config = {"agents": {}}
    cfg = get_agent_config(config, "email")
    assert cfg["budget"] == 0.50
    assert cfg["model"] == "sonnet"
    assert cfg["timeout"] == 180


def test_get_agent_config_with_overrides():
    from collectors.orchestrator import get_agent_config

    config = {"agents": {"email": {"budget": 1.50, "model": "opus", "timeout": 300}}}
    cfg = get_agent_config(config, "email")
    assert cfg["budget"] == 1.50
    assert cfg["model"] == "opus"
    assert cfg["timeout"] == 300


def test_extract_json_from_output():
    from collectors.orchestrator import _extract_json_from_output

    # Clean JSON
    assert _extract_json_from_output('[{"queue_id": 1}]') == [{"queue_id": 1}]

    # JSON embedded in text
    text = 'Here are the results:\n[{"queue_id": 1, "agent": "email"}]\nDone.'
    result = _extract_json_from_output(text)
    assert len(result) == 1
    assert result[0]["queue_id"] == 1

    # No JSON
    assert _extract_json_from_output("no json here") == []
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add collectors/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add async sweep orchestrator with parallel agent dispatch"
```

---

## Task 4: Update run.sh to Use Orchestrator

**Files:**
- Modify: `run.sh:77-84`

- [ ] **Step 1: Update run_sweep function**

Replace the current `run_sweep()`:

```bash
run_sweep() {
    local model=$(read_cfg claude.sweep_model opus)

    echo "=== Step 4: Morning Sweep ==="
    python collectors/orchestrator.py 2>> logs/claude-sweep.log
    echo ""
}
```

Note: Budget and model are now per-agent in config.toml, not passed to a single claude call.

- [ ] **Step 2: Add `sweep-sequential` option for debugging**

Add to the case statement:

```bash
    sweep-seq)
        python collectors/orchestrator.py --sequential
        ;;
```

- [ ] **Step 3: Test run.sh sweep invocation**

Run: `cd /Users/ceair/Documents/DNM_Projects/chief-of-staff && ./run.sh sweep --help 2>/dev/null || python collectors/orchestrator.py --dry-run`
Expected: Shows agent grouping or help text

- [ ] **Step 4: Commit**

```bash
git add run.sh
git commit -m "feat: update run.sh to use parallel orchestrator for sweep"
```

---

## Task 5: Integration Test with Dry Run

- [ ] **Step 1: Run full dry-run test**

```bash
cd /Users/ceair/Documents/DNM_Projects/chief-of-staff
source .venv/bin/activate
python collectors/orchestrator.py --dry-run
```

Expected: JSON output showing item counts per agent (or empty if no pending items)

- [ ] **Step 2: Run orchestrator in sequential mode**

```bash
python collectors/orchestrator.py --sequential
```

Expected: Processes items one agent at a time, logs per-agent results

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All tests pass including new orchestrator tests

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete subagent parallel sweep implementation"
```

---

## Migration Path (Post-Implementation)

1. **Week 1**: Run with `--sequential` flag to validate agent prompts produce correct JSON
2. **Week 2**: Switch to `max_workers=2` (parallel), monitor logs for rate limiting
3. **Week 3**: If stable, increase to `max_workers=3`
4. **Ongoing**: Add new agents by creating prompt file + config entry, no orchestrator changes needed

## Rollback

If parallel execution causes issues:
- `./run.sh sweep-seq` runs sequential mode
- The original `prompts/sweep.md` is untouched and can be restored in run.sh
