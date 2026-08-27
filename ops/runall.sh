#!/usr/bin/env bash
# Runs every suite in tests/.
#
# A suite is only OK if it reached its own summary line - a logged traceback
# from deliberate failure-injection is normal, a suite that died halfway is not,
# and the two are indistinguishable by grepping for "Traceback". The summary
# line proves the file ran to the end.
#
# Each suite runs with a THROWAWAY working directory. Several exercise code that
# resolves paths relative to cwd - save_trades_log() appends to
# logs/trade_history.csv, the signal journal writes logs/signal_journal.csv - and
# on the VPS those are the live files behind every report and ANALYSIS_LOG.md.
# Running the suite must never be able to touch them. tests/_repo.py handles
# this from inside the suites too; this is the belt to that pair of braces.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
cd "$repo" || { echo "no repo at $repo"; exit 1; }

# The bot runs from a virtualenv, so bare `python3` has none of its
# dependencies. Running the suite with the wrong interpreter produced 20
# "problem suites" on 2026-08-26 on a VPS where every one of them passes - a
# wall of ModuleNotFoundError that says nothing about whether the code works.
# Same detection deploy.sh uses, shared so the two cannot drift.
# shellcheck source=ops/_python.sh
. "$here/_python.sh"
find_bot_python || exit 1
echo "interpreter: $PY_BIN"
echo "             $("$PY_BIN" --version 2>&1)"
echo

cd "$repo/tests" || { echo "no tests/ directory at $repo"; exit 1; }

tot=0; fails=0; bad=""
for x in test_*.py preflight.py; do
  [ -e "$x" ] || continue
  sandbox=$(mktemp -d)
  out=$(cd "$sandbox" && timeout 300 "$PY_BIN" "$repo/tests/$x" 2>&1); rc=$?
  rm -rf "$sandbox"
  n=$(echo "$out" | grep -c "^PASS"); e=$(echo "$out" | grep -c "^FAIL")
  problem=""
  # Older suites end with "ALL PASSED" / "FAILURES: [...]"; newer ones with
  # "N passed, M failed". Either proves the file reached its end.
  echo "$out" | grep -qE "^[0-9]+ passed, [0-9]+ failed|^ALL PASSED|^FAILURES:" \
    || problem="DIED EARLY (never reached its summary)"
  [ "$rc" -ne 0 ] && [ "$e" -eq 0 ] && [ -z "$problem" ] && problem="EXIT $rc"
  tot=$((tot+n)); fails=$((fails+e))
  printf "%-24s %3d pass %2d fail %s\n" "$x" "$n" "$e" "$problem"
  if [ -n "$problem" ] || [ "$e" -gt 0 ]; then
    bad="$bad $x"
    echo "$out" | grep -E "^FAIL" | head -4 | sed 's/^/      /'
    [ -n "$problem" ] && echo "$out" | tail -5 | sed 's/^/      /'
  fi
done
echo "-------------------------------------------------"
echo "TOTAL: $tot pass, $fails fail"
[ -n "$bad" ] && { echo "PROBLEM SUITES:$bad"; exit 1; }
echo "ALL SUITES CLEAN"
