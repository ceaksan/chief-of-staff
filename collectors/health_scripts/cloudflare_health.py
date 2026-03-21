"""Cloudflare health check: Workers analytics + Pages deployment status.

Outputs JSON array to stdout, one entry per service.
Called by health_collector.py as a health script.

Usage:
    python collectors/health_scripts/cloudflare_health.py
    python collectors/health_scripts/cloudflare_health.py --config /path/to/config.toml
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cos.config import load_config

try:
    import httpx
except ImportError:
    print("httpx required: pip install httpx", file=sys.stderr)
    sys.exit(1)

API_BASE = "https://api.cloudflare.com/client/v4"
GRAPHQL_URL = f"{API_BASE}/graphql"
LOOKBACK_HOURS = 24


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def fetch_workers_analytics(
    client: httpx.Client, account_id: str, token: str, worker_names: list[str]
) -> dict[str, dict]:
    """Fetch error/request counts for workers via GraphQL."""
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    query = """
    {
        viewer {
            accounts(filter: {accountTag: "%s"}) {
                workersInvocationsAdaptive(
                    limit: 100,
                    filter: {datetime_gt: "%s"}
                ) {
                    dimensions {
                        scriptName
                        status
                    }
                    sum {
                        requests
                        errors
                    }
                }
            }
        }
    }
    """ % (account_id, since)

    resp = client.post(GRAPHQL_URL, headers=get_headers(token), json={"query": query})
    resp.raise_for_status()
    data = resp.json()

    results: dict[str, dict] = {}
    invocations = (
        data.get("data", {})
        .get("viewer", {})
        .get("accounts", [{}])[0]
        .get("workersInvocationsAdaptive", [])
    )

    for inv in invocations:
        name = inv["dimensions"]["scriptName"]
        if name not in worker_names:
            continue
        if name not in results:
            results[name] = {"requests": 0, "errors": 0}
        results[name]["requests"] += inv["sum"]["requests"]
        results[name]["errors"] += inv["sum"]["errors"]

    # Include workers with zero traffic
    for name in worker_names:
        if name not in results:
            results[name] = {"requests": 0, "errors": 0}

    return results


def fetch_pages_deployments(
    client: httpx.Client, account_id: str, token: str, page_names: list[str]
) -> dict[str, dict]:
    """Fetch latest deployment status for Pages projects."""
    results: dict[str, dict] = {}

    for name in page_names:
        url = f"{API_BASE}/accounts/{account_id}/pages/projects/{name}/deployments"
        resp = client.get(url, headers=get_headers(token), params={"per_page": 1})

        if resp.status_code != 200:
            results[name] = {
                "status": "error",
                "error": f"API {resp.status_code}",
            }
            continue

        data = resp.json()
        deployments = data.get("result", [])

        if not deployments:
            results[name] = {"status": "ok", "last_deploy": None}
            continue

        latest = deployments[0]
        stage = latest.get("latest_stage", {})
        deploy_status = stage.get("status", "unknown")
        deploy_time = stage.get("ended_on") or latest.get("created_on")

        trigger = latest.get("deployment_trigger", {})
        commit_msg = trigger.get("metadata", {}).get("commit_message", "")
        if commit_msg:
            commit_msg = commit_msg.split("\n")[0][:80]

        results[name] = {
            "deploy_status": deploy_status,
            "last_deploy": deploy_time,
            "commit": commit_msg,
            "environment": latest.get("environment", ""),
        }

    return results


def determine_status(errors: int, requests: int) -> str:
    if requests == 0:
        return "ok"
    error_rate = errors / requests
    if error_rate > 0.1:
        return "error"
    if error_rate > 0.01 or errors > 10:
        return "warning"
    return "ok"


def build_health_results(
    workers: dict[str, dict], pages: dict[str, dict]
) -> list[dict]:
    """Convert API data to health_collector output format."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    results = []

    for name, data in workers.items():
        status = determine_status(data["errors"], data["requests"])
        last_error = None
        if data["errors"] > 0:
            last_error = f"{data['errors']} errors in {data['requests']} requests ({LOOKBACK_HOURS}h)"

        results.append(
            {
                "id": f"cf-worker-{name}_{today}",
                "project": f"cf:{name}",
                "status": status,
                "errors_24h": data["errors"],
                "last_error": last_error,
                "checked_at": now.isoformat(),
                "priority": "P1" if status == "error" else None,
            }
        )

    for name, data in pages.items():
        if "error" in data and "deploy_status" not in data:
            status = "error"
            last_error = data["error"]
        elif data.get("deploy_status") == "failure":
            status = "error"
            last_error = f"Deploy failed: {data.get('commit', 'unknown commit')}"
        else:
            status = "ok"
            last_error = None

        results.append(
            {
                "id": f"cf-pages-{name}_{today}",
                "project": f"cf:{name}",
                "status": status,
                "last_deploy": data.get("last_deploy"),
                "last_error": last_error,
                "errors_24h": 1 if status == "error" else 0,
                "checked_at": now.isoformat(),
                "priority": "P1" if status == "error" else None,
            }
        )

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Cloudflare health check")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    args = parser.parse_args()

    config = load_config(args.config)
    cf_config = config.get("cloudflare", {})

    token = cf_config.get("api_token", "")
    account_id = cf_config.get("account_id", "")

    if not token or not account_id:
        print(json.dumps([]))
        return

    worker_names = cf_config.get("workers", [])
    page_names = cf_config.get("pages", [])

    with httpx.Client(timeout=15) as client:
        workers = {}
        pages = {}

        if worker_names:
            try:
                workers = fetch_workers_analytics(
                    client, account_id, token, worker_names
                )
            except Exception as e:
                print(f"Workers analytics failed: {e}", file=sys.stderr)

        if page_names:
            try:
                pages = fetch_pages_deployments(client, account_id, token, page_names)
            except Exception as e:
                print(f"Pages deployments failed: {e}", file=sys.stderr)

    results = build_health_results(workers, pages)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
