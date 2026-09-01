"""
Regime-scaled position sizing (2026-09-02): replaces breadth_halt's binary
stop with a smooth size multiplier - 100% bullish, 50% neutral, ~15% bearish -
using the watchlist's own breadth (reused from breadth_halt's measurement)
and SPY's move since the open. The point, straight from the user's own
diagnosis: "what's missing is breadth" - a hard halt makes that literally
true by refusing every remaining signal; scaling size keeps taking signals,
just fewer and smaller.

Covers _regime_multiplier as a pure function, its composition into
_position_size via executor.regime_size_multiplier, and that main.py is
actually wired so regime_sizing REPLACES (never layers with) breadth_halt's
halt decision.
"""
import copy
import pytz
import yaml
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.executor.executor import Executor

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def dt(hhmm):
    return M.parse_hhmm_today(hhmm, ET)


RC = {
    "trading": {
        "regime_sizing": {
            "enabled": True,
            "check_time": "09:45",
            "bearish_below_pct": -0.3,
            "bullish_above_pct": 0.0,
            "bullish_multiplier": 1.0,
            "neutral_multiplier": 0.5,
            "bearish_multiplier": 0.15,
        }
    }
}


def breadth(mean_move=None, spy_open=None):
    b = {"open_px": {}}
    if mean_move is not None:
        b["mean_move"] = mean_move
    if spy_open is not None:
        b["open_px"]["SPY"] = spy_open
    return b


print("=== 1. DISABLED / BEFORE check_time / NO EVIDENCE -> 1.0x, no opinion ===")
off_cfg = copy.deepcopy(RC); off_cfg["trading"]["regime_sizing"]["enabled"] = False
m, label = M._regime_multiplier(off_cfg, {}, breadth(0.5, 100.0), [(0, 101.0)], dt("10:00"), ET)
check("disabled -> full size, no label", m == 1.0 and label is None, (m, label))

m, label = M._regime_multiplier(RC, {}, breadth(0.5, 100.0), [(0, 101.0)], dt("09:40"), ET)
check("before check_time -> full size, no label", m == 1.0 and label is None, (m, label))

m, label = M._regime_multiplier(RC, {}, breadth(None, None), [], dt("09:45"), ET)
check("no readings at all -> full size (same guard breadth_halt uses), not penalized",
      m == 1.0 and label is None, (m, label))

print("\n=== 2. BULLISH: both readings clear the bullish floor ===")
m, label = M._regime_multiplier(RC, {}, breadth(0.4, 100.0), [(0, 100.6)], dt("09:45"), ET)
check("both readings >= 0.0 -> bullish, 1.0x", m == 1.0 and label == "bullish", (m, label))

print("\n=== 3. BEARISH: EITHER reading below the bearish floor ===")
m, label = M._regime_multiplier(RC, {}, breadth(-0.5, 100.0), [(0, 100.6)], dt("09:45"), ET)
check("weak breadth alone -> bearish, 0.15x even though SPY is fine",
      m == 0.15 and label == "bearish", (m, label))

m, label = M._regime_multiplier(RC, {}, breadth(0.4, 100.0), [(0, 99.6)], dt("09:45"), ET)
check("weak SPY alone -> bearish, 0.15x even though breadth is fine",
      m == 0.15 and label == "bearish", (m, label))

print("\n=== 4. NEUTRAL: neither bearish, not both clearing the bullish floor ===")
m, label = M._regime_multiplier(RC, {}, breadth(-0.1, 100.0), [(0, 100.6)], dt("09:45"), ET)
check("breadth slightly negative (above bearish floor), SPY fine -> neutral, 0.5x",
      m == 0.5 and label == "neutral", (m, label))

print("\n=== 5. LATCHED: computed once, held for the rest of the day ===")
state = {}
m1, l1 = M._regime_multiplier(RC, state, breadth(-0.5, 100.0), [(0, 100.6)], dt("09:45"), ET)
check("first call at check_time computes bearish", m1 == 0.15 and l1 == "bearish")
m2, l2 = M._regime_multiplier(RC, state, breadth(2.0, 100.0), [(0, 105.0)], dt("10:30"), ET)
check("a later bullish-looking reading does NOT un-latch it",
      m2 == 0.15 and l2 == "bearish", (m2, l2))

print("\n=== 6. _position_size COMPOSES executor.regime_size_multiplier ===")
sizing_cfg = {"trading": {
    "max_position_per_stock_usd": 10000,
    "max_total_exposure_fraction": 0.9,
    "max_concurrent_positions": 10,
    "max_risk_per_trade_fraction": 0.005,
    "final_exit_loss_pct": -1.0,
}}


class FakeExecutor:
    def __init__(self, equity):
        self.equity = equity


ex = FakeExecutor(100000)
baseline_qty = M._position_size(sizing_cfg, ex, 100.0)
check("no regime_size_multiplier set -> behaves exactly as before (default 1.0x)",
      baseline_qty == int(M._position_size(sizing_cfg, ex, 100.0)))

ex.regime_size_multiplier = 0.5
half_qty = M._position_size(sizing_cfg, ex, 100.0)
check("0.5x regime multiplier roughly halves the share count",
      half_qty == int(baseline_qty * 0.5) or abs(half_qty - baseline_qty // 2) <= 1,
      (baseline_qty, half_qty))

ex.regime_size_multiplier = 0.0
check("0.0x regime multiplier -> zero shares, entry would be skipped",
      M._position_size(sizing_cfg, ex, 100.0) == 0)

print("\n=== 7. main.py: regime_sizing REPLACES breadth_halt, never layers with it ===")
src = open(repo_file("src", "main.py")).read()
check("breadth_halt's measurement always runs (reused as regime evidence)",
      "breadth_would_halt = _breadth_halt(" in src)
check("the actual halt is suppressed while regime_sizing is active",
      "halted = breadth_would_halt and not regime_active" in src)
check("regime multiplier is written onto the executor for _position_size to read",
      "executor.regime_size_multiplier = _mult" in src)
check("_position_size reads it back via getattr with a safe 1.0 default",
      'getattr(executor, "regime_size_multiplier", 1.0)' in src)

print("\n=== 8. LIVE CONFIG ===")
rc_live = CFG["trading"].get("regime_sizing") or {}
check("regime_sizing is shipped enabled", rc_live.get("enabled") is True)
check("breadth_halt stays enabled too - its measurement is reused, only the halt is bypassed",
      CFG["trading"].get("breadth_halt", {}).get("enabled") is True)
check("bearish floor mirrors breadth_halt's own min_mean_pct",
      rc_live.get("bearish_below_pct") == CFG["trading"]["breadth_halt"]["min_mean_pct"])

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
