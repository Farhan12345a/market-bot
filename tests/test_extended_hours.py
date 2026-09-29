"""
Extended-hours measurement (trading.extended_hours_experiment), added
2026-09-27 on explicit user request: keep the SAME entry logic running past
entry_window_end (10:15) all the way to end_time (16:00) instead of going
idle, without touching the primary window's own thresholds or measurement -
every trade is tagged "primary" vs "extended" so a report can split them
and never sum the two into one number.
"""
import sys
from _repo import REPO, CONFIG, repo_file
import yaml

CFG = yaml.safe_load(open(CONFIG))
src = open(repo_file("src", "main.py")).read()
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


print("=== A. THE GATE WIDENS ONLY WHEN ENABLED ===")
check("new_entry_deadline is computed from extended_hours_experiment.end_time "
      "when enabled, entry_end unchanged otherwise",
      'new_entry_deadline = (parse_hhmm_today(_eh_cfg.get("end_time", "16:00"), et)\n'
      '                          if _eh_cfg.get("enabled") else entry_end)' in src)
check("the entry gate's upper bound is new_entry_deadline, not entry_end directly",
      "elif entry_start <= now < new_entry_deadline:" in src)
check("the day-completion check uses the same deadline, so the loop does not "
      "end early and skip the extended window entirely",
      "if not open_trades and now >= new_entry_deadline and had_any_trades:" in src)
check("entry_end ITSELF is never reassigned - the primary window's own "
      "boundary stays exactly what it always was",
      'entry_end = parse_hhmm_today(config["trading"]["entry_window_end"], et)' in src)

print("\n=== B. EVERY ENTRY IS TAGGED BY WINDOW ===")
check("entry_window_label is computed once per poll from `now` vs entry_end",
      'entry_window_label = "primary" if now < entry_end else "extended"' in src)
check("_attempt_entry accepts entry_window_label and records it on entry_meta",
      "entry_window_label=None):" in src
      and 'executor.entry_meta[symbol]["entry_window"] = entry_window_label' in src)
check("the long-side burst_candidates call site passes it through",
      "entry_window_label=entry_window_label," in src)
check("the opening burst is tagged its OWN window, not folded into "
      "primary/extended - it is a third, already-distinct mechanism",
      'entry_window_label="opening_burst",' in src)

print("\n=== C. THE CSV COLUMN, WITH A SAFE DEFAULT FOR OLD ROWS ===")
esrc = open(repo_file("src", "executor", "executor.py")).read()
check("trade_record carries entry_window, defaulting to 'primary' for any "
      "entry that never set it (every entry before 2026-09-27)",
      '"entry_window": meta.get("entry_window", "primary"),' in esrc)
check("side is recorded the same way, defaulting to 'long'",
      '"side": meta.get("side", "long"),' in esrc)
check("both columns are in trade_history.csv's fieldnames, appended at the "
      "end per repair_header's own convention",
      '"side", "entry_window",' in esrc)
check("a v4 legacy schema exists so repair_header can upgrade pre-existing "
      "trade_history.csv files that predate these two columns",
      '[c for c in fieldnames if c not in ("side", "entry_window")]' in esrc)

print("\n=== D. REPORT SECTION ===")
from src.notifications.email_notifier import EmailNotifier
n = EmailNotifier.__new__(EmailNotifier)
trades = [
    {"symbol": "AAPL", "entry_window": "extended", "entry_time": "2026-09-27T15:20:00",
     "exit_time": "2026-09-27T15:41:00", "entry_price": 230.0, "exit_price": 232.3,
     "qty": 40, "pl": 92.0, "pl_pct": 1.0, "exit_reason": "TAKE_PROFIT_1%", "side": "long"},
    # A SHORT that also happened to trade during the extended-hours window -
    # this must land in the Short Strategy section, NOT here, so the two
    # sections stay a single, unambiguous axis each (window vs. side).
    {"symbol": "TSLA", "entry_window": "extended", "entry_time": "2026-09-27T15:25:00",
     "exit_time": "2026-09-27T15:30:00", "entry_price": 250.0, "exit_price": 248.25,
     "qty": 20, "pl": -35.0, "pl_pct": -0.7, "exit_reason": "FIRST_EXIT_-0.7%", "side": "short"},
    {"symbol": "MSFT", "entry_window": "primary", "side": "long", "pl": 20.0, "pl_pct": 0.5},
    {"symbol": "NFLX", "entry_method": "OPENING_MOVE", "entry_window": "opening_burst",
     "side": "long", "pl": 15.0, "pl_pct": 0.3},
]
html = n._extended_hours_html(trades)
check("section renders when extended-hours trades exist", bool(html))
check("total P&L is ONLY the extended LONG trade ($92.00) - the short is excluded",
      "$92.00" in html, html[:400])
