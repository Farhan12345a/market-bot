#!/usr/bin/env bash
#
# Pull, verify, restart, confirm. Run on the Droplet from /root/market-bot.
#
# The point of this script is the ORDER. A python syntax error or a malformed
# config.yaml is survivable if it's caught here, and expensive if it's caught by
# systemd at 09:29 with the open six minutes away. It checks first, restarts
# second, and rolls back if the service doesn't come up.

set -euo pipefail

# systemctl pipes into a pager when it thinks it has a terminal, and that pager
# then sits waiting for a keypress - which looks exactly like a hang, at exactly
# the step that reads the unit file. Belt and braces alongside --no-pager.
export SYSTEMD_PAGER=cat
export PAGER=cat

# Never fail silently again: with set -e a dying command produces no output at
# all, which is indistinguishable from a hang or a dropped connection.
trap 'rc=$?; printf "\n\033[1;31m[ABORTED] ops/deploy.sh stopped at line $LINENO (exit $rc). Nothing was restarted.\033[0m\n" >&2' ERR
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

say "Finding the interpreter the SERVICE actually uses"
# Progress markers, so a run that stops here is diagnosable. A silent gap at
# this step is ambiguous between a dropped SSH session, a pager waiting for a
# keypress, and a slow import - and the difference matters, because a deploy
# that dies before the restart leaves the OLD code running.
echo "    reading the unit file..."
# Not `python3`. The bot runs from a virtualenv, so the system python has none
# of its dependencies - checking imports with the wrong interpreter reports a
# ModuleNotFoundError that says nothing about whether the deploy is safe.
# Ask systemd what it runs, and only fall back to guessing if that fails.
PY_BIN=""
# Read the unit into a variable FIRST, then parse it.
#
# The previous one-liner piped systemctl straight into `grep -m1`. grep exits
# the instant it matches, closing the pipe; systemctl then takes SIGPIPE and
# returns non-zero; `pipefail` propagates that; and `set -e` kills the script
# without printing anything. Whether systemctl finished writing before grep quit
# is a RACE, which is why this succeeded once and then died twice at exactly
# this line - leaving the old code running while the pull looked successful.
UNIT_TEXT=$(systemctl --no-pager cat market-bot 2>/dev/null || true)
UNIT_EXEC=$(printf '%s\n' "$UNIT_TEXT" | grep -m1 '^ExecStart=' | sed 's/^ExecStart=//' || true)
for cand in \
  "$(printf '%s\n' "$UNIT_EXEC" | tr ' ' '\n' | grep -m1 -E '(python|python3)$' || true)" \
  "$(dirname "$(printf '%s\n' "$UNIT_EXEC" | awk '{print $1}')")/python" \
  ./venv/bin/python ./venv/bin/python3 \
  ./.venv/bin/python ./.venv/bin/python3 \
  ./env/bin/python /usr/bin/python3
do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  if "$cand" -c "import alpaca" >/dev/null 2>&1; then PY_BIN="$cand"; break; fi
done

if [ -z "$PY_BIN" ]; then
  printf '\n\033[1;31m[FAIL] Could not find an interpreter with the bot deps installed.\033[0m\n'
  echo "  systemd ExecStart: ${UNIT_EXEC:-<not found>}"
  echo "  Find it by hand and re-run:   systemctl --no-pager cat market-bot | grep ExecStart"
  echo "  Nothing was restarted."
  exit 1
fi
echo "    candidates checked, picked one"
echo "    using $PY_BIN"
echo "    $("$PY_BIN" --version 2>&1)"

say "Verifying before restarting anything"
"$PY_BIN" -c "import ast,sys,pathlib
[ast.parse(p.read_text()) for p in pathlib.Path('src').rglob('*.py')]" \
  || fail "python syntax error - NOT restarting"
echo "    python syntax ok"

"$PY_BIN" -c "import yaml;yaml.safe_load(open('config.yaml'))" \
  || fail "config.yaml does not parse - NOT restarting"
echo "    config.yaml parses"

"$PY_BIN" -c "import sys;sys.path.insert(0,'.');import src.main" \
  || fail "src.main does not import - NOT restarting"
echo "    imports clean"

# requests underpins both HTTPS notification channels and is easy to miss if
# the venv predates them being added.
"$PY_BIN" -c "import requests" >/dev/null 2>&1 \
  || fail "'requests' is missing from $PY_BIN - run: $PY_BIN -m pip install -r requirements.txt"
echo "    requests present (notifications can send)"

say "Settings that will be live"
"$PY_BIN" - <<'PY'
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
