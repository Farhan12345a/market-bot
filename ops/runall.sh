#!/usr/bin/env bash
# Runs every suite. A suite is only OK if it reached its own summary line -
# a logged traceback from deliberate failure-injection is normal, a suite that
# died halfway is not, and the two are indistinguishable by grepping for
# "Traceback". The summary line proves the file ran to the end.
cd "$(dirname "$0")"
tot=0; fails=0; bad=""
for x in test_*.py preflight.py; do
  out=$(timeout 300 python3 "$x" 2>&1); rc=$?
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
