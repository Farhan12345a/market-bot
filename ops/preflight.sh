#!/usr/bin/env bash
#
# Post-deploy validation. Run on the Droplet AFTER ops/deploy.sh, especially
# before a session nobody will be watching.
#
#     cd /root/market-bot && bash ops/preflight.sh
#
# Read-only: starts nothing, stops nothing, sends nothing (unless you pass
# --notify, which delivers one test message through the real path).

cd "$(dirname "$0")/.." || exit 1
export SYSTEMD_PAGER=cat
export PAGER=cat
PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[92mOK\033[0m    %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[91mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[93mWARN\033[0m  %s\n' "$*"; WARN=$((WARN+1)); }
sec()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

sec "1. Service"
if systemctl is-active --quiet market-bot; then ok "market-bot.service is active"
else bad "market-bot.service is NOT active - run: systemctl start market-bot"; fi

# The account allows ONE data websocket, so a second process is not a
# duplicate-work problem, it is a "neither one gets live prices" problem.
NPROC=$(pgrep -f "python.*src.main" 2>/dev/null | wc -l | tr -d " ")
case "$NPROC" in
  1) ok "exactly one bot process running" ;;
  0) bad "no bot process found (service claims active?)" ;;
  *) bad "$NPROC bot processes running - they will fight over the single data websocket. Kill the extras: pkill -f 'python.*src.main' then systemctl start market-bot" ;;
esac

sec "2. Code is current"
git fetch origin main -q 2>/dev/null
LOCAL=$(git rev-parse HEAD 2>/dev/null); REMOTE=$(git rev-parse origin/main 2>/dev/null)
if [ "$LOCAL" = "$REMOTE" ]; then ok "at origin/main (${LOCAL:0:7})"
else warn "local ${LOCAL:0:7} != origin/main ${REMOTE:0:7} - run ops/deploy.sh"; fi
STARTED=$(systemctl show market-bot -p ActiveEnterTimestamp --value)
ok "service started: ${STARTED:-unknown}"

sec "3. Interpreter and dependencies"
PY=$(systemctl --no-pager cat market-bot 2>/dev/null | grep -m1 '^ExecStart=' | tr ' ' '\n' | grep -m1 -E 'python3?$')
[ -x "$PY" ] || PY=./venv/bin/python3
if [ -x "$PY" ]; then ok "interpreter: $PY ($($PY --version 2>&1))"
else bad "cannot find the service's interpreter"; fi
for m in alpaca requests yaml pandas pytz; do
  if "$PY" -c "import $m" >/dev/null 2>&1; then ok "$m importable"; else bad "$m MISSING - $PY -m pip install -r requirements.txt"; fi
done

sec "4. Settings that will trade"
if [ ! -x "$PY" ]; then bad "no usable interpreter - skipping settings dump"; else
"$PY" - <<'PY'
import yaml, sys
t = yaml.safe_load(open("config.yaml"))["trading"]
n = yaml.safe_load(open("config.yaml"))["notifications"]
for k in ("entry_window_start","screener_start_time","list_builder_start_time",
          "max_concurrent_positions","max_daily_entries","max_daily_loss_usd",
          "min_stock_price","max_stock_price","reentry_cooldown_minutes",
          "use_websocket_stream","stream_max_subscriptions","use_trade_ticks_for_entry",
          "merge_default_universe","num_stocks_to_trade","use_take_profit"):
    print(f"  ..    {k:28} {t.get(k)}")
for tier in t.get("take_profit_tiers", []):
    print(f"  ..    take-profit tier            +{tier['gain_pct']}% -> {tier['sell_fraction']*100:.0f}%")
budget = t["stream_max_subscriptions"] // (2 if t.get("use_trade_ticks_for_entry") else 1)
print(f"  ..    stream budget                {budget} symbols")
print(f"  ..    report times                 {n.get('report_times')}")
PY
fi

sec "5. Notification channels"
if [ -f /etc/market-bot.env ]; then
  ok "/etc/market-bot.env exists ($(stat -c %a /etc/market-bot.env) perms)"
  grep -q RESEND_API_KEY /etc/market-bot.env && ok "RESEND_API_KEY present in env file" || warn "no RESEND_API_KEY in env file"
else warn "/etc/market-bot.env missing - notifications will not send"; fi
systemctl --no-pager cat market-bot 2>/dev/null | grep -q EnvironmentFile \
  && ok "systemd loads the env file" \
  || bad "systemd has no EnvironmentFile - the key will NOT reach the process (see ops/NOTIFICATIONS.md)"
journalctl -u market-bot --since "1 hour ago" 2>/dev/null | grep -q "Notification channels active" \
  && ok "startup log confirms a live channel: $(journalctl -u market-bot --since '1 hour ago' | grep -o 'Notification channels active.*' | tail -1)" \
  || warn "no 'Notification channels active' line in the last hour of logs"

sec "6. Disk and logs"
AVAIL=$(df -m / | awk 'NR==2 {print $4}')
[ "$AVAIL" -gt 500 ] && ok "${AVAIL}MB free" || bad "only ${AVAIL}MB free - logs and reports may fail to write"
[ -w logs ] && ok "logs/ writable" || bad "logs/ not writable"
mkdir -p logs/reports 2>/dev/null && [ -w logs/reports ] && ok "logs/reports/ writable" || bad "logs/reports/ not writable"
LAST=$(stat -c %Y logs/trading.log 2>/dev/null || echo 0)
AGE=$(( ($(date +%s) - LAST) / 60 ))
[ "$LAST" -gt 0 ] && ok "trading.log last written ${AGE} min ago" || warn "no logs/trading.log yet"

sec "7. Clock"
ok "UTC:  $(date -u '+%Y-%m-%d %H:%M:%S')"
ok "ET:   $(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
DOW=$(TZ=America/New_York date +%u)
[ "$DOW" -le 5 ] && ok "today is a weekday in ET" || warn "weekend in ET - no session expected"

sec "8. Recent errors"
ERRS=$(journalctl -u market-bot --since "24 hours ago" 2>/dev/null | grep -c "ERROR" | tr -d " ")
ERRS=${ERRS:-0}
[ "$ERRS" -eq 0 ] && ok "no ERROR lines in the last 24h" || warn "$ERRS ERROR line(s) in the last 24h:"
[ "$ERRS" -gt 0 ] && journalctl -u market-bot --since "24 hours ago" | grep "ERROR" | tail -5 | sed 's/^/        /'

if [ "$1" = "--notify" ]; then
  sec "9. Live notification test"
  set -a; [ -f /etc/market-bot.env ] && . /etc/market-bot.env; set +a
  "$PY" ops/test-notifications.py && ok "test notification delivered" || bad "test notification FAILED"
fi

printf '\n\033[1m%s\033[0m\n' "-------------------------------------------------"
printf '%d OK, %d WARN, %d FAIL\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then printf '\033[91mNOT READY - fix the FAIL items above.\033[0m\n'; exit 1; fi
[ "$WARN" -gt 0 ] && printf '\033[93mReady, with warnings worth reading.\033[0m\n' || printf '\033[92mReady.\033[0m\n'
exit 0
