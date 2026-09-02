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


def vwap_acc(spy=None, qqq=None):
    """{symbol: [price*vol, vol]} - _vwap divides the two, so a VWAP of 100
    is just [100.0, 1.0]."""
    acc = {}
    if spy is not None:
        acc["SPY"] = [float(spy), 1.0]
    if qqq is not None:
        acc["QQQ"] = [float(qqq), 1.0]
    return acc


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

print("\n=== 5b. PRIMARY RULE: SPY AND QQQ vs THEIR OWN VWAP ===")
# VWAP 100 for both; price above/below decides the band.
VW = vwap_acc(spy=100.0, qqq=100.0)


def regime(spy_px, qqq_px, cfg=RC, bstate=None):
    return M._regime_multiplier(
        cfg, {}, bstate if bstate is not None else breadth(0.0, 100.0),
        [(0, spy_px)], dt("09:45"), ET,
        vwap_acc=VW, qqq_history=[(0, qqq_px)],
    )


m, label = regime(100.5, 100.5)
check("both indices above VWAP -> bullish, full size", m == 1.0 and label == "bullish", (m, label))
m, label = regime(100.5, 99.5)
check("SPY above, QQQ below -> neutral, half size", m == 0.5 and label == "neutral", (m, label))
m, label = regime(99.5, 100.5)
check("QQQ above, SPY below -> also neutral (disagreement is the case)",
      m == 0.5 and label == "neutral", (m, label))
m, label = regime(99.5, 99.5)
check("both below VWAP -> bearish", m == 0.15 and label == "bearish", (m, label))

stand_down = copy.deepcopy(RC)
stand_down["trading"]["regime_sizing"]["bearish_multiplier"] = 0.0
m, label = M._regime_multiplier(
    stand_down, {}, breadth(0.0, 100.0), [(0, 99.5)], dt("09:45"), ET,
    vwap_acc=VW, qqq_history=[(0, 99.5)],
)
check("bearish_multiplier 0.0 -> NO NEW LONGS, the shipped default",
      m == 0.0 and label == "bearish", (m, label))

print("\n=== 5c. VWAP BEATS THE FALLBACK, AND THE FALLBACK STILL WORKS ===")
# Breadth says bullish, but both indices are under VWAP -> VWAP wins.
m, label = regime(99.5, 99.5, bstate=breadth(1.5, 100.0))
check("VWAP is the primary rule - strong breadth does not override it",
      label == "bearish", (m, label))
# No VWAP at all -> falls back to breadth + SPY-since-open, not to 1.0x.
m, label = M._regime_multiplier(
    RC, {}, breadth(-0.5, 100.0), [(0, 100.6)], dt("09:45"), ET,
    vwap_acc={}, qqq_history=[],
)
check("no VWAP -> falls back to the breadth rule rather than 'no opinion'",
      m == 0.15 and label == "bearish", (m, label))
# Only ONE index has a VWAP - not enough for a two-index agreement test.
m, label = M._regime_multiplier(
    RC, {}, breadth(-0.5, 100.0), [(0, 100.6)], dt("09:45"), ET,
    vwap_acc=vwap_acc(spy=100.0), qqq_history=[(0, 99.0)],
)
check("one index missing its VWAP -> fallback, not a half-measured verdict",
      label == "bearish", (m, label))

print("\n=== 5d. QQQ IS ACTUALLY STREAMED ===")
_msrc = open(repo_file("src", "main.py")).read()
check("QQQ is a benchmark symbol, so its VWAP is not built on stale REST bars",
      'out = ["SPY", "QQQ"]' in _msrc)
check("both indices' VWAP is accumulated each poll",
      '_update_vwap(vwap_acc, _bench, _bar)' in _msrc)
check("...for SPY and QQQ specifically",
      'for _bench, _hist in (("SPY", spy_history), ("QQQ", qqq_history)):' in _msrc)
# Moved out of the entry-window branch on 2026-09-02. Sampling only from
# 09:33 left the opening burst (09:30-09:33) with no market context to
# record, and gave the 09:45 regime check twelve minutes of VWAP instead of
# fifteen.
check("benchmarks are sampled from the MARKET OPEN, not from entry_window_start",
      _msrc.index("BENCHMARK SAMPLING - from the market OPEN")
      < _msrc.index("halted = breadth_would_halt"))
check("a 0x regime is reported as a stand-down, not a sizing rounding error",
      "regime_sizing standing down" in _msrc)

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
check("bearish ships as NO NEW LONGS (0.0), the stronger claim to test first",
      rc_live.get("bearish_multiplier") == 0.0, rc_live.get("bearish_multiplier"))
check("the three bands are ordered bullish > neutral > bearish",
      rc_live["bullish_multiplier"] > rc_live["neutral_multiplier"] >= rc_live["bearish_multiplier"])

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
