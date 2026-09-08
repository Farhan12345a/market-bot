"""
2026-09-08: the daily-P&L-vs-active-settings chart added to the EOD email.

Two things are tested together because they are two halves of one feature
and neither is meaningful alone:

  1. `_write_daily_summary_csv` (src/main.py) - now writes several new
     columns and, more importantly, now REWRITES the whole file instead of
     appending, specifically so a growing column set (this is not the first
     time it has grown) can never silently misalign an old row against a new
     header. That migration path had zero test coverage before this file,
     the same gap this project keeps re-finding (see test_phantom_exit.py's
     section 7/8, the TSLA retry-cancel bug).
  2. `render_performance_timeline_html` (src/analytics/performance_timeline.py)
     - reads exactly the file (1) writes, so a fixture mismatch between the
     two would be invisible to either half tested in isolation.
"""
import copy
import csv
import os
import sys
import types
from datetime import datetime

import pytz
import yaml

from _repo import REPO, CONFIG, repo_file, sandbox_cwd

sandbox_cwd()

import src.main as M
from src.analytics.performance_timeline import (
    render_performance_timeline_html, _load_rows, TRACKED_COLUMNS,
)

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


class FakeAccount:
    def __init__(self, cash=100000.0): self.cash = str(cash)


class FakeBroker:
    def __init__(self, cash=100000.0): self._cash = cash
    def get_account(self): return FakeAccount(self._cash)


class FakeMarketData:
    def __init__(self, cash=100000.0): self.broker = FakeBroker(cash)


class FakeExecutor:
    def __init__(self, trades_log): self.trades_log = trades_log


def trade(symbol, pl, exit_date):
    return {"symbol": symbol, "pl": pl, "exit_time": f"{exit_date}T10:00:00"}


def write_day(cfg, filepath, date_str, pl, cash=100000.0, trades=None):
    """One call = one simulated finish_day() write for `date_str`."""
    ex = FakeExecutor(trades or [trade("X", pl, date_str)])
    md = FakeMarketData(cash)
    fixed_now = ET.localize(datetime.strptime(date_str, "%Y-%m-%d").replace(hour=16))
    orig_now = datetime.now
    try:
        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)
        M.datetime = _FrozenDT
        M._write_daily_summary_csv(
            cfg, ex, ["A", "B"], 1, cash, md, ET, filepath=filepath,
        )
    finally:
        M.datetime = orig_now


print("=== 1. FIRST WRITE: HEADER + ONE ROW ===")
tmp_csv = os.path.join(os.getcwd(), "logs", "daily_summary_test.csv")
if os.path.exists(tmp_csv):
    os.remove(tmp_csv)
write_day(CFG, tmp_csv, "2026-09-01", 100.0)
with open(tmp_csv, newline="") as f:
    rows = list(csv.DictReader(f))
check("one row written", len(rows) == 1, rows)
check("total_pl recorded", rows[0]["total_pl"] == "100.0", rows[0]["total_pl"])
check("total_pl_pct computed from starting_cash", rows[0]["total_pl_pct"] == "0.1", rows[0]["total_pl_pct"])
check("new tracked columns present in the header",
      all(col in rows[0] for col, _ in TRACKED_COLUMNS), list(rows[0]))
check("take_profit_tiers rendered as a compact string",
      "%" in rows[0]["take_profit_tiers"], rows[0]["take_profit_tiers"])

print("\n=== 2. SECOND WRITE: APPENDS, DOES NOT CLOBBER THE FIRST ===")
write_day(CFG, tmp_csv, "2026-09-02", -50.0)
with open(tmp_csv, newline="") as f:
    rows2 = list(csv.DictReader(f))
check("two rows now", len(rows2) == 2, len(rows2))
check("the first row survived the rewrite", rows2[0]["date"] == "2026-09-01", rows2[0])
check("the second row is the new one", rows2[1]["date"] == "2026-09-02" and rows2[1]["total_pl"] == "-50.0")

print("\n=== 3. OLD-SCHEMA ROW MIGRATES CLEANLY (the real regression this guards) ===")
old_header = ["date", "total_pl", "starting_cash", "ending_cash", "trades_count",
              "wins", "losses", "win_rate_pct", "entries_triggered",
              "symbols_watched_count", "symbols_watched", "symbols_traded",
              "use_pullback_entry", "use_three_bar_momentum", "use_rsi_filter",
              "rapid_increase_config", "final_stop_loss_pct",
              "first_scale_out_config", "trailing_stop_pct", "entry_window"]
