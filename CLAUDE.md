# market-bot — working agreements

Read this before proposing or making changes.

## Vocabulary — two different mechanisms, never conflate them

These have similar names and opposite intentions. Getting them mixed up would
break the thing each one exists to protect.

### "first few minutes test" — the OPENING BURST, 09:30-09:33

`opening_burst` in config.yaml, `_run_opening_burst` in src/main.py.

Its own measurement window, its own exit profile (-0.3% first / -0.35% final /
0.4% trail / +0.15% breakeven), its own budget of `max_positions: 7` at
`size_multiplier: 0.5`.

**The intent is BREADTH.** Take as many qualifying openers as the budget
allows. The whole premise is that the first three minutes contain the day's
cleanest continuation moves and the mode is starved if it takes two of them -
which is exactly what happened on 2026-09-02, when the spread gate refused
every candidate it measured and the mode entered twice.

**Nothing that throttles simultaneous signals may be applied here.** Verified
in tests/test_bursts_separate.py.

### "burst logic" — the NORMAL-WINDOW throttle, 09:33 onward

`use_burst_throttle` / `burst_width_threshold` / `burst_max_entries` /
`burst_size_multiplier` in config.yaml, `_burst_policy` in src/main.py.

When N symbols signal in the SAME poll, take at most `burst_max_entries` of
them at `burst_size_multiplier` size.

**The intent is CONCENTRATION CONTROL.** Symbols moving together in one poll
are usually one bet wearing several tickers; sized as if independent, they lose
together. 2026-09-02 is the evidence: NOW, CRM and WDAY all map to XLK, were
bought inside 96 seconds, and produced $267.94 of a $403.66 loss.

**These two never interact.** `_burst_policy` is called from exactly one place,
in `run_trading_day`'s normal entry path. `_run_opening_burst` reads only its
own `opening_burst` block.

## ENTRY changes need a stable measurement window — STOP AND SAY SO

**Before making any change to ENTRY logic, remind the user of this and get an
explicit go-ahead.** Standing instruction from the user, 2026-09-02.

The reason is not caution for its own sake, it is that entry changes are
**unmeasurable in retrospect**:

  - **EXIT changes are replayable.** `ops/replay.py` walks recorded price paths
    against different stops, targets and tiers. Change `final_exit_loss_pct`
    and replay says what it would have done, with no waiting. Tune freely.
  - **ENTRY changes are not.** Replay only sees trades that were TAKEN. Change
    `min_move_pct`, `max_extension_from_open_pct` or the extension gate and you
    change WHICH TRADES EXIST - and there is no recorded path for a trade never
    entered. The signal journal records forward returns for skipped signals,
    which helps, but it is a narrower instrument than a full replay.

So the methodology (PENDING_WORK item 6): **one entry variable at a time, held
for a week**, compared against the prior week via `ops/session-metrics.py`.

The sample size that matters is **trades since the last config change**, not
total trades. ~400 trades exist across roughly 15 different configurations,
which is not 400 observations of anything.

Entry-side settings this applies to: `rapid_increase_pct`,
`rapid_increase_max_pct`, `max_extension_from_open_pct`, `min_move_pct`,
`min_move_to_spread_ratio`, `entry_window_start`/`_end`, `use_pullback_entry`,
`use_three_bar_momentum`, screener thresholds, and universe selection.

## Testing and deployment

- Do NOT run the test suite, commit, or push unless the user explicitly asks.
  Standing instruction, restated several times.
- `bash ops/runall.sh` runs every suite. A suite is only OK if it reached its
  own summary line.
- The bot runs from a virtualenv on the VPS. `python3` is the WRONG
  interpreter there; `systemctl cat market-bot | grep ExecStart` names the
  right one.
- This container cannot SSH to the VPS - outbound port 22 times out, the same
  wall that blocks SMTP. Deploys are `git pull && ./ops/deploy.sh` run by the
  user on the Droplet.

## Style

Comments explain WHY, anchored to the specific session and dollar figure that
motivated the code. A guard with no recorded reason gets removed by someone who
cannot tell it apart from dead code - which is how `breadth_halt` survived as
dead code for days, and how `send_alert()` sat with zero call sites.