check("the primary-window and opening-burst trades are excluded from this section",
      "MSFT" not in html and "NFLX" not in html)
check("the extended-window SHORT is also excluded - it belongs in the Short "
      "Strategy section instead", "TSLA" not in html, html)
check("only the one extended long trade is shown", "AAPL" in html)
check("it never claims to be summed with the primary window",
      "never summed" in html.lower())
check("no extended-hours (long) trades -> no section at all (a quiet day, "
      "the feature is off, or every extended trade that day was a short)",
      n._extended_hours_html([{"symbol": "X", "entry_window": "primary", "pl": 1}]) == "")
check("empty input -> no section", n._extended_hours_html([]) == "")
check("old trades with no entry_window key at all are correctly treated as "
      "primary (not extended) - the safe default for pre-2026-09-27 rows",
      n._extended_hours_html([{"symbol": "OLD", "pl": 5}]) == "")

print("\n=== D2. SHORT STRATEGY REPORT SECTION ===")
# A second short, tagged "primary" (9:30-10:15), added alongside the
# existing "extended" TSLA short so BOTH of the requested categories -
# explicit user instruction, 2026-09-28: "I want two categories for the
# shorting. First category will be from 9:30 to 10:15... second category
# will be from 10:15 to the end of the day" - actually get exercised.
trades_2cat = trades + [
    {"symbol": "AMD", "entry_window": "primary", "entry_time": "2026-09-27T13:40:00",
     "exit_time": "2026-09-27T13:50:00", "entry_price": 150.0, "exit_price": 148.5,
     "qty": 30, "pl": 45.0, "pl_pct": 1.0, "exit_reason": "TAKE_PROFIT_1%", "side": "short"},
]
html_sh = n._short_strategy_html(trades_2cat)
check("section renders when short trades exist", bool(html_sh))
check("the OVERALL combined total is both shorts together ($10.00 = 45 - 35)",
      "$10.00" in html_sh, html_sh[:600])
check("the long trades are excluded from this section, regardless of window",
      "AAPL" not in html_sh and "MSFT" not in html_sh and "NFLX" not in html_sh)
check("both shorts ARE shown", "TSLA" in html_sh and "AMD" in html_sh)
check("its window is labeled on the row, so a short from any window is "
      "still traceable", "10:15-close" in html_sh and "9:30-10:15" in html_sh)
check("it never claims to be summed with the primary window",
      "never summed" in html_sh.lower())
check("mentions the mutual-exclusion guarantee",
      "never" in html_sh.lower() and "same regime window" in html_sh.lower())

check("CATEGORY 1 (9:30-10:15) shows AMD's own P&L ($45.00), not the combined total",
      "$45.00" in html_sh)
check("CATEGORY 2 (10:15-close) shows TSLA's own P&L ($-35.00), not the combined total",
      "$-35.00" in html_sh)
check("both category labels are present as their own headers",
      "primary window hours" in html_sh and "extended hours" in html_sh)

# A day with shorts in only ONE window - the other category must say so
# plainly rather than silently disappearing or showing a stale number.
one_cat = [t for t in trades_2cat if t.get("symbol") == "AMD"]
html_one = n._short_strategy_html(one_cat)
check("a category with nothing in it says so explicitly rather than "
      "vanishing", "No shorts taken in this window today." in html_one)

check("no short trades -> no section at all",
      n._short_strategy_html([{"symbol": "X", "side": "long", "pl": 1}]) == "")
check("empty input -> no section", n._short_strategy_html([]) == "")

print("\n=== D3. THE HEADLINE EXCLUDES BOTH SHORTS AND EXTENDED-HOURS TRADES ===")
esrc = open(repo_file("src", "notifications", "email_notifier.py")).read()
check("primary_trades filters out side==short AND entry_window==extended "
      "before the headline Total P&L/win-rate/trade-count are computed",
      'if t.get("side") != "short" and t.get("entry_window") != "extended"' in esrc)
check("opening_burst trades are explicitly NOT excluded from the headline - "
      "that has always been part of the tracked total, only the two NEW "
      "features (shorts, extended hours) are pulled out",
      "The opening-move experiment is NOT excluded here" in esrc)
check("the push-notification summary (_plain_text_summary) applies the "
      "SAME filter, so the phone alert and the report never disagree",
      'if t.get("side") != "short" and t.get("entry_window") != "extended"'
      in esrc.split("def _plain_text_summary")[1][:800])
check("...and still mentions short/extended P&L separately rather than "
      "hiding it from the push notification entirely",
      "Short strategy (separate)" in esrc and "Extended hours (separate)" in esrc)

print("\n=== E. LIVE CONFIG ===")
eh = CFG["trading"].get("extended_hours_experiment") or {}
check("extended_hours_experiment.enabled is on", eh.get("enabled") is True)
check("end_time reaches to the close", eh.get("end_time") == "16:00")
check("entry_window_end (the PRIMARY window) is unchanged at 10:15",
      CFG["trading"]["entry_window_end"] == "10:15")

