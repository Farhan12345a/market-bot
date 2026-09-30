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
    # as of 2026-09-29 this belongs HERE too (both sides shown together),
    # AND still separately in the Short Strategy section's own extended
    # category - the two deliberately overlap now, see each docstring.
    {"symbol": "TSLA", "entry_window": "extended", "entry_time": "2026-09-27T15:25:00",
     "exit_time": "2026-09-27T15:30:00", "entry_price": 250.0, "exit_price": 248.25,
     "qty": 20, "pl": -35.0, "pl_pct": -0.7, "exit_reason": "FIRST_EXIT_-0.7%", "side": "short"},
    {"symbol": "MSFT", "entry_window": "primary", "side": "long", "pl": 20.0, "pl_pct": 0.5},
    {"symbol": "NFLX", "entry_method": "OPENING_MOVE", "entry_window": "opening_burst",
     "side": "long", "pl": 15.0, "pl_pct": 0.3},
]
html = n._extended_hours_html(trades)
check("section renders when extended-hours trades exist", bool(html))
check("total P&L is BOTH extended trades combined ($57.00 = 92 - 35), long "
      "and short together", "$57.00" in html, html[:400])
check("the primary-window and opening-burst trades are excluded from this section",
      "MSFT" not in html and "NFLX" not in html)
check("the extended-window SHORT is now INCLUDED here too, tagged by side",
      "TSLA" in html and "SHORT" in html, html)
check("the extended-window LONG is shown, tagged by side",
      "AAPL" in html and "LONG" in html)
check("it never claims to be summed into the Total P&L above",
      "never summed" in html.lower())
check("no extended-hours trades at all -> no section",
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
check("says plainly which category is in the headline and which is not, "
      "now that the two categories are no longer treated the same way",
      "already included in the Total P&amp;L above" in html_sh
      and "NOT included in the Total P&amp;L" in html_sh)
check("mentions the mutual-exclusion guarantee",
      "never" in html_sh.lower() and "same regime window" in html_sh.lower())

check("CATEGORY 1 (9:30-10:15) shows AMD's own P&L ($45.00), not the combined total",
      "$45.00" in html_sh)
check("CATEGORY 2 (10:15-close) shows TSLA's own P&L ($-35.00), not the combined total",
      "$-35.00" in html_sh)
check("both category labels are present as their own headers, marked with "
      "whether they're in the headline total",
      "in Total P&amp;L above" in html_sh and "NOT in Total P&amp;L" in html_sh)

# A day with shorts in only ONE window - the other category must say so
# plainly rather than silently disappearing or showing a stale number.
one_cat = [t for t in trades_2cat if t.get("symbol") == "AMD"]
html_one = n._short_strategy_html(one_cat)
check("a category with nothing in it says so explicitly rather than "
      "vanishing", "No shorts taken in this window today." in html_one)

check("no short trades -> no section at all",
      n._short_strategy_html([{"symbol": "X", "side": "long", "pl": 1}]) == "")
check("empty input -> no section", n._short_strategy_html([]) == "")

print("\n=== D3. THE HEADLINE INCLUDES PRIMARY SHORTS, EXCLUDES EXTENDED (2026-09-29) ===")
esrc = open(repo_file("src", "notifications", "email_notifier.py")).read()
check("primary_trades filters OUT entry_window==extended only - side is no "
      "longer part of this filter, so primary-window shorts now count "
      "toward the headline Total P&L/win-rate/trade-count, same as longs",
      'primary_trades = [t for t in trades if t.get("entry_window") != "extended"]'
      in esrc)
check("...and does NOT still exclude by side anywhere in that filter line",
      'side' not in esrc.split('primary_trades = [t for t in trades')[1][:5])
check("opening_burst trades are explicitly NOT excluded from the headline - "
      "that has always been part of the tracked total",
      "The opening-move experiment" in esrc)
check("the push-notification summary (_plain_text_summary) applies the "
      "SAME filter, so the phone alert and the report never disagree",
      'closed = [t for t in all_closed if t.get("entry_window") != "extended"]'
      in esrc.split("def _plain_text_summary")[1][:1200])
check("...and still breaks out short/extended P&L separately for detail, "
      "rather than hiding the split from the push notification entirely",
      "Of which, shorts" in esrc and "Extended hours (separate" in esrc)

n.run_context = {}   # _plain_text_summary reads self.run_context; __new__ skips __init__'s default
trades_headline = [
    {"symbol": "MSFT", "entry_window": "primary", "side": "long", "pl": 20.0,
     "exit_price": 1, "entry_price": 1},
    {"symbol": "AMD", "entry_window": "primary", "side": "short", "pl": 45.0,
     "exit_price": 1, "entry_price": 1},
    {"symbol": "TSLA", "entry_window": "extended", "side": "short", "pl": -35.0,
     "exit_price": 1, "entry_price": 1},
]
check("end-to-end: the plain-text P&L is longs+shorts in primary ($65), "
      "excluding the extended short entirely",
      "P&L $+65.00" in n._plain_text_summary(trades_headline),
      n._plain_text_summary(trades_headline))
check("...and still surfaces the short-within-primary split",
      "Of which, shorts: $+45.00" in n._plain_text_summary(trades_headline))
check("...and the excluded extended short is broken out separately, "
      "explicitly marked as not in the P&L figure above",
      "Extended hours (separate, NOT in P&L above): $-35.00"
      in n._plain_text_summary(trades_headline))

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
      CFG["trading"]["max_daily_entries"] >= 100)

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

