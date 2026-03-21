"""Parallel sweep orchestrator: dispatches domain-specific agents concurrently.

Usage:
    python collectors/orchestrator.py
    python collectors/orchestrator.py --sequential
    python collectors/orchestrator.py --dry-run
    python collectors/orchestrator.py --config /path/to/config.toml
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.log import get_logger, log_with_data
from collectors.sweep import apply_actions, export_sweep_items, mark_done

logger = get_logger("orchestrator")

DOMAIN_TO_AGENT = {
    # "email" excluded: classification only, no auto-drafting
    "event": "calendar",
    "health": "health",
    "task": "task",
    "feed": "feed",
    "radar": "feed",
}

DEFAULT_AGENT_CONFIG = {
    "budget": 0.50,
    "model": "sonnet",
    "timeout": 180,
    "allowed_tools": "Read,Write,Edit,Bash,Glob,Grep",
}

AGENT_ALLOWED_TOOLS = {
    "email": "Read,Bash,mcp__claude_ai_Gmail__gmail_read_thread,mcp__claude_ai_Gmail__gmail_read_message,mcp__claude_ai_Gmail__gmail_create_draft,mcp__claude_ai_Gmail__gmail_search_messages",
    "calendar": "Read,Write,Bash,mcp__claude_ai_Google_Calendar__gcal_list_events,mcp__claude_ai_Google_Calendar__gcal_get_event",
    "health": "Read,Write,Bash",
    "task": "Read,Write,Bash",
    "feed": "Read,Write,Bash",
}

PROJECT_ROOT = Path(__file__).parent.parent
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def get_agent_config(config: dict, agent_name: str) -> dict:
    """Get per-agent config with defaults."""
    agents_section = config.get("agents", {})
    agent_overrides = agents_section.get(agent_name, {})
    merged = {**DEFAULT_AGENT_CONFIG, **agent_overrides}
    return {k: merged[k] for k in DEFAULT_AGENT_CONFIG}


def group_items_by_agent(items: dict) -> dict[str, list[dict]]:
    """Group dispatch+prep items by target agent.

    Args:
        items: {"dispatch": [...], "prep": [...]}

    Returns:
        {"email": [...], "calendar": [...], ...} with items tagged by category.
    """
    grouped: dict[str, list[dict]] = {}

    for category in ("dispatch", "prep"):
        for item in items.get(category, []):
            domain_type = item.get("domain_type", "")
            agent_name = DOMAIN_TO_AGENT.get(domain_type)
            if not agent_name:
                log_with_data(
                    logger,
                    logging.WARNING,
                    f"Unknown domain_type '{domain_type}', skipping item",
                    {"queue_id": item.get("queue_id")},
                )
                continue
            tagged = {**item, "category": category}
            grouped.setdefault(agent_name, []).append(tagged)

    return grouped


def _extract_json_from_output(output: str) -> list[dict]:
    """Regex extract JSON array from output that may contain extra text."""
    output = output.strip()
    if not output:
        return []

    # Try direct parse first
    try:
        parsed = json.loads(output)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _JSON_ARRAY_RE.search(output)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return []


async def run_agent(
    agent_name: str,
    items: list[dict],
    config: dict,
    tmp_dir: Path,
) -> dict:
    """Run a single agent as async subprocess.

    Returns:
        {"agent": str, "status": str, "actions": list, "duration": float, "error": str|None}
    """
    agent_cfg = get_agent_config(config, agent_name)
    budget = agent_cfg["budget"]
    model = agent_cfg["model"]
    timeout = agent_cfg["timeout"]

    input_file = tmp_dir / f"{agent_name}_input.json"
    output_file = tmp_dir / f"{agent_name}_output.json"
    log_file = tmp_dir / f"{agent_name}.log"

    input_payload = json.dumps(items, ensure_ascii=False, indent=2)
    # Debug artifact: write input to file for post-mortem inspection
    input_file.write_text(input_payload, encoding="utf-8")

    prompt_path = PROJECT_ROOT / "prompts" / "agents" / f"{agent_name}-agent.md"

    start_time = time.monotonic()

    allowed_tools = AGENT_ALLOWED_TOOLS.get(
        agent_name, DEFAULT_AGENT_CONFIG["allowed_tools"]
    )

    cmd = [
        "claude",
        "-p",
        str(prompt_path),
        "--max-budget-usd",
        str(budget),
        "--model",
        model,
        "--allowedTools",
        allowed_tools,
        "--permission-mode",
        "bypassPermissions",
    ]

    log_with_data(
        logger,
        logging.INFO,
        f"Starting agent: {agent_name}",
        {"items": len(items), "budget": budget, "model": model, "timeout": timeout},
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=input_payload.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration = time.monotonic() - start_time
            log_with_data(
                logger,
                logging.ERROR,
                f"Agent {agent_name} timed out after {timeout}s",
            )
            return {
                "agent": agent_name,
                "status": "timeout",
                "actions": [],
                "duration": duration,
                "error": f"Timed out after {timeout}s",
            }

        duration = time.monotonic() - start_time
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # Write output files for debugging
        output_file.write_text(stdout, encoding="utf-8")
        if stderr:
            log_file.write_text(stderr, encoding="utf-8")

        if proc.returncode != 0:
            log_with_data(
                logger,
                logging.ERROR,
                f"Agent {agent_name} exited with code {proc.returncode}",
                {"stderr": stderr[:500]},
            )
            return {
                "agent": agent_name,
                "status": "error",
                "actions": [],
                "duration": duration,
                "error": f"Exit code {proc.returncode}: {stderr[:200]}",
            }

        actions = _extract_json_from_output(stdout)
        log_with_data(
            logger,
            logging.INFO,
            f"Agent {agent_name} completed",
            {"actions": len(actions), "duration": round(duration, 2)},
        )

        return {
            "agent": agent_name,
            "status": "success",
            "actions": actions,
            "duration": duration,
            "error": None,
        }

    except FileNotFoundError:
        duration = time.monotonic() - start_time
        msg = f"claude CLI not found - cannot run agent {agent_name}"
        log_with_data(logger, logging.ERROR, msg)
        return {
            "agent": agent_name,
            "status": "error",
            "actions": [],
            "duration": duration,
            "error": msg,
        }
    except Exception as e:
        duration = time.monotonic() - start_time
        log_with_data(logger, logging.ERROR, f"Agent {agent_name} failed: {e}")
        return {
            "agent": agent_name,
            "status": "error",
            "actions": [],
            "duration": duration,
            "error": str(e),
        }


async def orchestrate(config: dict, sequential: bool = False) -> dict:
    """Main orchestration: export items, dispatch agents, collect results, import actions.

    Returns:
        Summary dict with per-agent results and totals.
    """
    max_workers = config.get("agents", {}).get("max_workers", 2)
    semaphore = asyncio.Semaphore(max_workers)

    log_with_data(
        logger,
        logging.INFO,
        "Starting orchestration",
        {"max_workers": max_workers, "sequential": sequential},
    )

    sweep_items = export_sweep_items(config)
    total_items = len(sweep_items.get("dispatch", [])) + len(
        sweep_items.get("prep", [])
    )

    if total_items == 0:
        log_with_data(logger, logging.INFO, "No items to process")
        return {
            "status": "empty",
            "agents": [],
            "totals": {"items": 0, "actions_recorded": 0, "actions_failed": 0},
        }

    grouped = group_items_by_agent(sweep_items)

    if not grouped:
        log_with_data(logger, logging.INFO, "No items mapped to any agent")
        return {
            "status": "empty",
            "agents": [],
            "totals": {
                "items": total_items,
                "actions_recorded": 0,
                "actions_failed": 0,
            },
        }

    tmp_dir = PROJECT_ROOT / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    async def run_with_semaphore(agent_name: str, items: list[dict]) -> dict:
        async with semaphore:
            return await run_agent(agent_name, items, config, tmp_dir)

    if sequential:
        results = []
        for agent_name, items in grouped.items():
            result = await run_agent(agent_name, items, config, tmp_dir)
            results.append(result)
    else:
        tasks = [
            run_with_semaphore(agent_name, items)
            for agent_name, items in grouped.items()
        ]
        results = await asyncio.gather(*tasks)

    # Collect all actions from successful agents
    all_actions: list[dict] = []
    done_queue_ids: list[int] = []

    for result in results:
        if result["status"] == "success" and result["actions"]:
            all_actions.extend(result["actions"])
            done_queue_ids.extend(
                a["queue_id"] for a in result["actions"] if a.get("queue_id")
            )

    # Apply actions and mark done
    apply_stats = {"recorded": 0, "failed": 0}
    done_stats = {"done": 0, "failed": 0}

    if all_actions:
        apply_stats = apply_actions(config, all_actions)

    if done_queue_ids:
        done_stats = mark_done(config, done_queue_ids)

    # Determine overall status
    successful = [r for r in results if r["status"] == "success"]
    failed_count = len(results) - len(successful)

    if not successful and failed_count:
        overall_status = "failed"
    elif failed_count:
        overall_status = "partial"
    else:
        overall_status = "success"

    manifest = {
        "status": overall_status,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "agents": results,
        "apply_stats": apply_stats,
        "done_stats": done_stats,
        "totals": {
            "items": total_items,
            "actions_recorded": apply_stats["recorded"],
            "actions_failed": apply_stats["failed"],
        },
    }

    manifest_path = tmp_dir / "orchestrator_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log_with_data(
        logger,
        logging.INFO,
        "Orchestration complete",
        {
            "status": overall_status,
            "agents_run": len(results),
            "agents_success": len(successful),
            "agents_failed": failed_count,
            "actions_recorded": apply_stats["recorded"],
        },
    )

    return manifest


def _print_summary(manifest: dict) -> None:
    """Print human-readable summary to stdout."""
    status = manifest.get("status", "unknown")
    totals = manifest.get("totals", {})
    agents = manifest.get("agents", [])

    print(f"\nOrchestration {status.upper()}")
    print(f"  Items processed: {totals.get('items', 0)}")
    print(f"  Actions recorded: {totals.get('actions_recorded', 0)}")
    print(f"  Actions failed: {totals.get('actions_failed', 0)}")
    print()

    for result in agents:
        agent = result.get("agent", "?")
        agent_status = result.get("status", "?")
        actions = len(result.get("actions", []))
        duration = result.get("duration", 0)
        error = result.get("error")

        line = f"  [{agent_status.upper():8}] {agent:<12} {actions:3} actions  {duration:6.1f}s"
        if error:
            line += f"  ERROR: {error}"
        print(line)

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chief of Staff parallel sweep orchestrator"
    )
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run agents sequentially instead of in parallel",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Export and group items but do not run agents",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.dry_run:
        sweep_items = export_sweep_items(config)
        grouped = group_items_by_agent(sweep_items)
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "total_items": sum(len(v) for v in grouped.values()),
                    "by_agent": {k: len(v) for k, v in grouped.items()},
                },
                indent=2,
            )
        )
        return

    manifest = asyncio.run(orchestrate(config, sequential=args.sequential))
    _print_summary(manifest)

    status = manifest.get("status", "failed")
    sys.exit(0 if status in ("success", "partial", "empty") else 1)


if __name__ == "__main__":
    main()
