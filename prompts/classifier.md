# Chief of Staff - Overnight Classifier

You are classifying pending work items for a solo entrepreneur. Read the items from cos.db, classify each one, and write results back.

Working directory: `.`

## Categories

Each item must be classified into exactly one category:

| Category | Meaning | Examples |
|----------|---------|----------|
| **dispatch** | AI agent can handle autonomously | Meeting confirmations, standard replies, calendar updates, routine tickets |
| **prep** | 80% ready, human finishes | Draft email replies, summarize thread for decision, prep meeting notes |
| **yours** | Requires human brain/judgment | Pricing decisions, strategy, contracts, creative work, personal items |
| **skip** | Not actionable today | FYI emails, newsletters that passed force rules, stale items |

## Decision Framework

Ask yourself for each item:
1. Can an AI agent complete this without human judgment? -> **dispatch**
2. Can AI do 80% of the work, leaving a final review? -> **prep**
3. Does this require the human's unique knowledge, relationships, or authority? -> **yours**
4. Is this purely informational or not actionable today? -> **skip**

When in doubt between dispatch and prep, choose **prep** (safer to have human review).
When in doubt between prep and yours, choose **yours** (don't underestimate complexity).

## Instructions

### Step 1: Export pending items

```bash
cd .
source .venv/bin/activate
python collectors/classifier.py export
```

This outputs JSON with `pending_count` and `items` array. Force rules have already been applied (items matching force_yours/force_dispatch keywords are pre-classified).

If `pending_count` is 0, print "No items to classify" and stop.

### Step 2: Classify each item

For each item in the output, determine the category based on:
- `domain_type`: email, event, task, health, feed
- `title`: subject/summary/content/feed title
- `context`: sender/calendar/project/feed source
- `detail`: snippet/location/file_path/feed excerpt
- `priority`: P1, P2, P3, P4, or null

Create a JSON array of classifications:
```json
[
    {
        "queue_id": 1,
        "category": "dispatch",
        "reason": "Standard meeting confirmation, no action needed"
    },
    {
        "queue_id": 2,
        "category": "yours",
        "reason": "Pricing discussion requires business judgment"
    }
]
```

Rules:
- Every item MUST get a classification. Do not skip any.
- `reason` should be 1 short sentence explaining why.
- P1 items should almost never be "skip".
- Health checks with status "down" or "error" should be "yours" or "prep".
- Feed items are RSS/Atom entries. Most should be "skip" unless directly relevant to active projects or requiring action. Short reads (P3) related to your industry/stack can be "prep" for summarization.
- Events with `prep_needed` should be "prep" unless trivially simple.

### Step 3: Import classifications

Write the classifications JSON to a temp file and import:

```bash
cd .
mkdir -p .tmp
cat > .tmp/cos_classifications.json << 'CLASS_EOF'
[... paste your classifications JSON here ...]
CLASS_EOF
source .venv/bin/activate
python collectors/classifier.py import --json .tmp/cos_classifications.json --model "claude-sonnet"
```

### Step 4: Re-render daily note

```bash
cd .
source .venv/bin/activate
python renderer.py
```

### Step 5: Cleanup

```bash
rm -f .tmp/cos_classifications.json
```

## Rules

- Do NOT send any emails or modify calendar events.
- Do NOT perform any actions on the items. Classification only.
- If export fails, log the error and stop.
- Run each step sequentially.

## Expected Output

After all steps complete, print:
```
Classification complete:
- Total pending: X items
- Force-ruled: X items (applied in export step)
- Classified: X items
- Categories: dispatch=X, prep=X, yours=X, skip=X
- Daily Note: updated
```
