#!/bin/bash
# Chief of Staff - Daily Brief
# Usage: cos (via alias)

COS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$COS_DIR"
source .venv/bin/activate

NOTE=$(python renderer.py --stdout 2>/dev/null)

if [ -z "$NOTE" ]; then
    echo "Daily note bos. Once pipeline calistir: ./run.sh"
    exit 1
fi

PROMPT_TEMPLATE="$COS_DIR/prompts/brief.md"

if [ ! -f "$PROMPT_TEMPLATE" ]; then
    echo "Error: prompt template not found: $PROMPT_TEMPLATE"
    exit 1
fi

python -c "
import os, sys
t = open(os.environ['PROMPT_TEMPLATE']).read()
n = sys.stdin.read()
print(t.replace('{{DAILY_NOTE}}', n))
" <<< "$NOTE" | claude -p -
