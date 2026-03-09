"""Structured JSON logging for Chief of Staff."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "layer": getattr(record, "layer", record.name),
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        extra = getattr(record, "extra_data", None)
        if extra:
            entry["data"] = extra
        return json.dumps(entry, ensure_ascii=False)


def get_logger(layer: str) -> logging.Logger:
    """Get a structured logger that writes to logs/YYYY-MM-DD.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"

    logger = logging.getLogger(f"cos.{layer}")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # File handler (JSON lines)
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setFormatter(JSONFormatter())
    logger.addHandler(fh)

    # Stderr handler (human readable, warnings+)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(sh)

    return logger


def log_with_data(
    logger: logging.Logger, level: int, msg: str, data: dict | None = None
) -> None:
    """Log a message with optional structured data."""
    record = logger.makeRecord(logger.name, level, "(unknown)", 0, msg, (), None)
    if data:
        record.extra_data = data
    logger.handle(record)