old_csv = os.path.join(os.getcwd(), "logs", "daily_summary_old.csv")
with open(old_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=old_header)
    w.writeheader()
    w.writerow({"date": "2026-08-27", "total_pl": "337.0", "starting_cash": "90000",
                "ending_cash": "90337", "trades_count": "5", "wins": "4", "losses": "1",
                "win_rate_pct": "80.0", "entries_triggered": "5",
                "symbols_watched_count": "14", "symbols_watched": "A|B",
                "symbols_traded": "A|B", "use_pullback_entry": "False",
                "use_three_bar_momentum": "True", "use_rsi_filter": "False",
                "rapid_increase_config": "0.4% / 3min", "final_stop_loss_pct": "-1.0",
                "first_scale_out_config": "-0.5% / 50%", "trailing_stop_pct": "0.5",
                "entry_window": "09:33-10:15"})
write_day(CFG, old_csv, "2026-09-01", 150.0)
with open(old_csv, newline="") as f:
    migrated = list(csv.DictReader(f))
check("old row preserved after the schema migration", len(migrated) == 2, len(migrated))
check("old row's original data is intact",
      migrated[0]["date"] == "2026-08-27" and migrated[0]["total_pl"] == "337.0", migrated[0])
check("old row's NEW columns backfilled empty, not crashed or dropped",
      migrated[0].get("take_profit_tiers", "MISSING") == "", migrated[0])
check("new row has the new columns populated",
      migrated[1]["date"] == "2026-09-01" and migrated[1].get("take_profit_tiers"), migrated[1])
check("header now matches the CURRENT schema exactly",
      list(migrated[0].keys()) == list(migrated[1].keys()))

print("\n=== 4. A BROKER READ FAILURE (no starting_cash) DOES NOT CRASH THE WRITE ===")
no_cash_csv = os.path.join(os.getcwd(), "logs", "daily_summary_nocash.csv")
write_day(CFG, no_cash_csv, "2026-09-01", 42.0, cash=None)
with open(no_cash_csv, newline="") as f:
    r = list(csv.DictReader(f))
check("row still written with no starting cash", len(r) == 1, r)
check("total_pl_pct is blank, not a crash or a fake 0", r[0]["total_pl_pct"] == "", r[0]["total_pl_pct"])

print("\n=== 5. render_performance_timeline_html: EMPTY / MISSING / ONE ROW ===")
check("missing file -> empty section, no raise", render_performance_timeline_html("logs/does_not_exist.csv") == "")
one_row_csv = os.path.join(os.getcwd(), "logs", "daily_summary_one.csv")
write_day(CFG, one_row_csv, "2026-09-01", 10.0)
check("a single day is not a timeline yet -> empty section",
      render_performance_timeline_html(one_row_csv) == "")

corrupt_csv = os.path.join(os.getcwd(), "logs", "daily_summary_corrupt.csv")
with open(corrupt_csv, "wb") as f:
    f.write(b"\xff\xfe\x00not,valid,csv\x00\xff")
check("unreadable/corrupt file -> empty section, no raise",
      render_performance_timeline_html(corrupt_csv) == "")

print("\n=== 6. TWO+ ROWS, NO SETTINGS CHANGE -> RENDERS, NO MARKER ===")
write_day(CFG, one_row_csv, "2026-09-02", -20.0)
out = render_performance_timeline_html(one_row_csv)
check("section is non-empty with 2+ rows", bool(out))
check("renders an SVG", "<svg" in out)
check("renders both dates", "2026-09-01" in out and "2026-09-02" in out)
check("no change marker with identical settings both days", "settings changed" not in out, out[:2000])
check("negative day rendered in red", "#ef4444" in out)
check("positive day rendered in green", "#10b981" in out)

print("\n=== 7. A SETTINGS CHANGE PRODUCES A MARKER WITH THE RIGHT DIFF ===")
changed_csv = os.path.join(os.getcwd(), "logs", "daily_summary_changed.csv")
cfg_a = copy.deepcopy(CFG)
cfg_a["trading"]["max_positions_per_sector"] = 3
write_day(cfg_a, changed_csv, "2026-09-01", 30.0)
cfg_b = copy.deepcopy(CFG)
cfg_b["trading"]["max_positions_per_sector"] = 2
write_day(cfg_b, changed_csv, "2026-09-02", -10.0)
out2 = render_performance_timeline_html(changed_csv)
check("a marker appears for the changed day", "settings changed" in out2, out2[:3000])
check("the diff names the actual old -> new values", "3 -&gt; 2" in out2 or "3 -> 2" in out2, out2[:3000])
check("the table highlights the changed row", "#eef2ff" in out2)

