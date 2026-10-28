#!/bin/bash
# Daily sales-velocity pull: writes the PREVIOUS complete UTC day's per-channel
# velocity to data/sales-velocity/daily/daily-velocity_YYYY-MM-DD.xlsx (+ .csv).
#
# Run by the Render cron 'based-inventory-velocity' (schedule 0 11 * * *; see
# render.yaml); also runnable locally by hand. Reuses the
# main daily_sales_velocity.py with a 1-day window, then renames the output to
# the friendly daily name and drops the per-day checkpoint. Days are UTC, to
# match the 6-month report.
set -euo pipefail

# ROOT defaults to the repo root (two levels up from this script) so the job
# runs inside the Render container (the scheduler) and locally by hand; override via env.
ROOT="${VELOCITY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# PYBIN defaults to the local venv; Render (no venv) sets PYBIN=python.
PYBIN="${PYBIN:-$ROOT/.venv/bin/python}"
SCRIPT="$ROOT/scripts/daily_sales_velocity.py"
OUTDIR="$ROOT/data/sales-velocity/daily"
LOGDIR="$OUTDIR/logs"
mkdir -p "$OUTDIR" "$LOGDIR"

# Target = yesterday in UTC (a complete calendar day). Override by passing a
# YYYY-MM-DD as $1 (handy for backfilling a missed day). Date math works on
# both GNU date (Linux/Render) and BSD date (macOS).
DAY="${1:-$(date -u -d '1 day ago' +%Y-%m-%d 2>/dev/null || date -u -v-1d +%Y-%m-%d)}"
LOG="$LOGDIR/daily-velocity_${DAY}.log"
SLACK_CHANNEL_ID="${SLACK_CHANNEL:-C0AK6UGA1NJ}"

# Keep the local log, and stream the same output to Render so errors survive
# the cron container. Flush Python output while a long fetch is still running.
exec > >(tee -a "$LOG") 2>&1
export PYTHONUNBUFFERED=1

# Alert to Slack on ANY non-zero exit so a broken run never goes silent again
# (failed pull, no xlsx, etc.). NOTE: this cannot cover the script-file-missing
# case itself (the trap lives inside the script) -- that recurrence is prevented
# by committing this toolchain to git so a stash/clean can't delete it.
notify_failure() {
  local code=$?
  trap - EXIT
  [ "$code" -eq 0 ] && return 0
  echo "FAILED: daily velocity for $DAY (exit $code). Log: $LOG"
  # Render injects credentials into the environment; .env is only a local
  # fallback. A missing file or failed alert must never replace the real exit.
  local tok="${SLACK_BOT_TOKEN:-}"
  if [ -z "$tok" ] && [ -f "$ROOT/.env" ]; then
    tok="$(grep -E '^SLACK_BOT_TOKEN=' "$ROOT/.env" 2>/dev/null | cut -d= -f2- || true)"
  fi
  if [ -z "$tok" ]; then
    echo "Slack: failure alert skipped (SLACK_BOT_TOKEN is not configured)"
  else
    local response
    if ! response="$(curl -fsS --connect-timeout 5 --max-time 15 \
      -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $tok" \
      --data-urlencode "channel=$SLACK_CHANNEL_ID" \
      --data-urlencode "text=:warning: Daily velocity job FAILED for $DAY (exit $code). Log: $LOG")"; then
      echo "Slack: failure alert request failed"
    elif ! "$PYBIN" -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)' <<< "$response"; then
      echo "Slack: failure alert rejected or returned an invalid response"
    fi
  fi
  exit "$code"
}
trap notify_failure EXIT

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') :: pulling $DAY (UTC day) ==="

"$PYBIN" "$SCRIPT" \
  --start-date "$DAY" --end-date "$DAY" \
  --out-dir "$OUTDIR" --fresh

# Rename the window-slug outputs to the friendly daily name; drop the checkpoint.
SLUG="daily-sales-velocity_${DAY}_${DAY}"
for ext in xlsx csv bundles.csv README.txt; do
  if [ -f "$OUTDIR/$SLUG.$ext" ]; then
    mv -f "$OUTDIR/$SLUG.$ext" "$OUTDIR/daily-velocity_${DAY}.$ext"
  fi
done
rm -f "$OUTDIR/$SLUG.checkpoint.json"

if [ -f "$OUTDIR/daily-velocity_${DAY}.xlsx" ]; then
  echo "OK -> $OUTDIR/daily-velocity_${DAY}.xlsx"
else
  echo "FAILED: no xlsx produced for $DAY"
  exit 1
fi

# Post the workbook + a summary to Slack. A Slack failure does NOT fail the run
# (the file is already on disk); it's just logged.
if "$PYBIN" "$ROOT/scripts/post_velocity_to_slack.py" \
     --xlsx "$OUTDIR/daily-velocity_${DAY}.xlsx" \
     --day "$DAY" --channel "$SLACK_CHANNEL_ID"; then
  echo "Slack: posted to $SLACK_CHANNEL_ID"
else
  echo "Slack: post FAILED (file still saved locally)"
fi
