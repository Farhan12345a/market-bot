#!/usr/bin/env bash
# Is the bot actually running when it should be?
#
# THE GAP THIS FILLS. Every alert the bot sends is sent BY the bot. A process
# that died, never started, or was left disabled after a deploy sends nothing -
# and silence is indistinguishable from a healthy quiet morning. That is the
# one failure the bot cannot report on itself, which is why this lives outside
# it and runs from cron.
#
# Checks, in order of how badly you want to know:
#   1. the systemd unit is active
#   2. it has logged something recently (active but wedged is still broken)
#   3. the machine has disk and memory left
#
# Sends ONE alert per problem per run, via the same Pushover/Resend channels
# the bot uses, and stays SILENT when everything is fine - a watchdog that
# reports success every morning is a watchdog you stop reading.
#
# Install (as root on the Droplet):
#   crontab -e
#   # weekdays, every 5 min from 09:20 to 16:05 ET (adjust for UTC offset)
#   */5 13-20 * * 1-5 /root/market-bot/ops/watchdog.sh >/dev/null 2>&1
#
# Or use the systemd timer in ops/systemd/ - see the README block there.
set -u

UNIT="${WATCHDOG_UNIT:-market-bot}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MAX_LOG_AGE_MIN="${WATCHDOG_MAX_LOG_AGE_MIN:-15}"
MIN_DISK_PCT="${WATCHDOG_MIN_DISK_PCT:-10}"

cd "$REPO" || exit 0

# Same interpreter detection deploy.sh and runall.sh use - a bare python3 has
# none of the bot's dependencies and would fail on import, turning a healthy
# morning into a false alarm.
PY=""
# shellcheck source=ops/_python.sh
if [ -f "$REPO/ops/_python.sh" ]; then
  # _python.sh runs under `set -u` here, so seed PY first - an unset variable
  # inside it would abort the watchdog, which is the one script that must never
  # fail silently.
  . "$REPO/ops/_python.sh" || true
fi
[ -n "${PY:-}" ] && [ -x "${PY:-}" ] || PY="$REPO/venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

set -a; [ -f /etc/market-bot.env ] && . /etc/market-bot.env; set +a

PROBLEMS=""
add() { PROBLEMS="${PROBLEMS}$1
"; }

# ---- 1. is the unit up? --------------------------------------------------
STATE="$(systemctl is-active "$UNIT" 2>/dev/null || true)"
if [ "$STATE" != "active" ]; then
  DETAIL="$(systemctl status "$UNIT" --no-pager -n 5 2>/dev/null | tail -6 | tr '\n' ' ')"
  add "SERVICE NOT RUNNING: systemctl is-active ${UNIT} says '${STATE:-unknown}'. ${DETAIL}"
fi

# ---- 2. active but wedged? -----------------------------------------------
# A process can hold its unit "active" while its loop is stuck. The journal is
# the cheapest liveness proof there is.
if [ "$STATE" = "active" ]; then
  LAST="$(journalctl -u "$UNIT" -n 1 --output=short-unix 2>/dev/null | awk '{print $1}' | cut -d. -f1)"
  if [ -n "${LAST:-}" ] && [ "$LAST" -gt 0 ] 2>/dev/null; then
    AGE_MIN=$(( ( $(date +%s) - LAST ) / 60 ))
    if [ "$AGE_MIN" -gt "$MAX_LOG_AGE_MIN" ]; then
      add "SERVICE IS WEDGED: the unit is active but has logged nothing for ${AGE_MIN} minutes (limit ${MAX_LOG_AGE_MIN}). The process is up and its loop is not."
    fi
  else
    add "NO JOURNAL OUTPUT at all for ${UNIT} - it may never have started."
  fi
fi

# ---- 3. the box itself ---------------------------------------------------
# A full disk stops the journal, the CSVs and the reports without stopping the
# process, so it presents as "everything looks fine" right up until the data is
# gone.
AVAIL_PCT="$(df --output=pcent "$REPO" 2>/dev/null | tail -1 | tr -d ' %')"
if [ -n "${AVAIL_PCT:-}" ] && [ "$AVAIL_PCT" -gt $((100 - MIN_DISK_PCT)) ] 2>/dev/null; then
  add "DISK ${AVAIL_PCT}% FULL on $(df -h "$REPO" | tail -1 | awk '{print $1}') - logs and trade CSVs will start failing to write."
fi

# ---- report --------------------------------------------------------------
[ -z "$PROBLEMS" ] && exit 0

printf '%s\n' "$PROBLEMS"
"$PY" - "$REPO" "$PROBLEMS" <<'PYEOF' 2>/dev/null || true
import os, sys
# The repo path is passed in rather than derived: this runs as stdin, so
# __file__ does not exist and cwd is whatever cron happened to give us.
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
try:
    import yaml
    from src.notifications.email_notifier import EmailNotifier
    from src.notifications import alerts
    config = yaml.safe_load(open("config.yaml"))
    body = sys.argv[2] if len(sys.argv) > 2 else "unspecified"
    alerts.degraded(
        config, EmailNotifier(config),
        what="watchdog: the bot is not healthy",
        detail=body + "\n\nThis came from ops/watchdog.sh, which runs OUTSIDE "
                      "the bot. A dead process cannot alert about itself, so "
                      "this is the only check that survives the bot being gone.",
    )
    print("watchdog alert sent")
except Exception as e:
    print(f"watchdog alert FAILED: {type(e).__name__}: {e}", file=sys.stderr)
PYEOF
exit 1
