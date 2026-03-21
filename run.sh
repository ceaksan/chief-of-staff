#!/bin/bash
# Chief of Staff - Manual Pipeline Runner
# Usage: ./run.sh [step]
#   ./run.sh          - run full pipeline
#   ./run.sh collect  - collection only
#   ./run.sh classify - classification only
#   ./run.sh sweep    - morning sweep only
#   ./run.sh render   - re-render daily note only
#   ./run.sh status   - show pipeline status
#   ./run.sh weekly   - weekly stats digest
#   ./run.sh insights - scheduling insights (manual adaptive)

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

# Healthchecks.io ping helper
hc_ping() {
    local url
    url=$(read_cfg "healthchecks.${1}_url" "")
    [ -z "$url" ] && return 0
    local suffix="${2:-}"
    curl -fsS -m 10 --retry 3 "${url}${suffix}" >/dev/null 2>&1 || true
}

# macOS notification on failure
notify_failure() {
    local msg="$1"
    osascript -e "display notification \"$msg\" with title \"Chief of Staff\" sound name \"Basso\"" 2>/dev/null || true
}

STEP="${1:-full}"

run_collect() {
    echo "=== Step 1: Feed + Health + Task Collection ==="
    if python collectors/feed_collector.py; then
        hc_ping feed
    else
        hc_ping feed /fail
    fi
    python collectors/health_collector.py 2>/dev/null || echo "  (health collector skipped - no scripts configured)"
    python collectors/task_collector.py
    echo ""
}

run_classify() {
    local budget=$(read_cfg claude.classifier_budget 1.50)
    local model=$(read_cfg claude.classifier_model sonnet)

    echo "=== Step 3: Classification ==="
    claude -p prompts/classifier.md --max-budget-usd "$budget" --model "$model" 2>> logs/claude-classifier.log
    echo ""
}

run_sweep() {
    hc_ping sweep /start
    echo "=== Step 4: Morning Sweep (Orchestrator) ==="
    if python collectors/orchestrator.py 2>> logs/claude-sweep.log; then
        hc_ping sweep
    else
        hc_ping sweep /fail
    fi
    echo ""
}