print("\n=== G. REGIME TIMELINE (2026-09-29) ===")
# The colored 9:30-16:00 strip in the email report showing which regime was
# in force when - explicit user request, so a reader can see at a glance
# when the tape favored longs vs shorts without reading the log.
check("main.py resets logs/regime_timeline.json at the start of every day, "
      "stamped with today's date, so a report built before the first "
      "regime read never shows yesterday's stale timeline",
      'json.dump({"date": datetime.now(et).strftime("%Y-%m-%d"), "events": regime_timeline}, _f)'
      in src)
check("a row is appended (and the file rewritten) only when the label "
      "actually CHANGES - not every poll, matching _regime_multiplier's "
      "own hysteresis rather than a second, noisier source of truth",
      "if _label is not None and _label != regime_timeline_last_label:" in src)

import json, tempfile, os as _os

n2 = EmailNotifier.__new__(EmailNotifier)
tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
try:
    json.dump({
        "date": __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
        "events": [
            {"time": "09:41:00", "label": "bullish"},
            {"time": "11:15:00", "label": "bearish"},
            {"time": "13:02:00", "label": "choppy"},
        ],
    }, tf)
    tf.close()
    html_rt = n2._regime_timeline_html(timeline_file=tf.name)
    check("renders a non-empty timeline for today's data", bool(html_rt))
    check("one colored cell per label, using the SAME colors main.py's own "
          "log lines imply (green=bullish, red=bearish)",
          "#10b981" in html_rt and "#ef4444" in html_rt and "#f59e0b" in html_rt)
    check("a gray lead-in segment covers 9:30 up to the first recorded read "
          "(09:41) - unknown is shown honestly, not guessed at",
          "#e5e7eb" in html_rt and "no read yet" in html_rt)
    check("the legend names all four regime labels",
          all(w in html_rt.upper() for w in ("BULLISH", "BEARISH", "NEUTRAL", "CHOPPY")))
    check("segment tooltips carry the actual start time and label, so "
          "hovering tells a reader exactly when a transition happened",
          "11:15 ET: BEARISH" in html_rt)
    check("every transition has a visible tick (border-left) on the bar "
          "itself, not just a hover title a phone can't reach - "
          "2026-09-30 explicit user request",
          "border-left:1px solid" in html_rt)
    check("a plain-text chronological line lists every transition's exact "
          "time and label, readable without hovering anything",
          "09:41 BULLISH" in html_rt and "11:15 BEARISH" in html_rt
          and "13:02 CHOPPY" in html_rt)

    # Stale-date guard: same file, but dated yesterday.
    tf2 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    import datetime as _dt
    json.dump({"date": (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d"),
               "events": [{"time": "09:41:00", "label": "bullish"}]}, tf2)
    tf2.close()
    check("a timeline stamped for a DIFFERENT day is refused, not shown as "
          "if it were today's", n2._regime_timeline_html(timeline_file=tf2.name) == "")
    _os.unlink(tf2.name)

    check("missing file -> no section, not an exception",
          n2._regime_timeline_html(timeline_file="/tmp/does-not-exist-regime.json") == "")

    tf3 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"date": __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
               "events": []}, tf3)
    tf3.close()
    check("empty events list -> no section", n2._regime_timeline_html(timeline_file=tf3.name) == "")
    _os.unlink(tf3.name)
finally:
    _os.unlink(tf.name)

check("wired into the assembled report", "regime_timeline_html" in esrc)
check("shown on every send, not just end-of-day - a partial-day timeline "
      "is still informative on a midday send, unlike performance_timeline_html",
      "regime_timeline_html = self._regime_timeline_html()" in esrc)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
