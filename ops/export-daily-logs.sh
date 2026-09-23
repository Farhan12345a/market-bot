#!/usr/bin/env bash
# ops/export-daily-logs.sh — pulls everything a later analysis session
# needs for one trading day into logs/daily/<date>/, commits and pushes.
# Read-only against the live logs/journal; only ever adds files under
# logs/daily/, never touches the originals.
set -euo pipefail
cd "$(dirname "$0")/.."

DATE="${1:-$(date +%F)}"
OUT="logs/daily/$DATE"
mkdir -p "$OUT"

# 1. Every CSV log's rows for this date. Match the date as either the first
#    column (trade_history.csv, signal_journal.csv both start with `date`) or
#    a later column preceded by a comma (trade_paths.csv col 3, trade_context.csv
#    col 2) - a plain ",$DATE," alone silently misses the first-column case
#    and came back header-only for both of those files on 2026-09-21 despite
#    real trades existing that day.
for f in trade_history trade_paths signal_journal trade_context short_signal_journal; do
  src="logs/${f}.csv"
  [ -f "$src" ] || continue
  { head -1 "$src"; grep -E "^$DATE,|,$DATE," "$src" || true; } > "$OUT/${f}.csv"
done

# 2. The service log for market hours that day (09:00-17:00 ET = 13:00-21:00 UTC).
#    Named .txt, not .log - the whole logs/ tree (including *.log) is
#    gitignored on purpose, so this file avoids that extension too.
journalctl -u market-bot --since "$DATE 13:00:00" --until "$DATE 21:00:00" \
  --no-pager > "$OUT/service-log.txt" 2>/dev/null || true

# 3. Refresh the multi-day rollup so it reflects today too
./venv/bin/python3 ops/session-metrics.py --write >/dev/null 2>&1 || true

# -f is required, not optional: logs/ is a directory-level gitignore rule,
# and git will not descend into an ignored directory to honor a negation
# pattern for its contents (a documented git limitation, confirmed against
# this repo - a `!logs/daily/**` gitignore exception does NOT work here).
# -f is the only thing that actually stages files under logs/.
git add -f "$OUT" ANALYSIS_LOG.md
if ! git diff --cached --quiet; then
  git commit -m "Daily log export for $DATE"
  git push
else
  echo "Nothing new for $DATE"
fi
