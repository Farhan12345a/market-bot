"""
ops/breadth-counterfactual.py: does the split-at-a-time-of-day logic actually
work, on a fixture the test controls rather than trusting the output by eye.

Built to answer "did the breadth halt cost real winners" - the question asked
after 2026-09-01, where the halt fired at 09:45 and the user pointed out PLTR
and ADBE trended up after 10am. That is exactly the claim this tool checks
against the signal journal instead of the naked eye: the average AFTER the
split time, and any named symbols broken out individually, since the average
and one loud anecdote can both be true.
"""
import copy, csv, subprocess, sys
from _repo import REPO, repo_file

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


FIELDS = [
    "date", "signal_time", "symbol", "entry_method", "price",
    "signal_pct", "excess_vs_spy_pct", "spy_pct", "rvol", "spread_pct",
    "burst_width",
    "opening_hit_rate", "opening_avg_gain", "opening_sessions",
    "opening_efficiency", "opening_directional",
    "cf_efficiency", "cf_rel_strength", "cf_vol_accel", "cf_vwap_pos",
    "cf_exhaustion", "cf_breakout", "cf_rvol", "cf_spread", "cf_vwap",
    "cf_sector_strength", "cf_sector_etf",
    "cf_score",
    "taken", "skip_reason", "qty", "size_multiplier",
    "price_15min", "pct_15min", "price_30min", "pct_30min",
]


def row(date, t, sym, taken, p15, p30):
    # signal_time is a full ISO datetime (now.isoformat()) in the real
    # journal, not a bare time - the fixture must match, since a bare "09:50:00"
    # is exactly the shape that hid the [:5]-year bug this file caught.
    r = {k: 0 for k in FIELDS}
    r.update(date=date, signal_time=f"{date}T{t}-04:00", symbol=sym, entry_method="RAPID_INCREASE",
             price=100.0, signal_pct=0.3, taken=taken,
             skip_reason="" if taken == "1" else "breadth_halt",
             qty=10 if taken == "1" else 0, size_multiplier=1,
             price_15min=100 * (1 + p15 / 100), pct_15min=p15,
             price_30min=100 * (1 + p30 / 100), pct_30min=p30)
    return [r[k] for k in FIELDS]


def write_fixture(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for r in rows:
            w.writerow(r)


def run(path, *args):
    return subprocess.run(
        [sys.executable, repo_file("ops", "breadth-counterfactual.py"),
         "--file", path, *args],
        capture_output=True, text=True, cwd=REPO,
    )


fixture = "/tmp/claude_test_journal.csv"

print("=== 1. THE SPLIT ITSELF ===")
write_fixture(fixture, [
    row("2026-09-01", "09:40:00", "AAA", "1", 1.0, 2.0),   # before the split
    row("2026-09-01", "09:50:00", "PLTR", "0", 0.8, 1.5),  # after, positive
    row("2026-09-01", "10:00:00", "ADBE", "0", 0.6, 1.2),  # after, positive
])
res = run(fixture, "--date", "2026-09-01", "--after", "09:45")
check("it runs cleanly", res.returncode == 0, res.stderr)
check("one signal counted before the split", "signals before 09:45: 1" in res.stdout, res.stdout)
check("two signals counted after it", "signals at/after 09:45: 2" in res.stdout, res.stdout)
check("the after-split mean is positive and visible",
      "+0.700%" in res.stdout, res.stdout)

print("\n=== 2. NAMED SYMBOLS BREAK OUT INDIVIDUALLY ===")
res = run(fixture, "--date", "2026-09-01", "--after", "09:45", "--symbols", "PLTR,ADBE")
check("PLTR's own row appears", "PLTR   09:50" in res.stdout, res.stdout)
check("ADBE's own row appears", "ADBE   10:00" in res.stdout, res.stdout)
check("...with its actual forward return, not the aggregate",
      "15m +0.80%" in res.stdout and "15m +0.60%" in res.stdout, res.stdout)
res_none = run(fixture, "--date", "2026-09-01", "--after", "09:45", "--symbols", "ZZZQ")
check("a symbol with no signals says so rather than erroring",
      "no signals at/after 09:45" in res_none.stdout, res_none.stdout)

print("\n=== 3. NOTHING FIRED AFTER THE SPLIT ===")
write_fixture(fixture, [row("2026-09-01", "09:40:00", "AAA", "1", 1.0, 2.0)])
res = run(fixture, "--date", "2026-09-01", "--after", "09:45")
check("it says the halt cost nothing rather than crashing on empty data",
      "cost nothing" in res.stdout, res.stdout)

print("\n=== 4. TAKEN VS SKIPPED SPLIT WITHIN THE AFTER GROUP ===")
write_fixture(fixture, [
    row("2026-09-01", "09:50:00", "BBB", "1", 1.0, 2.0),   # taken, after
    row("2026-09-01", "09:55:00", "CCC", "0", -1.0, -2.0),  # skipped, after
])
res = run(fixture, "--date", "2026-09-01", "--after", "09:45")
check("taken and skipped are reported separately, not merged",
      "taken" in res.stdout and "skipped" in res.stdout, res.stdout)
check("the skipped row's negative return shows up correctly",
      "-1.000%" in res.stdout, res.stdout)

print("\n=== 5. THE V1 (PRE-2026-08-26) SCHEMA STILL READS ===")
V1 = ["date", "signal_time", "symbol", "entry_method", "price",
      "signal_pct", "excess_vs_spy_pct", "spy_pct", "rvol", "spread_pct",
      "burst_width", "taken", "skip_reason", "qty", "size_multiplier",
      "price_15min", "pct_15min", "price_30min", "pct_30min"]
with open(fixture, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(V1)
    w.writerow(["2026-09-01", "2026-09-01T09:50:00-04:00", "OLD", "RAPID_INCREASE", 50.0, 0.3,
                0, 0, 0, 0, 0, "0", "breadth_halt", 0, 0, 50.5, 1.0, 51.0, 2.0])
res = run(fixture, "--date", "2026-09-01", "--after", "09:45")
check("an old-schema row is read, not silently dropped",
      "signals at/after 09:45: 1" in res.stdout, res.stdout)

print("\n=== 6. hhmm() PARSES THE REAL FORMAT, NOT A GUESS ===")
from importlib import util as _ilu
spec = _ilu.spec_from_file_location("bc", repo_file("ops", "breadth-counterfactual.py"))
bc = _ilu.module_from_spec(spec)
spec.loader.exec_module(bc)
# The exact shape signal_journal.py writes: now.isoformat().
check("a real isoformat timestamp yields HH:MM",
      bc.hhmm("2026-09-01T09:45:12.345678-04:00") == "09:45",
      bc.hhmm("2026-09-01T09:45:12.345678-04:00"))
# The bug this file caught in production: slicing [:5] off an isoformat
# string gives the YEAR ("2026-"), which sorts after any bare "HH:MM" and
# silently put every 2026-09-01 signal in the "after" bucket regardless of
# --after. Confirm the wrong answer is no longer possible.
check("...and it is NOT the year", bc.hhmm("2026-09-01T09:45:12-04:00") != "2026-",
      bc.hhmm("2026-09-01T09:45:12-04:00"))
check("a space-separated timestamp also works",
      bc.hhmm("2026-09-01 09:45:12") == "09:45")
check("a bare HH:MM:SS (no date prefix) still works",
      bc.hhmm("09:45:12") == "09:45")

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
