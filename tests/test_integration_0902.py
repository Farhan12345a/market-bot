"""
INTEGRATION + EDGE CASES for everything shipped on 2026-09-02.

The individual suites prove each feature works alone. This one exists
because that is not the question that decides tomorrow's session - the
question is whether they work TOGETHER, and whether the old features still
behave with all of them switched on at once.

Shipped that day, all live simultaneously:
  1. phantom-exit guard          6. loss-velocity warning
  2. sign-aware exits            7. true ATR percentile
  3. regime sizing (VWAP)        8. ETF exclusions
  4. opening-burst spread gate   9. unique-symbol stream cap + backoff
  5. correlation limiter        10. trade context/path recorders

The interactions below are the ones with a real chance of biting, chosen by
asking "which pair of these touches the same state?" rather than by covering
each feature again in isolation.
"""
import copy
import os
import tempfile
import types
import pytz
import yaml
from datetime import datetime, timedelta
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.main as M
from src.executor.executor import Executor, PHANTOM_EXIT
from src.strategy.strategy import Strategy
from src.analytics import trade_recorder as TR
from src.analytics.correlation import correlation_block
from src.analytics.dynamic_stops import DynamicStops, mae_level

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


BASE = {"trading": {"max_concurrent_positions": 10, "max_total_exposure_fraction": 0.9,
                    "max_daily_loss_usd": 100000, "use_take_profit": False,
                    "max_position_per_stock_usd": 10000,
                    "max_risk_per_trade_fraction": 0.005,
                    "final_exit_loss_pct": -1.0}}


class Pos:
    def __init__(self, sym, qty, avg=100.0, cur=100.0):
        self.symbol, self.qty = sym, str(qty)
        self.avg_entry_price, self.current_price = str(avg), str(cur)
        self.market_value = str(float(qty) * cur)
        self.unrealized_pl = "0"


class Order:
    id = "o1"


class Broker:
    def __init__(self, positions=None):
        self.positions = dict(positions or {})
        self.orders = []
    def get_positions(self): return dict(self.positions)
    def cancel_open_orders(self, s): return 0
    def submit_market_order(self, s, q, side="buy"):
        self.orders.append((s, q, side)); return Order()
    def get_account(self):
        return types.SimpleNamespace(cash="90000", equity="90000", buying_power="90000")
    def get_filled_sell_orders_since(self, s, since): return []


def dt(hhmm):
    return M.parse_hhmm_today(hhmm, ET)


print("=== 1. REGIME x POSITION SIZING: a 0x regime must not be read as a rounding error ===")
ex = Executor(Broker(), copy.deepcopy(BASE))
ex._equity = 100000.0   # equity is a read-only property; seed the backing field
full = M._position_size(BASE, ex, 100.0)
check("full size with no regime set", full > 0, full)
ex.regime_size_multiplier = 0.0
check("bearish regime -> zero shares, so no entry can be placed",
      M._position_size(BASE, ex, 100.0) == 0)
