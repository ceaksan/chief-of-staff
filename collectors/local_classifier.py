"""Local Classifier: classifies pending work items with Ollama instead of Claude.

The models live on a separate machine that is not always awake, so this fails loudly
rather than falling back to a paid model. A missed run costs little: the feed quota in
export_pending means the next run picks up where this one stopped.

Usage:
    python collectors/local_classifier.py
    python collectors/local_classifier.py --dry-run
    python collectors/local_classifier.py --limit 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.classifier import apply_classifications, export_pending
from cos.config import load_config
from cos.log import get_logger, log_with_data

logger = get_logger("local_classifier")

VALID_CATEGORIES = {"dispatch", "prep", "yours", "skip"}
PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "classify_item.md"

DEFAULT_HOST = "http://100.110.82.52:11434"
DEFAULT_MODEL = "qwen3.5:35b-a3b-q8_0"
DEFAULT_TIMEOUT = 120


def llm_config(config: dict) -> dict:
    """Read [local_llm], falling back to the host the taste layer already uses."""
    section = config.get("local_llm", {})
    host = section.get("host") or config.get("taste", {}).get(
        "ollama_url", DEFAULT_HOST
    )
    if not host.startswith("http"):
        host = f"http://{host}"
    return {
        "host": host.rstrip("/"),
        "model": section.get("model", DEFAULT_MODEL),
        "timeout": section.get("timeout", DEFAULT_TIMEOUT),
    }


def check_host(cfg: dict) -> None:
    """Raise if the model host is not reachable, naming the machine to wake up."""
    try:
        resp = httpx.get(f"{cfg['host']}/api/tags", timeout=10)
        resp.raise_for_status()
    except Exception as e:
        raise SystemExit(
            f"Ollama unreachable at {cfg['host']}: {e}\n"
            "Wake the model host and run again. There is no paid fallback by design."
        ) from e

    names = {m["name"] for m in resp.json().get("models", [])}
    if cfg["model"] not in names:
        raise SystemExit(
            f"Model {cfg['model']} not on {cfg['host']}. Available: {sorted(names)}"
        )


def build_prompt(item: dict) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    # str.format would trip over the JSON braces in the template's output example.
    for field in ("domain_type", "context", "title", "detail"):
        value = item.get(field) or "-"
        template = template.replace("{" + field + "}", str(value)[:400])
    return template


def classify_item(item: dict, cfg: dict, client: httpx.Client) -> dict | None:
    """Classify one item. Returns None when the model gives nothing usable."""
    payload = {
        "model": cfg["model"],
        "prompt": build_prompt(item),
        "stream": False,
        # Without this the model answers inside `thinking` and `response` comes back empty.
        "think": False,
        "format": "json",
        # Same item classified twice must land in the same category.
        "options": {"temperature": 0, "num_predict": 200},
    }

    try:
        resp = client.post(f"{cfg['host']}/api/generate", json=payload)
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        data = json.loads(raw)
    except Exception as e:
        logger.warning("Item %s: no usable answer (%s)", item.get("queue_id"), e)
        return None

    category = str(data.get("category", "")).lower().strip()
    if category not in VALID_CATEGORIES:
        logger.warning(
            "Item %s: model returned category %r, skipping",
            item.get("queue_id"),
            category,
        )
        return None

    return {
        "queue_id": item["queue_id"],
        "category": category,
        "reason": str(data.get("reason", ""))[:300],
    }


def classify_pending(
    config: dict, dry_run: bool = False, limit: int | None = None
) -> dict:
    cfg = llm_config(config)
    check_host(cfg)

    items = export_pending(config)
    if limit:
        items = items[:limit]

    if not items:
        log_with_data(logger, logging.INFO, "Nothing pending to classify")
        return {"classified": 0, "failed": 0}

    log_with_data(
        logger,
        logging.INFO,
        f"Classifying {len(items)} items with {cfg['model']} on {cfg['host']}",
    )

    results, failed = [], 0
    with httpx.Client(timeout=cfg["timeout"]) as client:
        for item in items:
            result = classify_item(item, cfg, client)
            if result is None:
                failed += 1
                continue
            results.append(result)
            if dry_run:
                print(
                    f"  [{result['category']:<8}] {(item.get('title') or '')[:64]}"
                    f"\n             {result['reason']}"
                )

    if dry_run:
        print(
            f"\n--- DRY RUN: {len(results)} classified, {failed} failed, 0 written ---"
        )
        return {"classified": len(results), "failed": failed}

    stats = apply_classifications(config, results, model=f"ollama:{cfg['model']}")
    stats["failed"] = stats.get("failed", 0) + failed
    return stats


def main():
    parser = argparse.ArgumentParser(description="Classify pending items locally")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--limit", type=int, help="Classify at most N items")
    args = parser.parse_args()

    config = load_config(args.config) if args.config else load_config()
    stats = classify_pending(config, dry_run=args.dry_run, limit=args.limit)
    print(f"Classified: {stats['classified']}, Failed: {stats['failed']}")


if __name__ == "__main__":
    main()
