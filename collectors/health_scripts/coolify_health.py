"""Coolify health check: applications, services, and databases status.

Queries the Coolify API via Cloudflare Tunnel and outputs JSON array
with one entry per resource (app, service, database).

Usage:
    python collectors/health_scripts/coolify_health.py
    python collectors/health_scripts/coolify_health.py --config /path/to/config.toml
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cos.config import load_config

try:
    import httpx
except ImportError:
    print("httpx required: pip install httpx", file=sys.stderr)
    sys.exit(1)


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def parse_coolify_status(status_str: str) -> str:
    """Convert Coolify status string to health status.

    Coolify uses format like "running:healthy", "exited:unhealthy", "running:unknown".
    """
    if not status_str:
        return "error"
    parts = status_str.split(":")
    state = parts[0] if parts else ""
    health = parts[1] if len(parts) > 1 else ""

    if state == "running" and health == "healthy":
        return "ok"
    if state == "running" and health == "unknown":
        return "warning"
    if state == "exited" or health == "unhealthy":
        return "error"
    if state == "running":
        return "ok"
    return "error"


def short_name(full_name: str) -> str:
    """Extract readable name from Coolify resource name.

    Coolify names can be like "ceaksan/leetty:main-skkos8040c88wwg840c00sco"
    or "redis-database-us404cgcgggkc8kkoo4gcswo". Extract the meaningful part.
    """
    if "/" in full_name:
        full_name = full_name.split("/")[-1]
    if ":" in full_name:
        full_name = full_name.split(":")[0]
    # Remove Coolify UUID suffixes (24+ char alphanumeric appended after last hyphen)
    cleaned = re.sub(r"-[a-z0-9]{20,}$", "", full_name)
    return cleaned


def fetch_resources(
    base_url: str, token: str, exclude: list[str] | None = None
) -> list[dict]:
    """Fetch all Coolify resources (apps, services, databases)."""
    headers = get_headers(token)
    exclude_set = set(exclude or [])
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    results = []

    def is_excluded(raw_name: str) -> bool:
        clean = short_name(raw_name)
        return clean in exclude_set or raw_name in exclude_set

    with httpx.Client(timeout=15) as client:
        # Applications
        try:
            resp = client.get(f"{base_url}/api/v1/applications", headers=headers)
            resp.raise_for_status()
            for app in resp.json():
                if is_excluded(app.get("name", "")):
                    continue
                name = short_name(app.get("name", "unknown"))
                raw_status = app.get("status", "")
                status = parse_coolify_status(raw_status)
                fqdn = app.get("fqdn", "")

                last_error = None
                if status != "ok":
                    last_error = f"Status: {raw_status}"
                    if fqdn:
                        last_error += f" ({fqdn})"

                results.append(
                    {
                        "id": f"coolify-app-{name}_{today}",
                        "project": f"coolify:{name}",
                        "status": status,
                        "errors_24h": 1 if status == "error" else 0,
                        "last_error": last_error,
                        "checked_at": now.isoformat(),
                        "priority": "P1" if status == "error" else None,
                    }
                )
        except Exception as e:
            print(f"Applications fetch failed: {e}", file=sys.stderr)

        # Services
        try:
            resp = client.get(f"{base_url}/api/v1/services", headers=headers)
            resp.raise_for_status()
            for svc in resp.json():
                if is_excluded(svc.get("name", "")):
                    continue
                name = short_name(svc.get("name", "unknown"))
                raw_status = svc.get("status", "")
                status = parse_coolify_status(raw_status)

                last_error = None
                if status != "ok":
                    last_error = f"Status: {raw_status}"

                results.append(
                    {
                        "id": f"coolify-svc-{name}_{today}",
                        "project": f"coolify:{name}",
                        "status": status,
                        "errors_24h": 1 if status == "error" else 0,
                        "last_error": last_error,
                        "checked_at": now.isoformat(),
                        "priority": "P1" if status == "error" else None,
                    }
                )
        except Exception as e:
            print(f"Services fetch failed: {e}", file=sys.stderr)

        # Databases
        try:
            resp = client.get(f"{base_url}/api/v1/databases", headers=headers)
            resp.raise_for_status()
            for db in resp.json():
                if is_excluded(db.get("name", "")):
                    continue
                name = short_name(db.get("name", "unknown"))
                raw_status = db.get("status", "")
                status = parse_coolify_status(raw_status)

                last_error = None
                if status != "ok":
                    last_error = f"Status: {raw_status}"

                results.append(
                    {
                        "id": f"coolify-db-{name}_{today}",
                        "project": f"coolify:{name}",
                        "status": status,
                        "errors_24h": 1 if status == "error" else 0,
                        "last_error": last_error,
                        "checked_at": now.isoformat(),
                        "priority": "P1" if status == "error" else None,
                    }
                )
        except Exception as e:
            print(f"Databases fetch failed: {e}", file=sys.stderr)

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Coolify health check")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    args = parser.parse_args()

    config = load_config(args.config)
    coolify_config = config.get("coolify", {})

    base_url = coolify_config.get("base_url", "")
    token = coolify_config.get("api_token", "")

    if not base_url or not token:
        print(json.dumps([]))
        return

    exclude = coolify_config.get("exclude", [])
    results = fetch_resources(base_url, token, exclude=exclude)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
