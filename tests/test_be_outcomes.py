"""
ops/be-outcomes.py: the BE-outcome distribution tool queued in
PENDING_WORK.md item 7 - for trades whose mfe_pct cleared a trigger, what
share went on to +0.75%, +1.0%, closed at -0.3% or worse, or scratched via
the BREAKEVEN_STOP exit. Built on a fixture the test controls, same reasoning
as test_counterfactual.py: trust the numbers, not the log by eye.
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


def row(date, sym, mfe, pl_pct, reason, pl=None):
    r = {k: "" for k in FIELDS}
    r.update(date=date, symbol=sym, entry_price=100.0, entry_method="RAPID_INCREASE_IMMEDIATE",
              mfe_pct=mfe, pl_pct=pl_pct, exit_reason=reason, qty=100,
              pl=pl if pl is not None else "")
    return [r[k] for k in FIELDS]


def write_fixture(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for r in rows:
            w.writerow(r)


def run(path, *args):
    return subprocess.run(
        [sys.executable, repo_file("ops", "be-outcomes.py"), "--trades", path, *args],
        capture_output=True, text=True, cwd=REPO,
    )


fixture = "/tmp/claude_test_trade_history.csv"

rows = [
    row("2026-08-30", "BIG1", 1.2, 1.1, "TAKE_PROFIT_1.5%", pl=110),
    row("2026-08-30", "MED1", 0.85, 0.5, "TRAILING_STOP", pl=50),
    row("2026-08-30", "SCRATCH1", 0.20, 0.05, "BREAKEVEN_STOP", pl=5),
    row("2026-08-30", "LOSER1", 0.18, -0.45, "FINAL_EXIT_-0.35%", pl=-45),
    row("2026-08-30", "NOTOUCH1", 0.05, -0.20, "FIRST_EXIT_-0.5%", pl=-20),
    row("2026-08-30", "NOMFE1", "", -0.10, "FIRST_EXIT_-0.5%", pl=-10),
    row("2026-08-31", "LATE1", 1.5, 1.4, "TAKE_PROFIT_1.5%", pl=140),
]
write_fixture(fixture, rows)

print("=== 1. DEFAULT TRIGGER (0.15) ===")
r = run(fixture)
check("ran cleanly", r.returncode == 0, r.stderr)
out = r.stdout
check("5 of 7 trades touched the 0.15 trigger (NOTOUCH1 and NOMFE1 don't)",
      "5 of 7 trades cleared" in out, out)
check("3 reached >=0.75%: BIG1, MED1, LATE1",
      "ran to +0.75% or more (mfe_pct):  3/5" in out, out)
check("2 reached >=1.00%: BIG1, LATE1",
      "ran to +1.00% or more (mfe_pct):  2/5" in out, out)
check("1 closed <=-0.3% despite touching trigger: LOSER1",
      "still closed at -0.3% or worse:   1/5" in out, out)
check("1 scratched via BREAKEVEN_STOP: SCRATCH1",
      "exited via BREAKEVEN_STOP:        1/5" in out, out)
check("BREAKEVEN_STOP mean is read back and judged against the +0.05% floor",
      "BREAKEVEN_STOP mean pl_pct is +0.050%" in out and "doing its documented job" in out, out)

print("\n=== 2. --trigger RAISES THE BAR (mirrors the normal session's 0.5% tier) ===")
r = run(fixture, "--trigger", "0.75")
check("only BIG1, MED1, LATE1 clear a 0.75% trigger",
      "3 of 7 trades cleared" in r.stdout, r.stdout)

print("\n=== 3. --since FILTERS BY DATE ===")
r = run(fixture, "--since", "2026-08-31")
check("only LATE1's date remains", "1 of 1 trades cleared" in r.stdout, r.stdout)

print("\n=== 4. --symbols FILTERS TO NAMED SYMBOLS ===")
r = run(fixture, "--symbols", "big1,loser1")
check("case-insensitive symbol filter keeps just the two named",
      "2 of 2 trades cleared" in r.stdout, r.stdout)

print("\n=== 5. NOTHING CLEARS THE TRIGGER -> CLEAN MESSAGE, NOT A CRASH ===")
r = run(fixture, "--trigger", "5.0")
check("exits 0 even with an empty result", r.returncode == 0, r.stderr)
check("says so in plain language", "Nothing to report" in r.stdout, r.stdout)

print("\n=== 6. MISSING FILE -> CLEAN ERROR, NOT A TRACEBACK ===")
r = run("/tmp/claude_test_trade_history_does_not_exist.csv")
check("non-zero exit on a missing file", r.returncode != 0)
check("no traceback", "Traceback" not in r.stderr, r.stderr)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
