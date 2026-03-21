#!/usr/bin/env python3
"""Chief of Staff - Interactive Setup Wizard.

Generates config.toml from config.example.toml, initializes SQLite database,
and optionally sets up macOS launchd scheduling.

Usage:
    python setup_wizard.py              # Interactive setup
    python setup_wizard.py --validate   # Validate existing config.toml
"""

import os
import re
import sqlite3
import subprocess
import sys
import xml.sax.saxutils as saxutils
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
CONFIG_TEMPLATE = PROJECT_ROOT / "config.example.toml"
CONFIG_OUTPUT = PROJECT_ROOT / "config.toml"
SCHEMA_FILE = PROJECT_ROOT / "schema.sql"
PLIST_TEMPLATE = PROJECT_ROOT / "com.chief-of-staff.overnight.plist"
PLIST_DEST = (
    Path.home() / "Library" / "LaunchAgents" / "com.chief-of-staff.overnight.plist"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ask(prompt, default="", required=False):
    display = f"  {prompt} [{default}]: " if default else f"  {prompt}: "
    while True:
        value = input(display).strip()
        if not value:
            if required and not default:
                print("    This field is required.")
                continue
            return default
        return value


def ask_yes_no(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    value = input(f"  {prompt} {suffix}: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def ask_list(prompt, default=None):
    default_str = ", ".join(str(d) for d in default) if default else ""
    raw = ask(prompt, default_str)
    if not raw:
        return default or []
    return [item.strip() for item in raw.split(",") if item.strip()]


def replace_value(content, key, value, section=None):
    """Replace a TOML key's value within a specific section."""
    if isinstance(value, bool):
        toml_val = "true" if value else "false"
    elif isinstance(value, (int, float)):
        toml_val = str(value)
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str):
                items.append(f'"{item}"')
            else:
                items.append(str(item))
        toml_val = f"[{', '.join(items)}]"
    else:
        toml_val = f'"{value}"'

    pattern = rf"^(\s*{re.escape(key)}\s*=\s*).*$"

    if section:
        section_pat = rf"^\[{re.escape(section)}\]"
        lines = content.split("\n")
        in_section = False
        replaced = False
        result = []
        for line in lines:
            if re.match(section_pat, line):
                in_section = True
                result.append(line)
                continue
            if in_section and re.match(r"^\[", line):
                in_section = False
            if in_section and not replaced and re.match(pattern, line):
                result.append(re.sub(pattern, rf"\g<1>{toml_val}", line))
                replaced = True
            else:
                result.append(line)
        return "\n".join(result)

    return re.sub(pattern, rf"\g<1>{toml_val}", content, count=1, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------


def setup_paths(content):
    print("\n--- Paths ---")

    vault = ask("Obsidian vault path", required=True)
    vault = os.path.expanduser(vault)
    if not Path(vault).is_dir():
        print(f"    WARNING: Directory does not exist: {vault}")
        if not ask_yes_no("Continue anyway?", False):
            return content, False

    daily_dir = ask("Daily notes subdirectory", "Daily")
    cos_dir = ask("Chief of Staff directory", str(PROJECT_ROOT))
    cos_dir = os.path.expanduser(cos_dir)

    content = replace_value(content, "obsidian_vault", vault, "paths")
    content = replace_value(content, "daily_notes_dir", daily_dir, "paths")
    content = replace_value(content, "cos_dir", cos_dir, "paths")

    return content, True


def setup_calendars(content):
    print("\n--- Calendars ---")
    print('  Calendar IDs from Google Calendar (e.g. "primary", "user@gmail.com")')

    cal_ids = ask_list("Calendar IDs (comma-separated)", ["primary"])
    content = replace_value(content, "ids", cal_ids, "calendars")
    return content


def setup_claude(content):
    print("\n--- Claude Budget & Model ---")
    print("  Defaults: collector $2, classifier $1.50, sweep $3, dayblock $1")
    if not ask_yes_no("Customize budgets?", False):
        return content

    for key, label, default in [
        ("collector_budget", "Collector budget ($)", "2.00"),
        ("classifier_budget", "Classifier budget ($)", "1.50"),
        ("sweep_budget", "Sweep budget ($)", "3.00"),
        ("dayblock_budget", "Day Block budget ($)", "1.00"),
    ]:
        val = ask(label, default)
        content = replace_value(content, key, float(val), "claude")

    return content


def setup_schedule(content):
    print("\n--- Schedule ---")
    time_str = ask("Overnight collection time (HH:MM)", "06:00")
    content = replace_value(content, "collector_time", time_str, "schedule")
    return content, time_str


def setup_classification(content):
    print("\n--- Classification Keywords ---")
    if not ask_yes_no("Customize classification keywords?", False):
        return content

    yours = ask_list(
        "force_yours keywords (comma-separated)",
        ["pricing", "strategy", "contract", "negotiation", "proposal"],
    )
    dispatch = ask_list(
        "force_dispatch keywords (comma-separated)",
        ["meeting confirmation", "calendar update", "subscription renewal"],
    )

    content = replace_value(content, "force_yours", yours, "classification")
    content = replace_value(content, "force_dispatch", dispatch, "classification")
    return content


def setup_dayblock(content):
    print("\n--- Day Block (time preferences) ---")
    if not ask_yes_no("Customize time blocks?", False):
        return content

    for key, label, default in [
        ("deep_work_start", "Deep work start", "07:00"),
        ("deep_work_end", "Deep work end", "12:00"),
        ("content_start", "Content block start", "13:00"),
        ("content_end", "Content block end", "17:00"),
        ("admin_start", "Admin block start", "17:00"),
        ("admin_end", "Admin block end", "18:00"),
    ]:
        val = ask(label, default)
        content = replace_value(content, key, val, "dayblock")

    gym = ask_list("Gym days (0=Mon, 6=Sun)", ["1", "3", "5"])
    gym_days = [int(d) for d in gym if d.isdigit()]
    content = replace_value(content, "gym_days", gym_days, "dayblock")

    return content


def setup_miniflux(content):
    if not ask_yes_no("Configure Miniflux (RSS reader)?", False):
        return content

    url = ask("Miniflux URL", required=True)
    token = ask("Miniflux API token", required=True)
    content = replace_value(content, "base_url", url, "miniflux")
    content = replace_value(content, "api_token", token, "miniflux")
    return content


def setup_coolify(content):
    if not ask_yes_no("Configure Coolify (container monitoring)?", False):
        return content

    url = ask("Coolify URL", required=True)
    token = ask("Coolify API token", required=True)
    content = replace_value(content, "base_url", url, "coolify")
    content = replace_value(content, "api_token", token, "coolify")

    exclude = ask_list("Resource names to exclude (comma-separated)", [])
    if exclude:
        content = replace_value(content, "exclude", exclude, "coolify")
    return content


def setup_cloudflare(content):
    if not ask_yes_no("Configure Cloudflare (Workers & Pages)?", False):
        return content

    token = ask("Cloudflare API token", required=True)
    account_id = ask("Cloudflare account ID", required=True)
    workers = ask_list("Worker names (comma-separated)", [])
    pages = ask_list("Pages project names (comma-separated)", [])

    content = replace_value(content, "api_token", token, "cloudflare")
    content = replace_value(content, "account_id", account_id, "cloudflare")
    if workers:
        content = replace_value(content, "workers", workers, "cloudflare")
    if pages:
        content = replace_value(content, "pages", pages, "cloudflare")
    return content


def setup_healthchecks(content):
    if not ask_yes_no("Configure healthchecks.io?", False):
        return content

    for key, label in [
        ("pipeline_url", "Pipeline ping URL"),
        ("feed_url", "Feed ping URL"),
        ("sweep_url", "Sweep ping URL"),
        ("weekly_url", "Weekly ping URL"),
    ]:
        val = ask(label)
        if val:
            content = replace_value(content, key, val, "healthchecks")
    return content


# ---------------------------------------------------------------------------
# Post-config steps
# ---------------------------------------------------------------------------


def init_database():
    print("\n--- Database ---")
    db_path = PROJECT_ROOT / "cos.db"

    if db_path.exists():
        print(f"  Database exists: {db_path}")
        if not ask_yes_no("Reinitialize? (data will be lost)", False):
            return

    if not SCHEMA_FILE.exists():
        print(f"  ERROR: Schema not found: {SCHEMA_FILE}")
        return

    schema = SCHEMA_FILE.read_text()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema)
    conn.close()
    print(f"  Database initialized: {db_path}")


def setup_launchd(collector_time):
    print("\n--- Scheduling ---")
    print("  Options: launchd (macOS) or crontab (Linux/macOS)")

    if sys.platform != "darwin":
        print("  Not macOS. Add to crontab manually:")
        parts = collector_time.split(":")
        hour = parts[0] if parts else "6"
        minute = parts[1] if len(parts) > 1 else "0"
        print(f"    {minute} {hour} * * * cd {PROJECT_ROOT} && ./cos-brief.sh run full")
        return

    if not ask_yes_no("Install launchd agent?", True):
        print("  Skipped. Add to crontab manually if needed:")
        parts = collector_time.split(":")
        hour = parts[0] if parts else "6"
        minute = parts[1] if len(parts) > 1 else "0"
        print(f"    {minute} {hour} * * * cd {PROJECT_ROOT} && ./cos-brief.sh run full")
        return

    if not PLIST_TEMPLATE.exists():
        print(f"  ERROR: Plist template not found: {PLIST_TEMPLATE}")
        return

    parts = collector_time.split(":")
    hour = int(parts[0]) if parts else 9
    minute = int(parts[1]) if len(parts) > 1 else 0

    cos_path_escaped = saxutils.escape(str(PROJECT_ROOT))

    plist = PLIST_TEMPLATE.read_text()
    plist = plist.replace(
        "/path/to/chief-of-staff/run.sh",
        f"{cos_path_escaped}/cos-brief.sh run full",
    )
    plist = plist.replace(
        "/path/to/chief-of-staff/logs",
        f"{cos_path_escaped}/logs",
    )
    plist = re.sub(
        r"<key>Hour</key>\s*<integer>\d+</integer>",
        f"<key>Hour</key>\n        <integer>{hour}</integer>",
        plist,
    )
    plist = re.sub(
        r"<key>Minute</key>\s*<integer>\d+</integer>",
        f"<key>Minute</key>\n        <integer>{minute}</integer>",
        plist,
    )

    PLIST_DEST.parent.mkdir(parents=True, exist_ok=True)

    if PLIST_DEST.exists():
        print(f"  Plist exists: {PLIST_DEST}")
        if not ask_yes_no("Overwrite?", False):
            return
        subprocess.run(["launchctl", "unload", str(PLIST_DEST)], capture_output=True)

    PLIST_DEST.write_text(plist)
    print(f"  Installed: {PLIST_DEST}")

    result = subprocess.run(
        ["launchctl", "load", str(PLIST_DEST)], capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  Loaded. Pipeline runs daily at {collector_time}.")
    else:
        print(f"  WARNING: launchctl load failed: {result.stderr.strip()}")
        print(f"  Try: launchctl load {PLIST_DEST}")


# ---------------------------------------------------------------------------
# Validate mode
# ---------------------------------------------------------------------------


def validate_config():
    print("\n--- Validating config.toml ---\n")

    if not CONFIG_OUTPUT.exists():
        print("  ERROR: config.toml not found. Run setup_wizard.py first.")
        sys.exit(1)

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib
        except ImportError:
            print("  ERROR: tomli required for Python < 3.11")
            sys.exit(1)

    try:
        with open(CONFIG_OUTPUT, "rb") as f:
            config = tomllib.load(f)
    except Exception as e:
        print(f"  ERROR: Invalid TOML: {e}")
        sys.exit(1)

    errors = []
    warnings = []

    # Paths
    vault = config.get("paths", {}).get("obsidian_vault", "")
    if not vault or vault == "/path/to/your/vault":
        errors.append("paths.obsidian_vault not configured")
    elif not Path(os.path.expanduser(vault)).is_dir():
        warnings.append(f"paths.obsidian_vault not found: {vault}")

    # Calendars
    if not config.get("calendars", {}).get("ids", []):
        errors.append("calendars.ids is empty")

    # Claude budgets
    claude_cfg = config.get("claude", {})
    for key in ["collector_budget", "classifier_budget", "sweep_budget"]:
        val = claude_cfg.get(key, 0)
        if val <= 0:
            errors.append(f"claude.{key} must be positive")
        elif val > 10:
            warnings.append(f"claude.{key} = {val} seems high")

    # Optional integrations: check for placeholder values
    for section, url_key in [
        ("miniflux", "base_url"),
        ("coolify", "base_url"),
    ]:
        sec = config.get(section, {})
        url = sec.get(url_key, "")
        if url and "example.com" in url:
            warnings.append(f"{section}.{url_key} still has example placeholder")

    # Database
    db_path = PROJECT_ROOT / "cos.db"
    if not db_path.exists():
        errors.append("cos.db not found. Run: sqlite3 cos.db < schema.sql")

    # Requirements
    req_file = PROJECT_ROOT / "requirements.txt"
    if req_file.exists():
        try:
            import importlib

            for pkg in ["httpx"]:
                try:
                    importlib.import_module(pkg)
                except ImportError:
                    warnings.append(f"Python package '{pkg}' not installed")
        except Exception:
            pass

    # Report
    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    x {e}")

    if warnings:
        print(f"  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    ! {w}")

    if not errors and not warnings:
        print("  All checks passed.")
    elif not errors:
        print(f"\n  Valid with {len(warnings)} warning(s).")
    else:
        print(f"\n  {len(errors)} error(s) found. Fix before running pipeline.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if "--validate" in sys.argv:
        validate_config()
        return

    print()
    print("=" * 50)
    print("  Chief of Staff - Setup Wizard")
    print("=" * 50)

    # Pre-checks
    print("\n--- Pre-checks ---")
    v = sys.version_info
    if v < (3, 11):
        print(f"  Python 3.11+ required. You have {v.major}.{v.minor}.{v.micro}")
        sys.exit(1)
    print(f"  Python {v.major}.{v.minor}.{v.micro}")

    if sys.prefix == sys.base_prefix:
        print("  WARNING: No virtual environment active.")
        print("  Recommended: python3 -m venv .venv && source .venv/bin/activate")
        if not ask_yes_no("Continue anyway?", False):
            sys.exit(0)
    else:
        venv_name = Path(sys.prefix).name
        print(f"  venv: {venv_name}")

    if not CONFIG_TEMPLATE.exists():
        print(f"  ERROR: Template not found: {CONFIG_TEMPLATE}")
        sys.exit(1)
    print(f"  Template: {CONFIG_TEMPLATE.name}")

    if CONFIG_OUTPUT.exists():
        print(f"\n  config.toml already exists.")
        if not ask_yes_no("Overwrite?", False):
            print("  Tip: python setup_wizard.py --validate")
            return

    # Load template
    content = CONFIG_TEMPLATE.read_text()

    # Core setup
    content, ok = setup_paths(content)
    if not ok:
        return

    content = setup_calendars(content)
    content = setup_claude(content)
    content, collector_time = setup_schedule(content)
    content = setup_classification(content)
    content = setup_dayblock(content)

    # Optional integrations
    print("\n--- Optional Integrations ---")
    content = setup_miniflux(content)
    content = setup_coolify(content)
    content = setup_cloudflare(content)
    content = setup_healthchecks(content)

    # Write config
    print("\n--- Writing config.toml ---")
    CONFIG_OUTPUT.write_text(content)
    print(f"  Written: {CONFIG_OUTPUT.name}")

    # Database
    init_database()

    # Logs directory
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)

    # Scheduling
    setup_launchd(collector_time)

    # Done
    print()
    print("=" * 50)
    print("  Setup complete!")
    print("=" * 50)
    print()
    print("  Next steps:")
    print("  1. Verify MCP connectors in Claude Code:")
    print("     /mcp  (test gmail_get_profile and gcal_list_calendars)")
    print("  2. Test the pipeline:")
    print("     ./cos-brief.sh run collect")
    print("  3. Validate config:")
    print("     python setup_wizard.py --validate")
    print()


if __name__ == "__main__":
    main()