run_sweep_seq() {
    hc_ping sweep /start
    echo "=== Step 4: Morning Sweep (Sequential) ==="
    if python collectors/orchestrator.py --sequential 2>> logs/claude-sweep.log; then
        hc_ping sweep
    else
        hc_ping sweep /fail
    fi
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

run_weekly() {
    echo "=== Weekly Stats Digest ==="
    python -c "
from cos.db import connect, get_db_path, init_db
from cos.config import load_config

config = load_config()
db_path = get_db_path(config)
init_db(db_path)

with connect(db_path) as conn:
    # Items collected by source
    rows = conn.execute('''
        SELECT domain_type, COUNT(*) as cnt
        FROM work_queue
        WHERE collected_at >= datetime('now', '-7 days')
        GROUP BY domain_type ORDER BY cnt DESC
    ''').fetchall()
    total = sum(r['cnt'] for r in rows)
    print(f'Items collected (7d): {total}')
    for r in rows:
        print(f'  {r[\"domain_type\"]}: {r[\"cnt\"]}')

    # Classification breakdown
    rows = conn.execute('''
        SELECT c.category, COUNT(*) as cnt
        FROM classifications c
        JOIN work_queue wq ON wq.id = c.queue_id
        WHERE wq.collected_at >= datetime('now', '-7 days')
        GROUP BY c.category ORDER BY cnt DESC
    ''').fetchall()
    if rows:
        print('Classifications (7d):')
        for r in rows:
            print(f'  {r[\"category\"]}: {r[\"cnt\"]}')

    # Run stats
    rows = conn.execute('''
        SELECT layer, COUNT(*) as runs,
               SUM(items_processed) as processed,
               SUM(items_failed) as failed,
               ROUND(SUM(budget_used), 2) as budget
        FROM runs
        WHERE started_at >= datetime('now', '-7 days')
        GROUP BY layer
    ''').fetchall()
    if rows:
        print('Pipeline runs (7d):')
        total_budget = 0
        for r in rows:
            failed = f', {r[\"failed\"]} failed' if r['failed'] else ''
            budget = r['budget'] or 0
            total_budget += budget
            print(f'  {r[\"layer\"]}: {r[\"runs\"]} runs, {r[\"processed\"]} items{failed} (\${budget})')
        print(f'  Total budget: \${total_budget:.2f}')

    # Failure rate
    total_runs = conn.execute('''
        SELECT COUNT(*) FROM runs WHERE started_at >= datetime('now', '-7 days')
    ''').fetchone()[0]
    failed_runs = conn.execute('''
        SELECT COUNT(*) FROM runs
        WHERE started_at >= datetime('now', '-7 days') AND status IN ('failed', 'partial')
    ''').fetchone()[0]
    if total_runs > 0:
        rate = (failed_runs / total_runs) * 100
        print(f'Failure rate: {failed_runs}/{total_runs} ({rate:.0f}%)')
"
    hc_ping weekly
}

run_insights() {
    echo "=== Scheduling Insights ==="
    python -c "
from cos.db import connect, get_db_path, init_db
from cos.config import load_config

config = load_config()
db_path = get_db_path(config)
init_db(db_path)

with connect(db_path) as conn:
    # Volume by day of week
    rows = conn.execute('''
        SELECT
            CASE CAST(strftime('%w', collected_at) AS INTEGER)
                WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue'
                WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri'
                WHEN 6 THEN 'Sat'
            END as day,
            COUNT(*) as cnt
        FROM work_queue
        WHERE collected_at >= datetime('now', '-30 days')
        GROUP BY strftime('%w', collected_at)
        ORDER BY strftime('%w', collected_at)
    ''').fetchall()
    if rows:
        max_cnt = max(r['cnt'] for r in rows)
        print('Volume by day of week (30d):')
        for r in rows:
            bar = '#' * int((r['cnt'] / max_cnt) * 20)
            print(f'  {r[\"day\"]}: {r[\"cnt\"]:>4} {bar}')

    # Volume by source per day of week
    rows = conn.execute('''
        SELECT domain_type,
            CASE CAST(strftime('%w', collected_at) AS INTEGER)
                WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue'
                WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri'
                WHEN 6 THEN 'Sat'
            END as day,
            COUNT(*) as cnt
        FROM work_queue
        WHERE collected_at >= datetime('now', '-30 days')
        GROUP BY domain_type, strftime('%w', collected_at)
        ORDER BY domain_type, strftime('%w', collected_at)
    ''').fetchall()
    if rows:
        print('Source breakdown by day:')
        current_source = None
        for r in rows:
            if r['domain_type'] != current_source:
                current_source = r['domain_type']
                print(f'  {current_source}:')
            print(f'    {r[\"day\"]}: {r[\"cnt\"]}')

    # Average run duration by layer
    rows = conn.execute('''
        SELECT layer,
            ROUND(AVG(
                (julianday(finished_at) - julianday(started_at)) * 86400
            ), 1) as avg_sec
        FROM runs
        WHERE finished_at IS NOT NULL
            AND started_at >= datetime('now', '-30 days')
        GROUP BY layer
    ''').fetchall()
    if rows:
        print('Avg run duration (30d):')
        for r in rows:
            print(f'  {r[\"layer\"]}: {r[\"avg_sec\"]}s')

    # Suggestion
    print()
    print('Recommendation:')
    print('  Review the day-of-week pattern above.')
    print('  If weekends are near-zero, consider skipping Sat/Sun in launchd.')
    print('  Adjust com.ceaksan.chief-of-staff.plist StartCalendarInterval accordingly.')
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
        hc_ping pipeline /start
        if run_collect && run_classify && run_render; then
            echo "=== Overnight pipeline complete (sweep pending your review) ==="
            run_status
            hc_ping pipeline
        else
            notify_failure "Pipeline failed. Check logs."
            hc_ping pipeline /fail
            exit 1
        fi
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
    sweep-seq)
        run_sweep_seq
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
    weekly)
        run_weekly
        ;;
    insights)
        run_insights
        ;;
    *)
        echo "Unknown step: $STEP"
        echo "Usage: ./run.sh [full|collect|classify|sweep|sweep-seq|render|status|weekly|insights|cleanup [days]]"
        exit 1
        ;;
esac
