#!/bin/bash
# Daily sales-velocity pull: writes the PREVIOUS complete UTC day's per-channel
# velocity to data/sales-velocity/daily/daily-velocity_YYYY-MM-DD.xlsx (+ .csv).
#
# Run by the launchd agent com.based.daily-velocity (or by hand). Reuses the
# main daily_sales_velocity.py with a 1-day window, then renames the output to
# the friendly daily name and drops the per-day checkpoint. Days are UTC, to
# match the 6-month report.
set -euo pipefail

# ROOT defaults to the repo root (two levels up from this script) so the job
# runs both locally (launchd) and inside the Render container; override via env.
ROOT="${VELOCITY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# PYBIN defaults to the local venv; Render (no venv) sets PYBIN=python.
PYBIN="${PYBIN:-$ROOT/.venv/bin/python}"
SCRIPT="$ROOT/scripts/daily_sales_velocity.py"
OUTDIR="$ROOT/data/sales-velocity/daily"
LOGDIR="$OUTDIR/logs"
mkdir -p "$OUTDIR" "$LOGDIR"

# Target = yesterday in UTC (a complete calendar day). Override by passing a
# YYYY-MM-DD as $1 (handy for backfilling a missed day). Date math works on
# both GNU date (Linux/Render) and BSD date (macOS/launchd).
DAY="${1:-$(date -u -d '1 day ago' +%Y-%m-%d 2>/dev/null || date -u -v-1d +%Y-%m-%d)}"
LOG="$LOGDIR/daily-velocity_${DAY}.log"
SLACK_CHANNEL_ID="${SLACK_CHANNEL:-C0AK6UGA1NJ}"

# Alert to Slack on ANY non-zero exit so a broken run never goes silent again
# (failed pull, no xlsx, etc.). NOTE: this cannot cover the script-file-missing
# case itself (the trap lives inside the script) -- that recurrence is prevented
# by committing this toolchain to git so a stash/clean can't delete it.
notify_failure() {
  local code=$?
  [ "$code" -eq 0 ] && return 0
  local tok
  tok="$(grep -E '^SLACK_BOT_TOKEN=' "$ROOT/.env" 2>/dev/null | cut -d= -f2-)"
  [ -n "$tok" ] && curl -fsS -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $tok" \
    -H 'Content-type: application/json; charset=utf-8' \
    --data "$(printf '{"channel":"%s","text":":warning: Daily velocity job FAILED for %s (exit %s). Log: %s"}' "$SLACK_CHANNEL_ID" "$DAY" "$code" "$LOG")" \
    >/dev/null 2>&1 || true
}
trap notify_failure EXIT

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') :: pulling $DAY (UTC day) ===" >> "$LOG"

"$PYBIN" "$SCRIPT" \
  --start-date "$DAY" --end-date "$DAY" \
  --out-dir "$OUTDIR" --fresh >> "$LOG" 2>&1

# Rename the window-slug outputs to the friendly daily name; drop the checkpoint.
SLUG="daily-sales-velocity_${DAY}_${DAY}"
for ext in xlsx csv README.txt; do
  if [ -f "$OUTDIR/$SLUG.$ext" ]; then
    mv -f "$OUTDIR/$SLUG.$ext" "$OUTDIR/daily-velocity_${DAY}.$ext"
  fi
done
rm -f "$OUTDIR/$SLUG.checkpoint.json"

if [ -f "$OUTDIR/daily-velocity_${DAY}.xlsx" ]; then
  echo "OK -> $OUTDIR/daily-velocity_${DAY}.xlsx" >> "$LOG"
else
  echo "FAILED: no xlsx produced for $DAY" >> "$LOG"
  exit 1
fi

# Post the workbook + a summary to Slack. A Slack failure does NOT fail the run
# (the file is already on disk); it's just logged.
if "$PYBIN" "$ROOT/scripts/post_velocity_to_slack.py" \
     --xlsx "$OUTDIR/daily-velocity_${DAY}.xlsx" \
     --day "$DAY" --channel "$SLACK_CHANNEL_ID" >> "$LOG" 2>&1; then
  echo "Slack: posted to $SLACK_CHANNEL_ID" >> "$LOG"
else
  echo "Slack: post FAILED (file still saved locally)" >> "$LOG"
fi
