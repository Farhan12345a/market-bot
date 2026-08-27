# Find the interpreter the BOT actually runs under. Sourced, not executed.
#
# Not `python3`. The bot runs from a virtualenv, so the system python has none
# of its dependencies. Using it produces a wall of ModuleNotFoundError that says
# nothing about whether the code is correct - which is exactly what happened on
# 2026-08-26, when ops/runall.sh reported 20 "problem suites" on a VPS where
# every one of them passes. deploy.sh had already learned this lesson; runall.sh
# had not, because the logic lived inside deploy.sh instead of somewhere both
# could reach. Hence this file.
#
# Sets PY_BIN, or returns non-zero having explained itself.

find_bot_python() {
  PY_BIN=""

  # Read the unit into a variable FIRST, then parse it.
  #
  # The original one-liner piped systemctl straight into `grep -m1`. grep exits
  # the instant it matches, closing the pipe; systemctl then takes SIGPIPE and
  # returns non-zero; `pipefail` propagates that; and `set -e` kills the script
  # without printing anything. Whether systemctl finished writing before grep
  # quit is a RACE, which is why it succeeded once and then died twice at
  # exactly that line.
  local unit_text unit_exec cand
  unit_text=$(systemctl --no-pager cat market-bot 2>/dev/null || true)
  unit_exec=$(printf '%s\n' "$unit_text" | grep -m1 '^ExecStart=' | sed 's/^ExecStart=//' || true)

  for cand in \
    "$(printf '%s\n' "$unit_exec" | tr ' ' '\n' | grep -m1 -E '(python|python3)$' || true)" \
    "$(dirname "$(printf '%s\n' "$unit_exec" | awk '{print $1}')")/python" \
    ./venv/bin/python ./venv/bin/python3 \
    ./.venv/bin/python ./.venv/bin/python3 \
    ./env/bin/python \
    "${VIRTUAL_ENV:-}/bin/python" \
    "$(command -v python3 || true)"
  do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    # The test is "can it import the bot's deps", not "does it exist". A python
    # that cannot import alpaca cannot run the bot or its tests, whatever its
    # path suggests.
    if "$cand" -c "import alpaca, pandas" >/dev/null 2>&1; then
      PY_BIN="$cand"
      return 0
    fi
  done

  printf '\033[1;31m[FAIL] No interpreter found with the bot dependencies installed.\033[0m\n' >&2
  echo "  systemd ExecStart: ${unit_exec:-<not found>}" >&2
  echo "  Find it by hand:   systemctl --no-pager cat market-bot | grep ExecStart" >&2
  echo "  Then:              <that python> -c 'import alpaca, pandas'" >&2
  return 1
}