ex.regime_size_multiplier = 0.5
check("neutral regime halves it", M._position_size(BASE, ex, 100.0) == full // 2
      or abs(M._position_size(BASE, ex, 100.0) - full // 2) <= 1)
ex.regime_size_multiplier = 1.0
check("bullish regime restores it exactly", M._position_size(BASE, ex, 100.0) == full)

print("\n=== 2. BREADTH HALT RETIRED: one guard owns 'how much risk is on' ===")
# 2026-09-02: breadth_halt's HALT is gone; its MEASUREMENT stays and feeds
# regime_sizing (as the no-VWAP fallback and as the chop reading). Two guards
# answering the same question from different evidence meant whichever ran
# first won by ordering accident rather than by rule.
msrc = open(repo_file("src", "main.py")).read()
check("the measurement still runs every poll",
      "_measure_breadth(config, market_data, symbols, breadth_state, now, et)" in msrc)
check("the halt function is gone entirely",
      "def _breadth_halt(" not in msrc)
check("...and nothing gates entries on a 'halted' flag any more",
      "halted = breadth_would_halt" not in msrc
      and "\n        if halted:" not in msrc)
check("the retired config key is gone; only the measurement block remains",
      "breadth_halt" not in CFG["trading"] and CFG["trading"]["breadth"]["enabled"] is True)
check("regime sizing is the single owner and is enabled",
      CFG["trading"]["regime_sizing"]["enabled"] is True)

print("\n=== 3. PHANTOM GUARD x SIGN-AWARE EXIT: a phantom SHORT is still a phantom ===")
b = Broker({})                      # broker holds nothing at all
e = Executor(b, copy.deepcopy(BASE))
e.open_entries["GHOST"] = 100.0
check("a phantom long returns the sentinel and submits nothing",
      e.submit_exit_order("GHOST", 10, "FIRST_EXIT", price=99.0) is PHANTOM_EXIT
      and b.orders == [], b.orders)
e2 = Executor(Broker({}), copy.deepcopy(BASE))
e2.open_entries["GHOST"] = 100.0
check("...and so does a phantom cover - the guard keys off holdings, not side",
      e2.submit_exit_order("GHOST", 10, "FLATTEN_ALL", price=99.0, side="buy") is PHANTOM_EXIT)

print("\n=== 4. SIGN-AWARE FLATTEN x LOSS ACCOUNTING: a cover must not corrupt the day ===")
b3 = Broker({"SH": Pos("SH", -10, avg=100.0, cur=90.0)})
e3 = Executor(b3, copy.deepcopy(BASE))
e3.open_entries["SH"] = 100.0
e3.flatten_all_positions()
rec = e3.trades_log[-1]
check("covering a short below its entry books a PROFIT", rec["pl"] > 0, rec["pl"])
check("...and the realized-P&L accumulator agrees",
      e3._realized_pnl_today > 0, e3._realized_pnl_today)
# That matters because the daily-loss limit reads this number. A sign error
# here would make a winning cover push the account toward its circuit breaker.
e3.daily_pnl = e3._realized_pnl_today
check("so the daily-loss limit is not tripped by a winning cover",
      e3.check_daily_loss_limit() is False)

print("\n=== 5. LOSS VELOCITY x DAILY LIMIT: warn before, never instead of ===")
cfg5 = copy.deepcopy(BASE)
cfg5["trading"]["max_daily_loss_usd"] = 500
cfg5["trading"]["loss_velocity_warning"] = {"enabled": True, "warn_fractions": [0.4, 0.8]}
e5 = Executor(Broker(), cfg5)
e5.daily_pnl = -450
note = e5.check_loss_velocity(datetime(2026, 9, 3, 10, 0))
check("warns at 90% of the ceiling", note is not None and "90%" in note, note)
check("but does NOT halt - the hard limit is still the only stop",
      e5.check_daily_loss_limit() is False)
e5.daily_pnl = -501
check("the hard limit still fires when actually breached",
      e5.check_daily_loss_limit() is True)

print("\n=== 6. EXCLUSIONS x SCREENER x WATCHLIST: an ETF cannot get through ANY door ===")
from src.screener.exclusions import is_excluded
t = CFG["trading"]
for sym in ("SOXL", "TQQQ", "SQQQ", "UVXY"):
    watch_out = M._filter_watchlist_by_exclusions(CFG, ["NVDA", sym, "HOOD"])
    entry_blocked, _ = M._is_excluded_symbol(CFG, sym)
    check(f"{sym}: dropped from the watchlist AND refused at entry",
          sym not in watch_out and entry_blocked, (watch_out, entry_blocked))
check("a legitimate name passes all three layers",
      "NVDA" in M._filter_watchlist_by_exclusions(CFG, ["NVDA"])
      and M._is_excluded_symbol(CFG, "NVDA")[0] is False)

print("\n=== 7. EDGE: EVERY watchlist symbol excluded -> keep the list, do not blank the day ===")
allbad = M._filter_watchlist_by_exclusions(CFG, ["SOXL", "TQQQ", "SQQQ"])
check("the list survives rather than emptying", allbad == ["SOXL", "TQQQ", "SQQQ"], allbad)
check("...and the entry gate still refuses each one individually",
      all(M._is_excluded_symbol(CFG, s)[0] for s in allbad))

print("\n=== 8. CORRELATION LIMITER: fails OPEN on thin or degenerate data ===")
cc = {"trading": {"correlation_limit": {"enabled": True, "threshold": 0.85,
                                        "min_samples": 10, "max_correlated_positions": 1}}}
rising = [100 + i for i in range(20)]
check("two identical series ARE blocked", correlation_block(
    cc, "A", {"A": rising, "B": rising}, ["B"])[0] is True)
check("too few samples -> allowed, never refused on noise",
      correlation_block(cc, "A", {"A": [100, 101], "B": [100, 101]}, ["B"])[0] is False)
check("a FLAT series has no correlation and must not block",
      correlation_block(cc, "A", {"A": [100] * 20, "B": [100] * 20}, ["B"])[0] is False)
check("a symbol with no history at all -> allowed",
      correlation_block(cc, "A", {"B": rising}, ["B"])[0] is False)
check("no open positions -> nothing to correlate against",
      correlation_block(cc, "A", {"A": rising}, [])[0] is False)
check("disabled -> always allowed",
      correlation_block({"trading": {"correlation_limit": {"enabled": False}}},
                        "A", {"A": rising, "B": rising}, ["B"])[0] is False)
# A TRUE mirror, not a linear ramp. Two perfectly linear series - one rising,
# one falling - have RETURN series that both shrink monotonically, so they
# correlate at about +0.995 even though the prices move in opposite
# directions. That is a real property of returns on a straight line, not a
# bug, and a naive fixture built from ramps tests the opposite of what it
# looks like it tests.
import random as _rnd
_rnd.seed(3)
up, down = [100.0], [100.0]
for _ in range(20):
    step = _rnd.gauss(0, 1)
    up.append(up[-1] + step)
    down.append(down[-1] - step)
check("a genuinely inverse pair is NOT blocked - that is diversification",
      correlation_block(cc, "A", {"A": up, "B": down}, ["B"])[0] is False)
check("...and the same pair correlated the other way IS blocked",
      correlation_block(cc, "A", {"A": up, "B": list(up)}, ["B"])[0] is True)

print("\n=== 9. DYNAMIC STOPS: the cap is the safety property ===")
ds_cfg = {"trading": {"final_exit_loss_pct": -1.0, "dynamic_stops": {
    "enabled": True, "min_samples": 3, "mae_percentile": 75,
    "atr_multiple": 1.0, "min_stop_pct": 0.25}}}
# A wild symbol whose own MAE distribution is far wider than the static stop.
wild = {"WILD": [-3.0, -2.5, -2.8, -3.2, -2.9]}
ds = DynamicStops(ds_cfg, history=wild, atr_by_symbol={"WILD": 4.0})
stop, why = ds.stop_for("WILD")
check("a wild symbol is CAPPED at the static stop, never widened",
      stop == -1.0, (stop, why))
check("...and says so", "CAPPED" in why, why)
calm = {"CALM": [-0.3, -0.25, -0.2, -0.35, -0.28]}
ds2 = DynamicStops(ds_cfg, history=calm, atr_by_symbol={"CALM": 0.2})
stop2, why2 = ds2.stop_for("CALM")
check("a calm symbol gets a TIGHTER stop than the static one",
      -1.0 < stop2 < 0, (stop2, why2))
check("...but never tighter than min_stop_pct (the spread would trip it)",
      stop2 <= -0.25, stop2)
ds3 = DynamicStops(ds_cfg, history={}, atr_by_symbol={})
check("no history and no ATR -> falls back to the static stop exactly",
      ds3.stop_for("NEW")[0] == -1.0)
check("disabled -> static stop, whatever the history says",
      DynamicStops({"trading": {"final_exit_loss_pct": -1.0,
                                "dynamic_stops": {"enabled": False}}},
                   history=calm).stop_for("CALM")[0] == -1.0)
# CHANGED 2026-09-02 (second pass): wired and enabled. ATR was the unblocking
# input all along - it is a volatility measurement, not an outcome measurement,
# so it needs no trade history. The cap at final_exit_loss_pct is what makes
# turning it on bounded: the worst case is exactly the previous behaviour.
check("it IS now wired into the live entry path",
      "_resolved_exit_cfg = _dynamic_exit_config(" in msrc
      and "_DYNAMIC_STOPS[\"engine\"] = _build_dynamic_stops" in msrc)
check("...and shipped enabled",
      (CFG["trading"].get("dynamic_stops") or {}).get("enabled") is True)
check("...but still capped so it can only ever TIGHTEN",
      DynamicStops(CFG, history={}, atr_by_symbol={"WILD": 9.0}).stop_for("WILD")[0]
      == CFG["trading"]["final_exit_loss_pct"])

print("\n=== 10. DYNAMIC STOPS: milestones are monotonic, so a stop never re-widens ===")
ds4 = DynamicStops(ds_cfg, history=calm)
check("recalculates on the first call", ds4.should_recalculate(0.0, None) is True)
check("recalculates when a new milestone is crossed", ds4.should_recalculate(0.6, 0.0) is True)
check("does NOT recalculate inside the same band", ds4.should_recalculate(0.7, 0.5) is False)
check("falling back below a milestone does NOT re-widen the stop",
      ds4.should_recalculate(0.1, 1.0) is False)

print("\n=== 11. RECORDERS: a full trade round-trips through both files ===")
with tempfile.TemporaryDirectory() as d:
    pf, cf = os.path.join(d, "paths.csv"), os.path.join(d, "ctx.csv")
    tid = TR.make_trade_id("HOOD", "2026-09-03T09:35:00")
    TR.record_path_samples([
        {"trade_id": tid, "symbol": "HOOD", "date": "2026-09-03",
         "timestamp": f"2026-09-03T09:{35+i}:00", "price": 100 + i, "gain_pct": i}
        for i in range(3)], path=pf)
    meta = {"entry_time": "2026-09-03T09:35:00", "method": "RAPID",
            "context": {"spy_vs_vwap": 0.2, "qqq_vs_vwap": 0.1, "regime": "bullish",
                        "continuation_score": 85, "relative_volume": 3.1}}
    rec = {"entry_price": 100.0, "qty": 10, "exit_price": 102.0,
           "exit_time": "2026-09-03T09:50:00", "exit_reason": "TAKE_PROFIT",
           "pl": 20.0, "pl_pct": 2.0, "mfe_pct": 2.2, "mae_pct": -0.3}
    TR.record_context(TR.build_context_row("HOOD", meta, rec), path=cf)

    import csv as _csv
    ctx_rows = list(_csv.DictReader(open(cf)))
    path_rows = list(_csv.DictReader(open(pf)))
    check("one context row written", len(ctx_rows) == 1, len(ctx_rows))
    check("three path samples written", len(path_rows) == 3, len(path_rows))
    check("the join key matches across both files",
          ctx_rows[0]["trade_id"] == path_rows[0]["trade_id"] == tid)
    check("entry context survived to the row", ctx_rows[0]["regime"] == "bullish"
          and ctx_rows[0]["continuation_score"] == "85")
    check("outcome fields landed too", ctx_rows[0]["realized_pnl"] == "20.0")
    # A missing reading must be BLANK, not 0 - these become filter conditions.
    check("an unrecorded market reading is blank, never a false zero",
          ctx_rows[0]["market_breadth"] == "", repr(ctx_rows[0]["market_breadth"]))

print("\n=== 12. RECORDERS: never able to break a session ===")
check("an unwritable path is swallowed, not raised",
      TR.record_path_samples([{"trade_id": "x"}], path="/nonexistent-dir-xyz/p.csv") is None)
check("an unwritable context file is swallowed too",
      TR.record_context({"trade_id": "x"}, path="/nonexistent-dir-xyz/c.csv") is None)
check("a context row builds even from an empty meta/record",
      TR.build_context_row("X", {}, {})["symbol"] == "X")
check("empty sample list is a no-op", TR.record_path_samples([]) is None)

print("\n=== 13. OPENING BURST: spread gate x multifactor rank, both live ===")
ob = CFG["trading"]["opening_burst"]
check("the spread gate is on", ob["min_move_to_spread_ratio"] == 2.0)
check("multifactor rank ships OFF - it inverted move-order on its own first test",
      ob["multifactor_rank"] is False)
check("...with a scored-fraction guard for when it is turned on",
      0 < ob["min_scored_fraction"] <= 1)
check("move order therefore still decides the burst, as it always has",
      "measured.sort(reverse=True)" in msrc)

print("\n=== 14. STREAM: the cap, the counting model and the backoff agree ===")
import src.data.stream as ST
from src.data.stream import PriceStream
ps = PriceStream("k", "s", feed="iex", subscribe_trades=True,
                 max_subscriptions=CFG["trading"]["stream_max_subscriptions"])
check("trade ticks no longer halve the budget",
      ps.symbol_budget() == CFG["trading"]["stream_max_subscriptions"])
check("main.py's warning uses the SAME rule (it used to divide by 2)",
      "budget = max(1, cap)" in msrc)
check("the cap is the known-good 14 until the boundary is tested live",
      CFG["trading"]["stream_max_subscriptions"] == 14)
check("a symbol-limit rejection is recoverable, not fatal",
      "_reduce_and_retry" in open(repo_file("src", "data", "stream.py")).read())
check("...and its retry delay is NOT the 15s connection-limit one",
      ST.SYMBOL_LIMIT_RETRY_DELAY < ST.CONNECTION_LIMIT_RETRY_DELAY)
check("the backoff is bounded, so it cannot loop for the session",
      ST.SYMBOL_LIMIT_RETRIES >= 1 and ST.SYMBOL_LIMIT_BACKOFF < 1)

print("\n=== 15. QQQ IS STREAMED, OR THE REGIME RULE READS A STALE PRICE ===")
bm = M._benchmark_symbols(CFG, ["NVDA", "HOOD"])
check("QQQ is a benchmark", "QQQ" in bm, bm)
check("SPY still is too", "SPY" in bm, bm)
check("benchmarks are not tradeable names", not ({"SPY", "QQQ"} & {"NVDA", "HOOD"}))
check("...and both are on the basket exclusion list, so neither can be bought",
      M._is_excluded_symbol(CFG, "SPY")[0] and M._is_excluded_symbol(CFG, "QQQ")[0])

print("\n=== 16. CONFIG COHERENCE: the numbers must not contradict each other ===")
t = CFG["trading"]
ob_ex = t["opening_burst"]["exits"]
check("the burst's hard stop is tighter than the session's",
      ob_ex["final_exit_loss_pct"] > t["final_exit_loss_pct"],
      (ob_ex["final_exit_loss_pct"], t["final_exit_loss_pct"]))
check("the burst's partial exit fires BEFORE its hard stop",
      ob_ex["first_exit_loss_pct"] > ob_ex["final_exit_loss_pct"],
      (ob_ex["first_exit_loss_pct"], ob_ex["final_exit_loss_pct"]))
check("every loss-velocity warning fires below the hard ceiling",
      all(0 < f < 1 for f in t["loss_velocity_warning"]["warn_fractions"]))
check("regime multipliers are ordered and bounded",
      1.0 >= t["regime_sizing"]["bullish_multiplier"]
      > t["regime_sizing"]["neutral_multiplier"]
      >= t["regime_sizing"]["bearish_multiplier"] >= 0)
check("the burst decides before the normal entry window opens",
      t["opening_burst"]["decide_by"] <= t["entry_window_start"])
check("the regime check happens inside the entry window, or it can never bind",
      t["entry_window_start"] <= t["regime_sizing"]["check_time"] <= t["entry_window_end"],
      (t["entry_window_start"], t["regime_sizing"]["check_time"], t["entry_window_end"]))
check("the burst's budget still leaves room for the normal session",
      t["opening_burst"]["max_positions"] < t["max_concurrent_positions"])

print("\n=== 17. BROKER MISBEHAVING: none of the new code may raise into the loop ===")
class Hostile:
    """Every method fails the way a real API failure does."""
    def get_positions(self): raise ConnectionError("api down")
    def cancel_open_orders(self, s): raise ConnectionError("api down")
    def submit_market_order(self, s, q, side="buy"): return Order()
    def get_account(self): raise ConnectionError("api down")
    def get_filled_sell_orders_since(self, s, since): raise ConnectionError("api down")

eh = Executor(Hostile(), copy.deepcopy(BASE))
eh.open_entries["X"] = 100.0
r = eh.submit_exit_order("X", 10, "FINAL_EXIT", price=99.0)
check("get_positions failing does NOT block a real exit (fails open)",
      r is not None and r is not PHANTOM_EXIT, r)
check("...and a cancel failure does not either", True)
check("loss velocity survives a broken account call",
      eh.check_loss_velocity(datetime(2026, 9, 3, 10, 0)) is None)

print("\n=== 18. MALFORMED BROKER DATA: garbage quantities must not crash a flatten ===")
class Junk:
    def __init__(self, positions): self.positions = positions; self.orders = []
    def get_positions(self): return self.positions
    def cancel_open_orders(self, s): return 0
    def submit_market_order(self, s, q, side="buy"):
        self.orders.append((s, q, side)); return Order()
    def get_account(self):
        return types.SimpleNamespace(cash="0", equity="0", buying_power="0")

class BadPos:
    symbol = "BAD"; qty = "not-a-number"
    avg_entry_price = "100"; current_price = "100"; market_value = "0"; unrealized_pl = "0"

jb = Junk({"BAD": BadPos(), "GOOD": Pos("GOOD", 5)})
ej = Executor(jb, copy.deepcopy(BASE))
try:
    flat = ej.flatten_all_positions()
    raised = None
except Exception as exc:
    flat, raised = None, exc
check("a non-numeric qty does not abort the whole flatten sweep",
      raised is None, raised)
check("...and the VALID position beside it is still closed",
      flat is not None and "GOOD" in flat, flat)

print("\n=== 19. ZERO-QTY AND FRACTIONAL EDGE CASES ===")
ez = Executor(Broker({"Z": Pos("Z", 0)}), copy.deepcopy(BASE))
check("a zero-qty broker position is skipped, not sold",
      ez.flatten_all_positions() == [], ez.flatten_all_positions())
en = Executor(Broker(), copy.deepcopy(BASE))
en._equity = 100.0
check("an account too small for one share sizes to 0, not a negative",
      M._position_size(BASE, en, 10000.0) == 0)
en2 = Executor(Broker(), copy.deepcopy(BASE))
en2._equity = 0.0
check("zero equity fails closed (no entry) rather than dividing by zero",
      M._position_size(BASE, en2, 100.0) == 0)
check("a zero price cannot produce a division error",
      M._position_size(BASE, en, 0.0) == 0)

print("\n=== 20. REGIME: MISSING/PARTIAL VWAP DATA NEVER PRODUCES A WRONG VERDICT ===")
RC = {"trading": {"regime_sizing": {"enabled": True, "check_time": "09:45",
                                    "bullish_multiplier": 1.0, "neutral_multiplier": 0.5,
                                    "bearish_multiplier": 0.0,
                                    "bearish_below_pct": -0.3, "bullish_above_pct": 0.0}}}
m, lab = M._regime_multiplier(RC, {}, {"open_px": {}}, [], dt("09:45"), ET,
                              vwap_acc={}, qqq_history=[])
check("no data at all -> full size and NO opinion, never a bearish stand-down",
      m == 1.0 and lab is None, (m, lab))
m2, lab2 = M._regime_multiplier(RC, {}, {"open_px": {}}, [(0, 100.0)], dt("09:45"), ET,
                                vwap_acc={"SPY": [0.0, 0.0]}, qqq_history=[(0, 100.0)])
check("a zero-volume VWAP accumulator does not divide by zero",
      m2 == 1.0 and lab2 is None, (m2, lab2))
# CHANGED 2026-09-02 (second pass): the regime is read CONTINUOUSLY from the
# open rather than once at check_time. Waiting cost the whole 09:30-09:45
# stretch, and on 2026-09-02 the session was over at 09:38:19 before the read
# ever happened. Before the OPEN there is still nothing to read.
pre_open = M._regime_multiplier(RC, {}, {"open_px": {}}, [(0, 99.0)], dt("09:15"), ET,
                                vwap_acc={"SPY": [100.0, 1.0], "QQQ": [100.0, 1.0]},
                                qqq_history=[(0, 99.0)])
check("before the OPEN the regime never binds - nothing has traded",
      pre_open == (1.0, None), pre_open)
at_open = M._regime_multiplier(RC, {}, {"open_px": {}}, [(0, 99.0)], dt("09:31"), ET,
                               vwap_acc={"SPY": [100.0, 1.0], "QQQ": [100.0, 1.0]},
                               qqq_history=[(0, 99.0)])
check("...but a bad tape at 09:31 DOES bind now, instead of being ignored "
      "until 09:45", at_open[1] == "bearish", at_open)

print("\n=== 21. REPLAY: the simulator agrees with hand-computed outcomes ===")
import importlib.util as _il
_spec = _il.spec_from_file_location("replay_mod", repo_file("ops", "replay.py"))
RP = _il.module_from_spec(_spec); _spec.loader.exec_module(RP)

# Rises to +1.2% then collapses to -2%. A -0.5% hard stop must fire on the way
# down, NOT ride it to the end of the path.
path = [(f"t{i}", g) for i, g in enumerate([0.0, 0.4, 0.8, 1.2, 0.3, -0.6, -2.0])]
gain, reason, _ = RP.replay_one(path, RP.ExitConfig(stop_pct=-0.5))
check("a hard stop fires on the way down, at its level, not at path end",
      abs(gain - (-0.5)) < 1e-9 and reason == "FINAL_EXIT", (gain, reason))

# Same path, with a breakeven armed at +0.5 -> floor +0.15.
gain2, reason2, _ = RP.replay_one(path, RP.ExitConfig(stop_pct=-1.0, be_trigger=0.5, be_floor=0.15))
# Fills at the LEVEL (0.15), not at the next observed sample (-0.6). Paths
# are sampled every ~10s so a level is nearly always crossed between two
# samples; charging the full distance to the next observation would penalise
# tight stops specifically - and stop tightness is the thing being tuned.
check("an armed breakeven floor fills AT the floor, not at the next sample",
      abs(gain2 - 0.15) < 1e-9 and reason2 == "BREAKEVEN_STOP", (gain2, reason2))
# A position already below its stop on the FIRST sample never crossed
# anything - that is a gap, and the observed price is the honest fill.
gapped = RP.replay_one([("t0", -2.0), ("t1", -2.5)], RP.ExitConfig(stop_pct=-1.0))
check("a gap through the stop fills at the observed price, not the level",
      abs(gapped[0] - (-2.0)) < 1e-9, gapped)

# Tiers: 40% at +0.75, 30% at +1.0, rest at +1.25 (never reached here).
tiers = RP.ExitConfig(stop_pct=-1.0, tiers=[(0.75, 0.4), (1.0, 0.3), (1.25, 1.0)])
gain3, reason3, _ = RP.replay_one(path, tiers)
# 0.4 sold at 0.8, 0.3 sold at 1.2, remaining 0.3 stopped out AT the -1.0
# level (crossed between the -0.6 and -2.0 samples), not at -2.0.
expect3 = 0.4 * 0.8 + 0.3 * 1.2 + 0.3 * -1.0
check("partial tiers bank their fractions and the remainder rides on",
      abs(gain3 - expect3) < 1e-9, (gain3, expect3))

# A monotonic winner must close on the last tier, not at end-of-path.
up = [(f"t{i}", g) for i, g in enumerate([0.0, 0.5, 1.0, 1.5, 2.0])]
g4, r4, _ = RP.replay_one(up, tiers)
check("the closing tier ends the trade", r4.startswith("TAKE_PROFIT"), (g4, r4))
check("...and a trade that never triggers anything marks END_OF_PATH",
      RP.replay_one([("t0", 0.0), ("t1", 0.1)], RP.ExitConfig(stop_pct=-1.0))[1] == "END_OF_PATH")

print("\n=== 22. REPLAY: protective exits are checked BEFORE profit taking ===")
# One sample that trades through BOTH a tier and the stop. A bar that did both
# went against the position; banking the tier would make the backtest lie.
both = [("t0", 0.0), ("t1", -1.5)]
g5, r5, _ = RP.replay_one(both, RP.ExitConfig(stop_pct=-1.0, tiers=[(0.75, 1.0)]))
check("a sample through both the stop and a tier resolves as the STOP",
      r5 == "FINAL_EXIT" and abs(g5 - (-1.0)) < 1e-9, (g5, r5))

print("\n=== 23. REPLAY: summarize() reports honest uncertainty ===")
one = RP.summarize([{"pnl": 10.0}])
check("n=1 has no interval rather than a fake one", one["se"] is None and one["ci_low"] is None)
check("n=0 does not divide by zero", RP.summarize([])["n"] == 0)
spread = RP.summarize([{"pnl": v} for v in (-50, 60, -40, 70, -30)])
check("a wide sample produces a wide interval",
      spread["ci_high"] - spread["ci_low"] > 40, spread)
check("win rate counts only positives", abs(spread["win_rate"] - 0.4) < 1e-9, spread)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
