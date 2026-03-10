#!/bin/bash
# Chief of Staff - Manual Pipeline Runner
# Usage: ./run.sh [step]
#   ./run.sh          - run full pipeline
#   ./run.sh collect  - collection only
#   ./run.sh classify - classification only
#   ./run.sh sweep    - morning sweep only
#   ./run.sh render   - re-render daily note only
#   ./run.sh status   - show pipeline status

set -e
cd "$(dirname "$0")"

# Lock check
if ! /usr/bin/shlock -p $$ -f .lockfile; then
    echo "Another instance is running. Exiting."
    exit 1
fi
trap '/bin/rm -f .lockfile' EXIT

mkdir -p logs .tmp
source .venv/bin/activate

# Read a single config value: read_cfg claude.collector_budget 2.00
read_cfg() {
    CFG_KEY="$1" CFG_DEFAULT="$2" python -c "
import os
from cos.config import load_config
c = load_config()
v = c
for k in os.environ['CFG_KEY'].split('.'):
    v = v.get(k, {})
print(v if v != {} else os.environ.get('CFG_DEFAULT', ''))
"
}

STEP="${1:-full}"

run_collect() {
    local budget=$(read_cfg claude.collector_budget 2.00)
    local model=$(read_cfg claude.collector_model sonnet)

    echo "=== Step 1: Collection (Gmail + Calendar via MCP) ==="
    claude -p prompts/collect.md --budget "$budget" --model "$model" 2>> logs/claude-collect.log
    echo ""

    echo "=== Step 2: Feed + Health + Task Collection ==="
    python collectors/feed_collector.py
    python collectors/health_collector.py 2>/dev/null || echo "  (health collector skipped - no scripts configured)"
    python collectors/task_collector.py
    echo ""
}

run_classify() {
    local budget=$(read_cfg claude.classifier_budget 1.50)
    local model=$(read_cfg claude.classifier_model sonnet)

    echo "=== Step 3: Classification ==="
    claude -p prompts/classifier.md --budget "$budget" --model "$model" 2>> logs/claude-classifier.log
    echo ""
}

run_sweep() {
    local budget=$(read_cfg claude.sweep_budget 3.00)
    local model=$(read_cfg claude.sweep_model opus)

    echo "=== Step 4: Morning Sweep ==="
    claude -p prompts/sweep.md --budget "$budget" --model "$model" 2>> logs/claude-sweep.log
    echo ""
}

run_render() {
    echo "=== Rendering Daily Note ==="
    python renderer.py
    echo ""
}

run_status() {
    echo "=== Pipeline Status ==="
    if [ ! -f cos.db ]; then
        echo "No database found. Run './run.sh' to start."
        exit 0
    fi
    python -c "
import sqlite3, json
conn = sqlite3.connect('cos.db')
conn.row_factory = sqlite3.Row

# Work queue summary
rows = conn.execute('''
    SELECT status, COUNT(*) as count
    FROM work_queue
    WHERE collected_at >= datetime(\"now\", \"-1 day\")
    GROUP BY status ORDER BY count DESC
''').fetchall()
print('Work Queue (last 24h):')
for r in rows:
    print(f'  {r[\"status\"]}: {r[\"count\"]}')

# Classification summary
rows = conn.execute('''
    SELECT c.category, COUNT(*) as count
    FROM classifications c
    JOIN work_queue wq ON wq.id = c.queue_id
    WHERE wq.collected_at >= datetime(\"now\", \"-1 day\")
    GROUP BY c.category ORDER BY count DESC
''').fetchall()
if rows:
    print('Classifications (last 24h):')
    for r in rows:
        print(f'  {r[\"category\"]}: {r[\"count\"]}')

# Recent runs
rows = conn.execute('''
    SELECT layer, source, status, items_processed, started_at
    FROM runs ORDER BY started_at DESC LIMIT 5
''').fetchall()
if rows:
    print('Recent Runs:')
    for r in rows:
        src = f' ({r[\"source\"]})' if r['source'] else ''
        print(f'  {r[\"layer\"]}{src}: {r[\"status\"]} - {r[\"items_processed\"]} items @ {r[\"started_at\"]}')

conn.close()
"
}

run_cleanup() {
    DAYS="${1:-30}"
    echo "=== Cleanup (older than $DAYS days) ==="
    python -c "
from cos.db import connect, get_db_path, cleanup
from cos.config import load_config
config = load_config()
with connect(get_db_path(config)) as conn:
    stats = cleanup(conn, days=$DAYS)
    total = sum(stats.values())
    if total == 0:
        print('Nothing to clean up.')
    else:
        for k, v in stats.items():
            if v > 0:
                print(f'  {k}: {v} deleted')
        print(f'  Total: {total} records purged')
"
}

case "$STEP" in
    full)
        run_collect
        run_classify
        run_sweep
        run_render
        echo "=== Full pipeline complete ==="
        run_status
        ;;
    collect)
        run_collect
        ;;
    classify)
        run_classify
        ;;
    sweep)
        run_sweep
        ;;
    render)
        run_render
        ;;
    status)
        run_status
        ;;
    cleanup)
        run_cleanup "$2"
        ;;
    *)
        echo "Unknown step: $STEP"
        echo "Usage: ./run.sh [full|collect|classify|sweep|render|status|cleanup [days]]"
        exit 1
        ;;
esac
