#!/usr/bin/env bash
#
# Pull, verify, restart, confirm. Run on the Droplet from /root/market-bot.
#
# The point of this script is the ORDER. A python syntax error or a malformed
# config.yaml is survivable if it's caught here, and expensive if it's caught by
# systemd at 09:29 with the open six minutes away. It checks first, restarts
# second, and rolls back if the service doesn't come up.

set -euo pipefail
cd /root/market-bot

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m[FAIL] %s\033[0m\n' "$*"; exit 1; }

BEFORE=$(git rev-parse HEAD)

say "Pulling"
git pull --ff-only origin main || fail "pull failed - resolve by hand, nothing was restarted"
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "    already up to date at ${AFTER:0:7}"
else
  echo "    ${BEFORE:0:7} -> ${AFTER:0:7}"
  git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
fi

say "Verifying before restarting anything"
python3 -c "import ast,sys,pathlib
[ast.parse(p.read_text()) for p in pathlib.Path('src').rglob('*.py')]" \
  || fail "python syntax error - NOT restarting"
echo "    python syntax ok"

python3 -c "import yaml;yaml.safe_load(open('config.yaml'))" \
  || fail "config.yaml does not parse - NOT restarting"
echo "    config.yaml parses"

python3 -c "import sys;sys.path.insert(0,'.');import src.main" \
  || fail "src.main does not import - NOT restarting"
echo "    imports clean"

say "Settings that will be live"
python3 - <<'PY'
import yaml
t = yaml.safe_load(open("config.yaml"))["trading"]
for k in ("entry_window_start","use_websocket_stream","rapid_increase_pct",
          "max_concurrent_positions","max_daily_entries","max_daily_loss_usd",
          "use_take_profit","take_profit_pct","use_earnings_list","use_qqq_list"):
    if k in t:
        print(f"    {k:28} {t[k]}")
PY

if systemctl is-active --quiet market-bot && \
   [ -n "$(pgrep -f 'python.*src.main' || true)" ]; then
  MKT=$(TZ=America/New_York date +%H%M)
  DOW=$(TZ=America/New_York date +%u)
  if [ "$DOW" -le 5 ] && [ "$MKT" -ge 0930 ] && [ "$MKT" -le 1600 ]; then
    printf '\n\033[1;33m[!] The market is OPEN and positions may be live.\033[0m\n'
    printf '    A restart drops in-memory trade tracking. Type YES to continue: '
    read -r ans; [ "$ans" = "YES" ] || fail "aborted, nothing restarted"
  fi
fi

say "Restarting"
systemctl restart market-bot
sleep 5

if systemctl is-active --quiet market-bot; then
  echo "    market-bot.service is up"
  say "Last 15 lines"
  journalctl -u market-bot -n 15 --no-pager | sed 's/^/    /'
else
  printf '\n\033[1;31m[FAIL] service did not come up - rolling back to %s\033[0m\n' "${BEFORE:0:7}"
  git reset --hard "$BEFORE"
  systemctl restart market-bot
  journalctl -u market-bot -n 30 --no-pager
  exit 1
fi
