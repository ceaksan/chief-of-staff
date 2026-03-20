# Chief of Staff - Overnight Collection

You are collecting Gmail and Calendar data for the Chief of Staff system. Write all results to cos.db using the Python scripts in this project.

Working directory: `.`

## Instructions

### Step 1: Calendar Collection

Collect events for today and tomorrow from all configured calendars.

Use `gcal_list_events` for each calendar ID listed in `config.toml` under `[calendars] ids`.

Read the config first:
```bash
cd .
source .venv/bin/activate
python -c "from cos.config import load_config; c = load_config(); print('\n'.join(c['calendars']['ids']))"
```

Then call `gcal_list_events` for each returned calendar ID.

Parameters:
- timeMin: today at 00:00:00 (local time)
- timeMax: tomorrow at 23:59:59 (local time)
- timeZone: Europe/İstanbul

After collecting all events, write the raw MCP response data to a temp JSON file and run the collector:

```bash
cd .
mkdir -p .tmp
cat > .tmp/cos_events.json << 'EVENTS_EOF'
{
    "user@example.com": [... paste raw event objects from primary ...],
    "secondary@example.com": [... paste raw event objects ...],
    "other@example.com": [... paste raw event objects ...]
}
EVENTS_EOF
source .venv/bin/activate
python collectors/calendar_collector.py --json .tmp/cos_events.json
```

Use the raw event objects exactly as returned by gcal_list_events. Include all fields.

### Step 2: Gmail Collection

Search for actionable emails from the last 24 hours.

Use `gmail_search_messages` with query:
```
is:inbox newer_than:1d -category:promotions -category:social -category:updates
```

Set maxResults to 50.

Write the raw message objects to a temp JSON file and run the collector:

```bash
cd .
cat > .tmp/cos_emails.json << 'EMAILS_EOF'
[... paste raw message objects from gmail_search_messages ...]
EMAILS_EOF
source .venv/bin/activate
python collectors/gmail_collector.py --json .tmp/cos_emails.json
```

If no emails are found, skip this step and note it in the summary.

### Step 3: Render Daily Note

Generate the Obsidian Daily Note:

```bash
cd .
source .venv/bin/activate
python renderer.py
```

### Step 4: Cleanup

```bash
rm -f .tmp/cos_events.json .tmp/cos_emails.json
```

## Rules

- Do NOT send any emails. Collection only.
- Do NOT modify any calendar events.
- If a source fails (MCP auth expired, API error), continue with other sources.
- Log all errors. The renderer will show warnings for failed sources.
- Run each step sequentially. Do not skip steps.

## Expected Output

After all steps complete, print a summary:
```
Collection complete:
- Calendar: X events collected
- Gmail: X emails collected, X filtered
- Daily Note: written to <path>
```
