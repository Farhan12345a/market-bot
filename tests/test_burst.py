"""Burst throttle + signal journal + report column."""
import sys, os, types, tempfile, csv
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
from datetime import datetime, timedelta
import pytz
import src.main as m
from src.main import _burst_policy, _summarise_burst_notes, _compute_rvol, _window_pct_change
from src.analytics.signal_journal import SignalJournal, JOURNAL_FIELDS

ET = pytz.timezone("America/New_York")
fails = []
def check(n, c, d=""):
    print(("PASS  " if c else "FAIL  ") + n + ("" if c else f"   <- {d}")); (c or fails.append(n))

BC = {"trading": {"use_burst_throttle": True, "burst_width_threshold": 5,
                  "burst_max_entries": 3, "burst_size_multiplier": 0.5}}

print("=== 12. BURST POLICY ===")
mx, mult, note = _burst_policy(BC, 2)
check("below threshold -> untouched", mx is None and mult == 1.0, (mx, mult))
mx, mult, note = _burst_policy(BC, 4)
check("just below threshold -> untouched", mx is None and mult == 1.0)
mx, mult, note = _burst_policy(BC, 5)
check("at threshold -> throttled", mx == 3 and mult == 0.5, (mx, mult))
mx, mult, note = _burst_policy(BC, 20)
check("large burst -> throttled to 3 @ 0.5x", mx == 3 and mult == 0.5)
check("note is descriptive", "THROTTLED" in note and "20" in note, note)
off = {"trading": dict(BC["trading"], use_burst_throttle=False)}
mx, mult, note = _burst_policy(off, 20)
check("disabled -> no throttle at any width", mx is None and mult == 1.0)

print("\n=== 13. DAY SUMMARY STRING ===")
notes = [_burst_policy(BC, w)[2] for w in (1, 2, 9, 20, 3)]
summ = _summarise_burst_notes(BC, notes)
check("summary reports engagement count", "2 of 5" in summ, summ)
check("summary states the settings", "5" in summ and "3" in summ and "0.5x" in summ, summ)
check("disabled summary is explicit", "disabled" in _summarise_burst_notes(off, notes).lower())

print("\n=== 14. FEATURE HELPERS ===")
check("rvol needs history", _compute_rvol({"volume": 100}, []) is None)
check("rvol computes vs average", _compute_rvol({"volume": 300}, [100, 100, 100]) == 3.0,
      _compute_rvol({"volume": 300}, [100, 100, 100]))
check("rvol ignores zero-volume bars", _compute_rvol({"volume": 0}, [100, 100, 100]) is None)
t0 = datetime.now(ET)
check("window pct change", _window_pct_change([(t0, 100.0), (t0, 101.5)]) == 1.5)
check("window pct needs 2 samples", _window_pct_change([(t0, 100.0)]) is None)

print("\n=== 15. SIGNAL JOURNAL ===")
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "sig.csv")
CFG = {"analytics": {"log_signals": True, "signal_log_file": path, "forward_return_minutes": [15, 30]}}
j = SignalJournal(CFG)
j.record(symbol="AAA", entry_method="RAPID_INCREASE_IMMEDIATE", price=10.0, signal_pct=0.8,
         spy_pct=0.1, excess_vs_spy_pct=0.7, rvol=2.4, spread_pct=0.05,
         burst_width=12, taken=True, skip_reason=None, size_multiplier=0.5)
j.record(symbol="BBB", entry_method="THREE_BAR_MOMENTUM", price=20.0, signal_pct=None,
         burst_width=12, taken=False, skip_reason="burst_throttle", size_multiplier=0.5)
check("both taken and skipped recorded", j.stats()["pending"] == 2, j.stats())
check("taken count tracked", j.stats()["taken"] == 1, j.stats())

# forward returns not yet due
j.update_forward_returns(lambda s: 11.0)
check("forward return not filled before horizon", j._pending[0]["row"]["pct_15min"] is None)
# age both rows past 15 min but not 30
for e in j._pending: e["born"] = datetime.now() - timedelta(minutes=16)
j.update_forward_returns(lambda s: 11.0 if s == "AAA" else 19.0)
check("15min filled once due", j._pending[0]["row"]["pct_15min"] == 10.0, j._pending[0]["row"]["pct_15min"])
check("30min still pending", j._pending[0]["row"]["pct_30min"] is None)
check("skipped signal ALSO gets forward return (the control group)",
      j._pending[1]["row"]["pct_15min"] == -5.0, j._pending[1]["row"]["pct_15min"])

j.flush()
check("csv written", os.path.exists(path))
rows = list(csv.DictReader(open(path)))
check("both rows persisted", len(rows) == 2, len(rows))
check("all fields present", set(rows[0]) == set(JOURNAL_FIELDS))
check("skip_reason preserved", rows[1]["skip_reason"] == "burst_throttle", rows[1]["skip_reason"])
check("buffer cleared after flush", j.stats()["pending"] == 0)

# second day appends without a duplicate header
j2 = SignalJournal(CFG); j2.record(symbol="CCC", price=5.0, taken=False, skip_reason="max_daily_entries")
j2.flush()
check("appends without duplicate header", len(list(csv.DictReader(open(path)))) == 3)

print("\n=== 16. JOURNAL IS NON-FATAL ===")
bad = SignalJournal({"analytics": {"log_signals": True, "signal_log_file": "/proc/x/y.csv"}})
bad.record(symbol="Z", price=1.0)
try:
    bad.update_forward_returns(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    check("raising price_lookup does not propagate", True)
except Exception as e:
    check("raising price_lookup does not propagate", False, repr(e))
check("unwritable path returns None, no raise", bad.flush() is None)
off_j = SignalJournal({"analytics": {"log_signals": False}})
off_j.record(symbol="Z", price=1.0)
check("disabled journal records nothing", off_j.stats()["pending"] == 0)

print("\n=== 17. REPORT COLUMN ===")
from src.notifications.email_notifier import EmailNotifier
n = EmailNotifier({"notifications": {"report_dir": tmp, "email": {"enabled": False}}})
html = n._generate_html_summary(
    [{"symbol": "AAA", "entry_price": 10, "exit_price": 11, "qty": 5, "pl": 5, "pl_pct": 10,
      "exit_reason": "TRAILING_STOP", "entry_method": "RAPID_INCREASE_IMMEDIATE",
      "burst_logic": "THROTTLED: burst=12 >= 5, took <= 3 at 0.5x size", "stop_loss_used": False}],
    burst_summary="Burst throttle ON (>= 5 simultaneous signals -> take at most 3 at 0.5x size). Engaged on 2 of 5 entry-window polls.")
check("column header present", "<th>Bursting Logic</th>" in html)
check("per-trade burst logic rendered", "burst=12" in html)
check("day-level description rendered", "Engaged on 2 of 5" in html)
html2 = n._generate_html_summary([{"symbol":"B","entry_price":1,"exit_price":1,"qty":1,"pl":0,"pl_pct":0,
                                   "exit_reason":"X","entry_method":"Y","stop_loss_used":False}])
check("missing burst data degrades to n/a", "n/a" in html2)

import shutil; shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
