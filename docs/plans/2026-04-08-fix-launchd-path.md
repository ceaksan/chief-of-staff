# Fix: cos-feed & cos-pipeline Health Checks DOWN

**Date:** 2026-04-08
**Status:** Pending
**Impact:** Pipeline not running since 2026-03-20 (19 days)

## Problem

Chief of Staff launchd job (`com.ceaksan.chief-of-staff`) fails because `claude` CLI is not in the launchd PATH.

- `claude` lives at `/Users/ceair/.local/bin/claude`
- Plist PATH: `/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin`
- `claude` is called in `run_collect()` (cos-brief.sh:100) and `run_classify()` (cos-brief.sh:119)
- Pipeline fails silently, no Healthchecks.io pings fire

Last successful run: 2026-03-20 (launchd-stdout.log).
LastExitStatus: 19968 (exit code 78).

## Root Cause

The plist `EnvironmentVariables.PATH` does not include `/Users/ceair/.local/bin`.

## Fix

**File:** `~/Library/LaunchAgents/com.ceaksan.chief-of-staff.plist`

Update PATH from:
```
/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin
```
to:
```
/Users/ceair/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin
```

Then reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.ceaksan.chief-of-staff.plist
launchctl load ~/Library/LaunchAgents/com.ceaksan.chief-of-staff.plist
```

## Also update: repo template plist

The repo's `com.chief-of-staff.overnight.plist` still has placeholder paths (`/path/to/chief-of-staff/`). Consider updating it to match the actual deployed plist or adding a note that it's a template.

## Verification

1. Run `./cos-brief.sh run` manually (uses claude with budget, needs approval)
2. Check `logs/launchd-stdout.log` for fresh output
3. Confirm Healthchecks.io dashboard shows cos-feed and cos-pipeline as UP

## Notes

- The weekly job (`com.ceaksan.chief-of-staff.weekly`) works because `run_weekly()` only uses Python, not `claude` CLI
- `mise` shims are also not in the launchd PATH but not needed here since venv is activated directly
