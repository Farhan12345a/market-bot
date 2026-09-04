#!/usr/bin/env bash
#
# One command for the whole pre-market checklist: deploy, run the full suite,
# check for stray positions, confirm the settings that matter are live.
#
# Run from /root/market-bot on the Droplet:
#
#     bash ops/deploy-and-verify.sh
#
# This is a WRAPPER around ops/deploy.sh, not a replacement for it - deploy.sh
# already does the pull/syntax-check/config-check/restart/rollback sequence
# and handles re-exec-on-self-update safely (see its own header). Duplicating
# that here would just be a second, unsynced copy of logic that already has to
# be exactly right. This script adds the parts deploy.sh deliberately does NOT
# do: the full test suite (deploy.sh only checks that imports/config parse,
# not that the suite passes - a compiling bug is not the same class of risk as
# a broken test), and a stray-position/short check.
#
# Stops at the first thing that needs a human, rather than plowing through a
# checklist that already failed at step 2.

set -uo pipefail
cd /root/market-bot || { echo "not in /root/market-bot"; exit 1; }

RED='\033[1;31m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; CYAN='\033[1;36m'; RESET='\033[0m'
say()  { printf "\n${CYAN}==> %s${RESET}\n" "$*"; }
ok()   { printf "${GREEN}[OK]${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}[!]${RESET} %s\n" "$*"; }
bad()  { printf "${RED}[FAIL]${RESET} %s\n" "$*"; }

STEP=1
step_header() { say "STEP $STEP: $*"; STEP=$((STEP+1)); }

# ---------------------------------------------------------------------------
step_header "Deploy (pull, verify, restart, confirm)"
if ! bash ops/deploy.sh; then
  bad "deploy.sh failed or rolled back - see its output above. STOPPING HERE."
  echo "Nothing past this point ran. Fix the deploy before continuing."
  exit 1
fi
ok "deploy.sh completed and the service is up"

# ---------------------------------------------------------------------------
step_header "Full test suite (the venv, not bare python3)"
. ops/_python.sh
if ! find_bot_python; then
  bad "could not find an interpreter with the bot's dependencies - cannot run the suite"
  exit 1
fi
SUITE_OUT=$(bash ops/runall.sh 2>&1)
echo "$SUITE_OUT" | tail -20
if echo "$SUITE_OUT" | grep -q "ALL SUITES CLEAN"; then
  ok "full suite clean"
else
  bad "the suite did NOT come back clean - see PROBLEM SUITES above."
  echo "This does not mean the service is down (deploy.sh already restarted it"
  echo "on code that at least imports cleanly), but do not trust today's run"
  echo "until this is investigated. Full output was printed above."
  # Deliberately does not exit here - the service is already live at this
  # point and stopping the SCRIPT does not stop the BOT. A human needs to see
  # this and decide, not have the checklist hide it by continuing silently.
fi

# ---------------------------------------------------------------------------
step_header "Stray or short positions (DRY RUN - this touches nothing)"
FLATTEN_OUT=$("$PY_BIN" ops/flatten-now.py 2>&1)
echo "$FLATTEN_OUT"
if echo "$FLATTEN_OUT" | grep -q "No open positions"; then
  ok "flat - no open positions"
elif echo "$FLATTEN_OUT" | grep -qi "SHORT"; then
  bad "a SHORT position was found - this bot has no intent to ever be short."
  echo "This is the phantom-position failure mode from PENDING_WORK.md if it"
  echo "fires overnight. Do not assume it will resolve itself before 09:30."
else
  warn "open position(s) found (listed above) - expected only if you left"
  echo "    something open on purpose. Investigate before market open if not."
fi

# ---------------------------------------------------------------------------
step_header "Notifications (only checked, never required)"
if [ -f /etc/market-bot.env ] && grep -q "RESEND_API_KEY=re_" /etc/market-bot.env 2>/dev/null; then
  ok "RESEND_API_KEY is set in /etc/market-bot.env"
else
  warn "RESEND_API_KEY not found in /etc/market-bot.env - daily reports/alerts"
  echo "    will not be emailed. Not required to trade; see ops/NOTIFICATIONS.md"
  echo "    if you want it working."
fi

# ---------------------------------------------------------------------------
say "SUMMARY"
echo "Scroll up for full detail on anything marked [!] or [FAIL]."
echo "If everything above says [OK], you're clear for the open."
