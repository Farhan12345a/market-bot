"""
Soft loss-velocity warning (PENDING_WORK.md item 8, elevated to HIGH
PRIORITY on request 2026-09-02, shipped the same day).

max_daily_loss_usd was doing double duty: circuit breaker AND the only loss
number that exists, so a day was either fine or over with no signal on the
way there. At $500 that gap is sharper, not softer - 2026-08-31 closed at
-$546.24, which under today's setting flattens the book mid-session with
nothing said beforehand.

This is WARNING-ONLY plumbing, so the tests that matter most are the ones
asserting it never changes a trading decision.
"""
import copy
import types
from datetime import datetime, timedelta
from _repo import REPO, CONFIG, repo_file
from src.executor.executor import Executor

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def cfg(enabled=True, fractions=(0.4, 0.6, 0.8), max_loss=500):
    return {"trading": {
        "max_daily_loss_usd": max_loss,
        "loss_velocity_warning": {"enabled": enabled, "warn_fractions": list(fractions)},
        "max_concurrent_positions": 10,
    }}


class Broker:
    def get_positions(self): return {}
    def get_account(self): return types.SimpleNamespace(cash="90000", equity="90000", buying_power="90000")


def mk(pnl, config=None):
    e = Executor(Broker(), config or cfg())
    e.daily_pnl = pnl
    return e


T0 = datetime(2026, 9, 2, 9, 45, 0)

print("=== 1. NOTHING FIRES WHILE THE DAY IS SHALLOW OR GREEN ===")
check("a green day says nothing", mk(+300).check_loss_velocity(T0) is None)
check("flat says nothing", mk(0).check_loss_velocity(T0) is None)
check("-$150 is under the 40% ($200) first threshold",
      mk(-150).check_loss_velocity(T0) is None)

print("\n=== 2. EACH THRESHOLD FIRES, ONCE ===")
e = mk(-200)                                  # exactly 40% of $500
note = e.check_loss_velocity(T0)
check("40% fires", note is not None and "40%" in note, note)
check("it says plainly that it is a warning only",
      note and "WARNING ONLY" in note, note)
check("the same depth does NOT fire again on the next poll",
      e.check_loss_velocity(T0 + timedelta(seconds=10)) is None)
e.daily_pnl = -310
check("crossing the next threshold DOES fire", e.check_loss_velocity(T0 + timedelta(minutes=5)) is not None)
e.daily_pnl = -320
check("...and then goes quiet again at the same level",
      e.check_loss_velocity(T0 + timedelta(minutes=6)) is None)

print("\n=== 3. A JUMP PAST SEVERAL THRESHOLDS REPORTS THE DEEPEST ===")
e2 = mk(-450)                                 # 90%: clears 0.4, 0.6 and 0.8 at once
note2 = e2.check_loss_velocity(T0)
check("one line, not three", note2 is not None and "90%" in note2, note2)
check("and all three are marked fired, so nothing re-reports later",
      e2.check_loss_velocity(T0 + timedelta(minutes=1)) is None)

print("\n=== 4. VELOCITY IS REPORTED, NOT JUST DEPTH ===")
# -$300 by 10:00 and -$300 by 15:30 are the same depth, very different days.
e3 = mk(-300)
note3 = e3.check_loss_velocity(T0 + timedelta(minutes=10))
check("reports a $/min rate", note3 and "/min" in note3, note3)
check("projects when the hard stop would arrive at that rate",
      note3 and "stop is ~" in note3, note3)

print("\n=== 5. IT NEVER HALTS ANYTHING ===")
e4 = mk(-499)                                  # one dollar from the ceiling
e4.check_loss_velocity(T0)
check("the hard limit is still the only thing that stops the day",
      e4.check_daily_loss_limit() is False)
e4.daily_pnl = -501
check("...and it does stop when actually breached", e4.check_daily_loss_limit() is True)

print("\n=== 6. DISABLED / MISCONFIGURED -> SILENT, NOT BROKEN ===")
check("disabled says nothing", mk(-400, cfg(enabled=False)).check_loss_velocity(T0) is None)
check("no max_daily_loss_usd -> nothing to be a fraction OF, stays silent",
      mk(-400, cfg(max_loss=0)).check_loss_velocity(T0) is None)

print("\n=== 7. IT RESETS ACROSS DAYS (long-running process) ===")
e5 = mk(-250)
check("fires on day one", e5.check_loss_velocity(T0) is not None)
check("silent later the same day", e5.check_loss_velocity(T0 + timedelta(hours=2)) is None)
check("fires again the NEXT day at the same depth",
      e5.check_loss_velocity(T0 + timedelta(days=1)) is not None)

print("\n=== 8. NOTES ARE KEPT FOR THE DAILY REPORT ===")
e6 = mk(-260)
e6.check_loss_velocity(T0)
check("the note is recorded on the executor", len(e6.loss_velocity_notes) == 1, e6.loss_velocity_notes)

print("\n=== 9. WIRED INTO THE POLL LOOP, BEFORE THE HARD LIMIT ===")
msrc = open(repo_file("src", "main.py")).read()
check("main.py calls it", "executor.check_loss_velocity()" in msrc)
check("...before check_daily_loss_limit, so a day that crosses both still reports",
      msrc.index("executor.check_loss_velocity()") < msrc.index("if executor.check_daily_loss_limit():"))
check("...and a failure in it can never stop the poll",
      "loss-velocity check skipped" in msrc)

print("\n=== 10. LIVE CONFIG ===")
import yaml
LIVE = yaml.safe_load(open(CONFIG))["trading"]
lv = LIVE.get("loss_velocity_warning") or {}
check("shipped enabled", lv.get("enabled") is True)
check("fractions are ascending and all below 1.0 (a warning must precede the stop)",
      lv.get("warn_fractions") == sorted(lv.get("warn_fractions") or [])
      and all(0 < f < 1 for f in (lv.get("warn_fractions") or [])), lv.get("warn_fractions"))
check("the hard stop it measures against still exists", LIVE.get("max_daily_loss_usd") == 500)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