print("\n=== F. EXTENDED-HOURS PER-HOUR CAP + QUALITY RANKING (2026-09-29) ===")
# 2026-09-28's max_daily_entries bug (fixed separately, see test_phantom_exit.py
# 12b-12e and test_timeline.py's changed-settings list) meant extended hours
# never got a real sample. Once that was fixed, the user asked for a SEPARATE
# dial scoped to just this window: no daily cap, but a per-hour rate limit,
# PLUS picking the best-scored candidates first when an hour's slots run
# scarce - reusing the SAME continuation-score ranking the long side's burst
# throttle already uses, rather than inventing a new quality gate.
check("_extended_hourly_due exists and resets on the hour, not the day",
      "def _extended_hourly_due(state, now):" in src
      and 'bucket = now.replace(minute=0, second=0, microsecond=0)' in src)
check("...and resets its own 'already logged the cap' flag on rollover too, "
      "so the cap-reached message logs once per HOUR, not once ever",
      'state["cap_logged"] = False' in src)

# Exercise the real function directly. Importing main.py wholesale has heavy
# top-level side effects elsewhere in this suite's environment, so pull just
# this pure function out of source and exec it in an isolated namespace -
# cheap, and it is real behavioural coverage rather than another string match.
import re as _re
_m = _re.search(
    r"def _extended_hourly_due\(state, now\):.*?(?=\n\ndef )", src, _re.S)
_ns = {}
exec(compile(_m.group(0), "<_extended_hourly_due>", "exec"), _ns)
_extended_hourly_due = _ns["_extended_hourly_due"]

import datetime as _dt
st = {"bucket": None, "count": 3, "cap_logged": True}
_extended_hourly_due(st, _dt.datetime(2026, 9, 29, 14, 12))
check("first call this hour adopts the 14:00 bucket and resets count/cap_logged",
      st == {"bucket": _dt.datetime(2026, 9, 29, 14, 0), "count": 0, "cap_logged": False},
      st)
st["count"] = 20
st["cap_logged"] = True
_extended_hourly_due(st, _dt.datetime(2026, 9, 29, 14, 47))
check("still inside the same hour -> state is left alone (not reset mid-hour)",
      st == {"bucket": _dt.datetime(2026, 9, 29, 14, 0), "count": 20, "cap_logged": True}, st)
_extended_hourly_due(st, _dt.datetime(2026, 9, 29, 15, 3))
check("the hour rolls over -> count and cap_logged both reset for the fresh hour",
      st == {"bucket": _dt.datetime(2026, 9, 29, 15, 0), "count": 0, "cap_logged": False}, st)

check("config carries the new per-hour cap, separate from max_daily_entries",
      (CFG["trading"].get("extended_hours_experiment") or {}).get("max_entries_per_hour") == 20)
check("max_daily_entries itself was raised well past the old 50 - it is no "
      "longer sized for the pre-fix count-every-submission model",
      CFG["trading"]["max_daily_entries"] >= 200)

check("the extended-hourly cap is checked in BOTH the long burst_candidates "
      "loop and the short_candidates loop - it must gate whichever side is "
      "actually active that afternoon, not just one",
      src.count('skip_reason = "extended_hourly_cap"') >= 2)
check("hitting the cap logs once per hour, naming the hour and the cap",
      'Reached the extended-hours per-hour cap' in src)
check("a filled extended-hours entry increments the SAME cumulative "
      "day-long counter the day-complete log line reports",
      "extended_entries_today += 1" in src
      and "extended_hours_entries={extended_entries_today}" in src)
check("a phantom-dropped extended-hours entry refunds BOTH the hourly bucket "
      "and the cumulative day counter, mirroring max_daily_entries' own "
      "refund exactly - a never-filled order must not count against either",
      src.count('extended_hourly_state["count"] = max(0, extended_hourly_state["count"] - 1)') >= 2
      and src.count("extended_entries_today = max(0, extended_entries_today - 1)") >= 2)

check("the short-candidates loop is now ranked best-first by the SAME "
      "continuation score the long side's burst throttle already uses - "
      "previously it had NO ranking at all, taking whatever order the "
      "screener happened to sort in",
      "short_candidates, _short_rank_note = _rank_burst(config, short_candidates)" in src)
_short_side_src = src.split("# SHORT-SIDE, requested by the user")[1]
check("...and the enrichment pass (computing cf_score) still runs BEFORE "
      "ranking, in its own loop - ranking on a score that has not been "
      "computed yet would just be ranking by None for everyone",
      _short_side_src.index('cand["cont"] = _continuation_fields(') <
      _short_side_src.index("short_candidates, _short_rank_note = _rank_burst"))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
