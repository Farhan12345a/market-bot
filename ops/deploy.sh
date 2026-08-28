#!/usr/bin/env bash
#
# Pull, verify, restart, confirm. Run on the Droplet from /root/market-bot.
#
# FIRST: re-run from a snapshot, so bash is never reading a file git may replace.
#
# bash reads a script incrementally, by BYTE OFFSET, as it executes. This script
# git-pulls a new copy of ITSELF partway through, so without this the running
# shell continues from a shifted position in a different file. On 2026-08-27 a
# +50/-4 change pulled cleanly and then printed the OLD settings block, because
# the shell carried on through stale content. That was the harmless outcome; the
# harmful one is an offset landing mid-statement, which reproduces as
# "unexpected EOF while looking for matching quote" - potentially with the
# service already stopped.
#
# Copying to /tmp and executing that means the file under bash never changes,
# whatever git does to the repo. The re-exec after the pull then makes sure the
# NEW version is what actually runs.
if [ -z "${MARKET_BOT_SNAPSHOT:-}" ]; then
  _snap=$(mktemp /tmp/market-bot-deploy.XXXXXX)
  cp "$0" "$_snap"
  export MARKET_BOT_SNAPSHOT="$_snap"
  export MARKET_BOT_ORIGIN="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  exec bash "$_snap" "$@"
fi
SELF_SNAPSHOT="$MARKET_BOT_SNAPSHOT"
SELF_ORIGIN="$MARKET_BOT_ORIGIN"
trap 'rm -f "$SELF_SNAPSHOT"' EXIT
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

# Re-exec if the pull changed THIS script, so the NEW logic is what runs.
#
# Safe to do here only because of the snapshot at the top of this file: without
# it, this check can never be reached, because bash may already have died on the
# changed file before getting here.
if ! cmp -s "$SELF_ORIGIN" "$SELF_SNAPSHOT" && [ -z "${MARKET_BOT_REEXEC:-}" ]; then
  printf '\n\033[1;33m[!] ops/deploy.sh was updated by this pull - restarting it so the\n'
  printf '    NEW version runs. Nothing has been restarted yet.\033[0m\n'
  rm -f "$SELF_SNAPSHOT"
  MARKET_BOT_REEXEC=1 MARKET_BOT_SNAPSHOT= exec bash "$SELF_ORIGIN" "$@"
fi

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
# Shared with ops/runall.sh - see ops/_python.sh for why this is not `python3`.
#
# $0 is the /tmp SNAPSHOT, not the repo copy - see the re-exec at the top. So
# dirname "$0" is /tmp, and sourcing relative to it fails with
# "/tmp/_python.sh: No such file or directory". Resolve against SELF_ORIGIN,
# which is the real path in the repo.
# shellcheck source=ops/_python.sh
. "$(dirname "$SELF_ORIGIN")/_python.sh"
if ! find_bot_python; then
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
# This block is the pre-flight confidence check, so it has to show what CHANGED,
# not a fixed list written once. On 2026-08-27 it printed take_profit_pct - a
# legacy fallback superseded by take_profit_tiers and not in use - while saying
# nothing about the opening-move experiment, the exit profile or the dynamic
# universe. It would have looked identical whether any of that shipped or not.
"$PY_BIN" - <<'PY'
import yaml
t = yaml.safe_load(open("config.yaml"))["trading"]

def show(label, value):
    print(f"    {label:30} {value}")

def tiers(part, key, field):
    return "/".join(str(x.get(field)) for x in (part.get(key) or [])) or "-"

print("  session")
for k in ("entry_window_start", "entry_window_end", "rapid_increase_pct",
          "rapid_increase_max_pct", "max_concurrent_positions", "max_daily_entries",
          "max_daily_loss_usd", "reentry_cooldown_minutes"):
    if k in t:
        show(k, t[k])
show("take_profit gains", tiers(t, "take_profit_tiers", "gain_pct") + "%")
show("first / final exit", f"{t.get('first_exit_loss_pct')}% / {t.get('final_exit_loss_pct')}%")
show("trailing_stop_pct", f"{t.get('trailing_stop_pct')}%")
show("breakeven triggers", tiers(t, "breakeven_tiers", "trigger_pct") + "%")
show("use_resistance_exit", t.get("use_resistance_exit"))

print("  data")
for k in ("use_websocket_stream", "stream_max_subscriptions", "stream_benchmarks",
          "stream_prestart_minutes", "entry_check_interval_seconds",
          "use_dynamic_universe", "universe_size", "universe_shortlist_size"):
    if k in t:
        show(k, t[k])
if t.get("universe_min_dollar_volume"):
    show("universe_min_dollar_volume", f"${t['universe_min_dollar_volume']/1e6:.0f}M")
show("use_continuation_score", t.get("use_continuation_score"))

ob = t.get("opening_burst") or {}
if ob.get("enabled"):
    print("  opening burst  *** EXPERIMENT ACTIVE ***")
    show("window", f"{ob.get('baseline_time')} -> {ob.get('decide_by')}")
    show("min_move_pct", f"{ob.get('min_move_pct')}%")
    show("max_positions", f"{ob.get('max_positions')} of {t.get('max_concurrent_positions')}")
    show("size_multiplier", f"{ob.get('size_multiplier')}x")
    show("streamed_only", ob.get("streamed_only"))
    ex = ob.get("exits") or {}
    if ex:
        show("exits: first / final",
             f"{ex.get('first_exit_loss_pct')}% / {ex.get('final_exit_loss_pct')}%")
        show("exits: trailing", f"{ex.get('trailing_stop_pct')}%")
        show("exits: take-profit", tiers(ex, "take_profit_tiers", "gain_pct") + "%")
        show("exits: breakeven", tiers(ex, "breakeven_tiers", "trigger_pct") + "%")
else:
    print("  opening burst                  DISABLED")
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
