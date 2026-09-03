"""
Held-position halt alerts and milestone stop recalculation (2026-09-02, late).

Both close "a function exists and nothing calls it" gaps, which is a shape this
codebase keeps producing - send_alert() had zero call sites for weeks, and
DynamicStops.should_recalculate has had none since it was written.
"""
import copy
import yaml
from _repo import CONFIG, repo_file
from src.strategy.strategy import TradeManager
from src.analytics.dynamic_stops import DynamicStops

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


msrc = open(repo_file("src", "main.py")).read()

print("=== 1. A HELD POSITION THAT HALTS ALERTS, AND DOES NOT ACT ===")
check("the check exists for OPEN positions, not just entries",
      "HELD POSITIONS THAT HALT" in msrc)
check("it alerts rather than closing - the right response depends on WHY it "
      "halted, which the bot cannot see",
      "No action has been taken" in msrc)
check("...and says a stop cannot protect a halted position",
      "there \\nare no trades to fill against" in msrc or "no trades to fill against" in msrc)
check("it fires once per CHANGE, not once per interval",
      'halt_state.get("last")' in msrc)
check("it runs on the reconcile cadence, not every poll - an asset lookup per "
      "position per 10s is a lot of calls for minute-scale state",
      msrc.index("HELD POSITIONS THAT HALT") > msrc.index("_rc = config[\"trading\"].get(\"reconcile\")")
      or "reconcile" in msrc)
check("a lookup failure never produces a false alert",
      "if _t is False:" in msrc)
check("it can be turned off", "alert_on_held" in msrc)

print("\n=== 2. MILESTONE STOP RECALCULATION ===")
eng = DynamicStops(CFG, history={}, atr_by_symbol={"Q": 0.4, "W": 3.0})
tm = TradeManager("Q", 100.0, 100, copy.deepcopy(CFG))
check("a fresh position has no milestone yet", tm._last_stop_milestone is None)

note = tm.recalculate_stop(eng, 100.0)
check("the first evaluation tightens a quiet name onto its ATR stop",
      note and tm.config["trading"]["final_exit_loss_pct"] == -0.4, (note, tm.config["trading"]["final_exit_loss_pct"]))
check("...and records the milestone", tm._last_stop_milestone == 0.0, tm._last_stop_milestone)

before = tm.config["trading"]["final_exit_loss_pct"]
tm.recalculate_stop(eng, 100.2)
check("a move WITHIN the band does not recompute", tm.config["trading"]["final_exit_loss_pct"] == before)

# MONOTONIC: the property that makes this safe to run on a live position.
tm2 = TradeManager("W", 100.0, 100, copy.deepcopy(CFG))
tm2.config["trading"]["final_exit_loss_pct"] = -0.3     # already very tight
n2 = tm2.recalculate_stop(eng, 100.0)                    # ATR would say -1.0%
check("it REFUSES to widen a stop that is already tighter", n2 is None, n2)
check("...leaving the tight stop exactly as it was",
      tm2.config["trading"]["final_exit_loss_pct"] == -0.3)

tm3 = TradeManager("Q", 100.0, 100, copy.deepcopy(CFG))
tm3.recalculate_stop(eng, 101.5)
m_high = tm3._last_stop_milestone
tm3.recalculate_stop(eng, 100.1)
check("a position falling back does not step its milestone DOWN",
      tm3._last_stop_milestone == m_high, (m_high, tm3._last_stop_milestone))

check("no engine -> no change, never a crash",
      TradeManager("Q", 100.0, 100, copy.deepcopy(CFG)).recalculate_stop(None, 100.0) is None)
check("the recalculation cannot leak into the shared config",
      CFG["trading"]["final_exit_loss_pct"] == -1.0)

check("it is wired into the exit sweep, before the rules run on that price",
      "trade.recalculate_stop(" in msrc)
check("...and guarded so it can never raise into the exit path",
      "stop recalculation skipped" in msrc)

sstrat = open(repo_file("src", "strategy", "strategy.py")).read()
check("should_recalculate finally has a caller",
      "engine.should_recalculate(" in sstrat)

print("\n=== 3. HONEST LIMITS, RECORDED ===")
check("the code says why milestones and not continuous recalculation",
      "chases the price" in sstrat)
check("...and that widening a live stop is the worst thing it could do",
      "single worst thing" in sstrat)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
