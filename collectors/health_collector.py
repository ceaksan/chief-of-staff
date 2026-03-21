"""Health Collector: runs project monitoring scripts and writes results to cos.db.

Each health script must output JSON to stdout:
{
    "status": "ok" | "warning" | "error" | "down",
    "uptime": 99.9,
    "errors_24h": 0,
    "last_error": "optional error message",
    "last_deploy": "2026-03-07T14:30:00Z"
}

Usage:
    python collectors/health_collector.py
    python collectors/health_collector.py --config /path/to/config.toml
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cos.config import load_config
from cos.db import (
    connect,
    finish_run,
    get_db_path,
    init_db,
    insert_health_check,
    start_run,
)
from cos.log import get_logger, log_with_data
import logging

logger = get_logger("health_collector")
SCRIPT_TIMEOUT = 30


def run_health_script(project: str, script_path: str) -> dict | None:
    """Run a health script and parse its JSON output."""
    path = Path(script_path).expanduser()
    if not path.exists():
        log_with_data(
            logger, logging.WARNING, f"Script not found: {path}", {"project": project}
        )
        return None

    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            cwd=path.parent,
        )
    except subprocess.TimeoutExpired:
        log_with_data(
            logger,
            logging.ERROR,
            f"Script timed out after {SCRIPT_TIMEOUT}s",
            {"project": project},
        )
        return {
            "id": f"{project}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            "project": project,
            "status": "down",
            "errors_24h": 0,
            "last_error": f"Health script timed out after {SCRIPT_TIMEOUT}s",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    if result.returncode != 0:
        log_with_data(
            logger,
            logging.ERROR,
            f"Script failed with exit code {result.returncode}",
            {"project": project, "stderr": result.stderr[:500]},
        )
        return {
            "id": f"{project}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            "project": project,
            "status": "error",
            "errors_24h": 0,
            "last_error": f"Health script exited with code {result.returncode}: {result.stderr[:200]}",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        log_with_data(
            logger,
            logging.ERROR,
            f"Invalid JSON from script",
            {"project": project, "stdout": result.stdout[:500], "error": str(e)},
        )
        return {
            "id": f"{project}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            "project": project,
            "status": "error",
            "errors_24h": 0,
            "last_error": f"Health script returned invalid JSON: {e}",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "id": f"{project}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "project": project,
        "status": data.get("status", "error"),
        "uptime": data.get("uptime"),
        "errors_24h": data.get("errors_24h", 0),
        "last_error": data.get("last_error"),
        "last_deploy": data.get("last_deploy"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "priority": "P1" if data.get("status") == "down" else None,
    }


def run_platform_script(script_path: str, config: dict) -> list[dict]:
    """Run a platform health script that returns a JSON array of results."""
    path = Path(script_path).expanduser()
    if not path.exists():
        log_with_data(logger, logging.WARNING, f"Platform script not found: {path}")
        return []

    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            cwd=path.parent,
        )
    except subprocess.TimeoutExpired:
        log_with_data(logger, logging.ERROR, f"Platform script timed out: {path}")
        return []

    if result.returncode != 0:
        log_with_data(
            logger,
            logging.ERROR,
            f"Platform script failed: {path}",
            {"stderr": result.stderr[:500]},
        )
        return []

    try:
        data = json.loads(result.stdout.strip())
        if isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError as e:
        log_with_data(logger, logging.ERROR, f"Invalid JSON from platform script: {e}")
        return []


PLATFORM_SCRIPTS = {
    "cloudflare": Path(__file__).parent / "health_scripts" / "cloudflare_health.py",
}


def collect(config: dict) -> dict:
    """Run all health scripts and write results to cos.db."""
    projects = config.get("health", {}).get("projects", {})
    db_path = get_db_path(config)
    init_db(db_path)

    stats = {"processed": 0, "failed": 0, "skipped": 0}

    with connect(db_path) as conn:
        run_id = start_run(conn, "collector", source="health")

        # Per-project health scripts (existing)
        for project, script_path in projects.items():
            result = run_health_script(project, script_path)
            if result is None:
                stats["skipped"] += 1
                continue

            queue_id = insert_health_check(conn, result)
            if queue_id is not None:
                stats["processed"] += 1
                log_with_data(
                    logger,
                    logging.INFO,
                    f"Collected health for {project}",
                    {"project": project, "status": result["status"]},
                )
            else:
                stats["skipped"] += 1

        # Platform health scripts (Cloudflare, etc.)
        for platform, script_path in PLATFORM_SCRIPTS.items():
            if not config.get(platform):
                continue
            results = run_platform_script(str(script_path), config)
            for result in results:
                queue_id = insert_health_check(conn, result)
                if queue_id is not None:
                    stats["processed"] += 1
                    log_with_data(
                        logger,
                        logging.INFO,
                        f"Collected {platform} health for {result['project']}",
                        {"status": result["status"]},
                    )
                else:
                    stats["skipped"] += 1

        status = "completed" if stats["failed"] == 0 else "partial"
        finish_run(
            conn,
            run_id,
            status=status,
            items_processed=stats["processed"],
            items_failed=stats["failed"],
        )

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Collect project health data")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    args = parser.parse_args()

    config = load_config(args.config)
    stats = collect(config)
    log_with_data(logger, logging.INFO, "Health collection complete", stats)


if __name__ == "__main__":
    main()
