"""
ops/mae-percentiles.py: the MAE-percentile analysis layer queued in
PENDING_WORK.md item 3 (dynamic ATR/MAE-based stops). Verifies the
percentile math against a fixture with known values (not the log by eye),
the per-symbol --min-n filter, and that it stays a read-only analysis tool -
nothing here touches the live exit path.
"""
import csv, subprocess, sys
from _repo import REPO, repo_file

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


FIELDS = [
    "date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
    "price_source", "signal_pct", "post_exit_pct", "post_exit_note", "entry_rsi",
    "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
    "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct", "list_source",
]


def row(date, sym, mae):
    r = {k: "" for k in FIELDS}
    r.update(date=date, symbol=sym, entry_price=100.0, entry_method="RAPID_INCREASE_IMMEDIATE",
             mae_pct=mae, exit_reason="TRAILING_STOP", qty=100)
    return [r[k] for k in FIELDS]


def write_fixture(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for r in rows:
            w.writerow(r)


def run(path, *args):
    return subprocess.run(
        [sys.executable, repo_file("ops", "mae-percentiles.py"), "--trades", path, *args],
        capture_output=True, text=True, cwd=REPO,
    )


fixture = "/tmp/claude_test_mae_history.csv"

# HOOD: mae_pct -0.1 .. -1.0, ten trades, chosen so the nearest-rank
# percentile math is checkable by hand.
#   sorted ascending: -1.0,-0.9,...,-0.1
#   P50 -> k=ceil(0.5*10)=5  -> vals[4]  = -0.6
#   P75 -> k=ceil(0.25*10)=3 -> vals[2]  = -0.8
#   P90 -> k=ceil(0.10*10)=1 -> vals[0]  = -1.0
#   P95 -> k=ceil(0.05*10)=1 -> vals[0]  = -1.0
rows = [row("2026-08-24", "HOOD", -0.1 * i) for i in range(1, 11)]
# CRM: only 3 trades - below the default --min-n (15), must not get a row.
rows += [row("2026-08-25", "CRM", v) for v in (-0.2, -0.3, -0.1)]
# One trade with no mae_pct at all - must not crash or count toward n.
rows += [row("2026-08-25", "HOOD", "")]
write_fixture(fixture, rows)

print("=== 1. PER-SYMBOL PERCENTILE MATH, --symbol BYPASSES --min-n ===")
r = run(fixture, "--symbol", "HOOD")
check("ran cleanly", r.returncode == 0, r.stderr)
check("n=10 (the blank mae_pct row is excluded, not counted or crashed on)",
      "n=10" in r.stdout, r.stdout)
check("P50 = -0.600%", "P50=-0.600%" in r.stdout, r.stdout)
check("P75 = -0.800%", "P75=-0.800%" in r.stdout, r.stdout)
check("P90 = -1.000%", "P90=-1.000%" in r.stdout, r.stdout)
check("P95 = -1.000% (same rank as P90 at this n - nearest-rank, not interpolated)",
      "P95=-1.000%" in r.stdout, r.stdout)

print("\n=== 2. --min-n HIDES A THIN SYMBOL, POOLED ROW STILL SHOWS ===")
r = run(fixture)
check("ALL (pooled) row covers all 13 mae-bearing trades", "ALL" in r.stdout and "n=13" in r.stdout, r.stdout)
check("CRM (n=3) is below the default min-n=15 and is not listed as its own row",
      "\nCRM " not in r.stdout, r.stdout)
check("HOOD (n=10) is ALSO below min-n=15 by default, so neither symbol gets its own row",
      "none - every symbol has fewer than 15 trades" in r.stdout, r.stdout)
check("...but says which symbols exist and their n, so it's not silent about why",
      "HOOD n=10" in r.stdout and "CRM n=3" in r.stdout, r.stdout)

print("\n=== 3. --min-n LOWERED LETS HOOD THROUGH BUT NOT CRM ===")
r = run(fixture, "--min-n", "5")
check("HOOD (n=10) now gets its own row", "HOOD" in r.stdout and "n=10" in r.stdout, r.stdout)
check("CRM (n=3) still doesn't", "CRM" not in r.stdout.split("Per symbol")[1] if "Per symbol" in r.stdout else True)

print("\n=== 4. --since FILTERS ===")
r = run(fixture, "--symbol", "CRM", "--since", "2026-08-26")
check("filtering past every CRM row leaves nothing to report",
      "Nothing to report" in r.stdout or "n=0" in r.stdout, r.stdout)

print("\n=== 4b. EVERY HISTORICAL SCHEMA WIDTH IS PARSED, NOT DROPPED ===")
# Rows are matched by WIDTH, so a schema missing from TRADE_FIELDS_HISTORY is
# silently DISCARDED rather than mis-parsed. Only the 21-wide legacy was
# declared until 2026-09-02, which threw away every width-17 and width-18 row
# - 124 of 378 on the live history, a third of the record, and BOTH of those
# schemas carry mae_pct. Verified here against rows in the real column order
# recovered from git (executor.py fieldnames at 5691a2d and 62605f4).
W18 = ["2026-08-23", "HOOD", "2026-08-23T09:35:00", "50.0", "THREE_BAR_MOMENTUM",
       "THROTTLED: burst=25", "unknown", "", "0.9921", "-0.62",
       "2026-08-23T09:50:00", "50.9", "TIME_STOP_4PM", "False", "", "84",
       "75.60", "1.80"]
W17 = ["2026-08-21", "CRM", "2026-08-21T09:35:00", "50.0", "THREE_BAR_MOMENTUM",
       "THROTTLED: burst=25", "", "0.8", "-0.44", "2026-08-21T09:50:00",
       "50.9", "TIME_STOP_4PM", "False", "", "84", "75.60", "1.80"]
mixed = "/tmp/claude_test_mae_mixed.csv"
with open(mixed, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(FIELDS)                       # 22-wide header
    w.writerow(row("2026-08-30", "NEW", -0.9))   # current schema
    w.writerow(W18)
    w.writerow(W17)
r = run(mixed, "--symbol", "HOOD")
check("a width-18 row is parsed, not skipped", "n=1" in r.stdout, r.stdout)
check("...and its mae_pct lands in the right column",
      "-0.620%" in r.stdout, r.stdout)
r = run(mixed, "--symbol", "CRM")
check("a width-17 row is parsed too", "n=1" in r.stdout, r.stdout)
check("...with its mae_pct correctly placed", "-0.440%" in r.stdout, r.stdout)
r = run(mixed)
check("all three schema generations count toward the pooled distribution",
      "3 of 3 trades carry mae_pct" in r.stdout, r.stdout)
check("nothing is reported as unrecognized any more",
      "unrecognized column count" not in r.stderr, r.stderr)

print("\n=== 5. EMPTY / MISSING FILE -> CLEAN, NOT A TRACEBACK ===")
r = run("/tmp/claude_test_mae_history_does_not_exist.csv")
check("non-zero exit on a missing file", r.returncode != 0)
check("no traceback", "Traceback" not in r.stderr, r.stderr)

print("\n=== 6. READ-ONLY: nothing here touches the live exit path ===")
main_src = open(repo_file("src", "main.py")).read()
check("main.py does not import or shell out to this analysis script",
      "mae-percentiles" not in main_src and "mae_percentiles" not in main_src)
exec_src = open(repo_file("src", "executor", "executor.py")).read()
check("executor.py does not either - no live stop reads this file's output",
      "mae-percentiles" not in exec_src and "mae_percentiles" not in exec_src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
