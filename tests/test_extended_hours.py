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
    {"symbol": "TSLA", "entry_window": "extended", "entry_time": "2026-09-27T15:25:00",
     "exit_time": "2026-09-27T15:30:00", "entry_price": 250.0, "exit_price": 248.25,
     "qty": 20, "pl": -35.0, "pl_pct": -0.7, "exit_reason": "FIRST_EXIT_-0.7%", "side": "short"},
    {"symbol": "MSFT", "entry_window": "primary", "pl": 20.0, "pl_pct": 0.5},
    {"symbol": "NFLX", "entry_method": "OPENING_MOVE", "pl": 15.0, "pl_pct": 0.3},
]
html = n._extended_hours_html(trades)
check("section renders when extended-hours trades exist", bool(html))
check("total P&L is the SUM OF ONLY THE EXTENDED TRADES ($57.00), not all four",
      "$57.00" in html, html[:400])
check("the primary-window and opening-burst trades are excluded from this section",
      "MSFT" not in html and "NFLX" not in html)
check("both extended trades ARE shown", "AAPL" in html and "TSLA" in html)
check("a short is visibly tagged as one", "TSLA (SHORT)" in html or "TSLA</strong> (SHORT)" in html
      or "TSLA(SHORT)" in html or "(SHORT)" in html.split("TSLA")[1][:20], html)
check("win/loss counts shown", "1W / 1L" in html, html[:600])
check("it never claims to be summed with the primary window",
      "never summed" in html.lower())
check("no extended-hours trades -> no section at all (a quiet day, or the "
      "feature is off)",
      n._extended_hours_html([{"symbol": "X", "entry_window": "primary", "pl": 1}]) == "")
check("empty input -> no section", n._extended_hours_html([]) == "")
check("old trades with no entry_window key at all are correctly treated as "
      "primary (not extended) - the safe default for pre-2026-09-27 rows",
      n._extended_hours_html([{"symbol": "OLD", "pl": 5}]) == "")

print("\n=== E. LIVE CONFIG ===")
eh = CFG["trading"].get("extended_hours_experiment") or {}
check("extended_hours_experiment.enabled is on", eh.get("enabled") is True)
check("end_time reaches to the close", eh.get("end_time") == "16:00")
check("entry_window_end (the PRIMARY window) is unchanged at 10:15",
      CFG["trading"]["entry_window_end"] == "10:15")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