print("\n=== 8. A COLUMN MISSING ON THE OLDER ROW IS NOT TREATED AS A CHANGE ===")
# Reuses the migrated old_csv from section 3: 2026-08-27 has no
# take_profit_tiers value at all (pre-dates the column), 2026-09-01 does.
# That must not be reported as "" -> "1.0/1.25/1.5%", which would fire a
# spurious marker on every single pre-migration boundary forever.
out3 = render_performance_timeline_html(old_csv)
check("blank-vs-populated on an old column is not reported as a change",
      "Take-profit:" not in out3, out3[:3000])

print("\n=== 9. DUPLICATE DATE (SAME-DAY RE-RUN) IS DEDUPED, NOT DOUBLE-COUNTED ===")
dup_csv = os.path.join(os.getcwd(), "logs", "daily_summary_dup.csv")
write_day(CFG, dup_csv, "2026-09-01", 10.0)
write_day(CFG, dup_csv, "2026-09-01", 55.0)   # same date, e.g. process restart
write_day(CFG, dup_csv, "2026-09-02", -5.0)
rows_loaded = _load_rows(dup_csv)
check("duplicate date collapsed to one row", len(rows_loaded) == 2, len(rows_loaded))
check("the LAST write for that date wins", rows_loaded[0]["total_pl"] == "55.0", rows_loaded[0])
out4 = render_performance_timeline_html(dup_csv)
check("renders fine with a deduped history", bool(out4) and "<svg" in out4)

print("\n=== 10. MANY DAYS -> TRUNCATED TO max_days, NEWEST KEPT ===")
many_csv = os.path.join(os.getcwd(), "logs", "daily_summary_many.csv")
for day in range(1, 71):
    write_day(CFG, many_csv, f"2026-{(day // 28) + 1:02d}-{(day % 28) + 1:02d}", float(day))
out5 = render_performance_timeline_html(many_csv, max_days=10)
loaded_many = _load_rows(many_csv)
check("all 70 days were written to disk", len(loaded_many) == 70, len(loaded_many))
check("render_performance_timeline_html still returns a bounded section",
      bool(out5) and out5.count("<rect") <= 12, out5.count("<rect"))

print("\n=== 11. HTML/XSS-UNSAFE CHARACTERS ARE ESCAPED, NOT INJECTED ===")
xss_csv = os.path.join(os.getcwd(), "logs", "daily_summary_xss.csv")
cfg_x = copy.deepcopy(CFG)
cfg_x["trading"]["max_positions_per_sector"] = "<script>alert(1)</script>"
write_day(cfg_x, xss_csv, "2026-09-01", 5.0)
write_day(CFG, xss_csv, "2026-09-02", 5.0)
out6 = render_performance_timeline_html(xss_csv)
check("a raw <script> tag never reaches the output", "<script>" not in out6, out6)
check("it is present only escaped", "&lt;script&gt;" in out6, out6)

print("\n=== 12. WIRED INTO THE ACTUAL EMAIL, GATED BY LABEL ===")
import tempfile
from src.notifications.email_notifier import EmailNotifier
tmp_report_dir = tempfile.mkdtemp()
en_cfg = copy.deepcopy(CFG)
en_cfg["notifications"]["report_dir"] = tmp_report_dir
en = EmailNotifier(en_cfg)
tr = [{"symbol": "A", "entry_price": 10, "exit_price": 10.1, "qty": 10, "pl": 50.0,
       "pl_pct": 1.0, "exit_reason": "TAKE_PROFIT", "entry_method": "X",
       "burst_logic": "", "stop_loss_used": False}]

# Point the module-level default at our populated fixture so the email
# builder (which calls render_performance_timeline_html() with no args)
# actually has something to render, without touching the real logs/ dir.
import src.notifications.email_notifier as EN
_orig_render = EN.render_performance_timeline_html
EN.render_performance_timeline_html = lambda *a, **k: _orig_render(changed_csv)
try:
    h_daily = en._generate_html_summary(tr, label="Daily Summary")
    h_closing = en._generate_html_summary(tr, label="Closing Report")
    h_midday = en._generate_html_summary(tr, label="Midday Status")
finally:
    EN.render_performance_timeline_html = _orig_render

check("Daily Summary includes the performance timeline",
      "Daily P&amp;L vs. Active Settings" in h_daily)
check("Closing Report includes it too", "Daily P&amp;L vs. Active Settings" in h_closing)
check("Midday Status does NOT - today's row is not written yet at that point",
      "Daily P&amp;L vs. Active Settings" not in h_midday)
check("the rest of the report is still intact alongside it",
      "Closed Trades" in h_daily and "Realized P&amp;L" in h_daily)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
