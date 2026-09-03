"""
The 2026-09-02 second batch, feature by feature.

Everything here was built in response to one session: the account hit its
daily loss limit at 09:38:19, eight minutes after the open, on 22 entries
whose three largest losses were all XLK names bought inside 96 seconds. Each
suite section names the specific failure it is guarding against, because a
test that only asserts current behaviour cannot tell you when that behaviour
was the bug.
"""
import copy
import datetime
import types
import pytz
import yaml
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.strategy.strategy import TradeManager
from src.executor.executor import Executor, is_stop_loss_exit, is_partial_exit, PHANTOM_EXIT
from src.analytics.dynamic_stops import DynamicStops
from src.notifications import alerts

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def at(h, m, s=0):
    """A time on TODAY's date - parse_hhmm_today builds its boundaries against
    the current date, so a fixed historical date would never compare right."""
    today = datetime.datetime.now(ET).date()
    return ET.localize(datetime.datetime(today.year, today.month, today.day, h, m, s))


# ===================================================================
print("=== 1. SPREAD GATE: unknown is not the same as wide ===")
# 2026-09-02: EVERY burst refusal was this gate, on quotes that cannot be real.
# WDAY at ~$230 quoted an "11.2% spread" - a $26-wide market.
class Q:
    def __init__(s, spread): s._s = spread
    def get_latest_quote(s, sym):
        return None if s._s is None else {"bid": 100.0, "ask": 100.0 + s._s, "spread": s._s}


def md(spread):
    return types.SimpleNamespace(broker=Q(spread))


M._SPREAD_SAMPLES.clear()
check("a normal 0.05% spread is reported as-is",
      abs(M._usable_spread_pct(CFG, md(0.05), "AAA", 100.0) - 0.05) < 1e-6)
M._SPREAD_SAMPLES.clear()
check("an 11.2% reading (the WDAY case) is discarded as implausible -> unknown",
      M._usable_spread_pct(CFG, md(11.2), "WDAY", 100.0) is None)
M._SPREAD_SAMPLES.clear()
check("a 15.7% reading (the AI case) too",
      M._usable_spread_pct(CFG, md(15.7), "AI", 100.0) is None)
M._SPREAD_SAMPLES.clear()
check("a broker that returns nothing -> unknown, not a crash",
      M._usable_spread_pct(CFG, md(None), "ZZZ", 100.0) is None)

# One bad tick cannot move a median. AI read 0.290, 15.703, 16.058, 0.290
# inside ninety seconds - that oscillation is what the median exists for.
M._SPREAD_SAMPLES.clear()
for sp in (0.29, 0.31, 0.30):
    M._usable_spread_pct(CFG, md(sp), "AI", 100.0)
before = M._usable_spread_pct(CFG, md(0.30), "AI", 100.0)
after = M._usable_spread_pct(CFG, md(15.7), "AI", 100.0)   # implausible: dropped
check("an implausible reading does not enter the median at all", before == after, (before, after))
check("the ceiling is configured and sane",
      0 < CFG["trading"]["max_plausible_spread_pct"] <= 5,
      CFG["trading"]["max_plausible_spread_pct"])

msrc = open(repo_file("src", "main.py")).read()
check("the burst gate ABSTAINS on unknown rather than refusing",
      "if spread_pct is None:" in msrc and "the gate abstains" in msrc)
check("...and still refuses on a KNOWN-wide spread",
      "elif move < spread_pct * ratio:" in msrc)

# ===================================================================
print("\n=== 2. STREAM WATCHDOG: the budget starts at the BELL ===")
# 2026-09-02: subscribed ~09:28, watchdog woke at 09:30:01, `now >= open_at`
# was already true, the old code returned -inf, max() fell through to
# _started_at (120s ago, all of it pre-market) and killed a healthy socket one
# second after the open. Cost the burst its first 90 seconds.
import src.data.stream as ST


def budget_elapsed(sub_h, sub_m, sub_s, now_h, now_m, now_s):
    """Seconds of the watchdog's budget consumed, replicating the caller."""
    BASE = 100000.0
    sub_now = at(sub_h, sub_m, sub_s)
    now = at(now_h, now_m, now_s)
    mono = BASE + (now - sub_now).total_seconds()

    open_at = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_at = now.replace(hour=16, minute=0, second=0, microsecond=0)
    open_mono = float("-inf") if now >= close_at else mono + (open_at - now).total_seconds()
    return mono - max(BASE, open_mono)


check("subscribed 09:28, at 09:30:01 only 1s of budget is spent (was 120s)",
      abs(budget_elapsed(9, 28, 1, 9, 30, 1) - 1) < 0.001,
      budget_elapsed(9, 28, 1, 9, 30, 1))
check("...so it does NOT give up one second after the bell",
      budget_elapsed(9, 28, 1, 9, 30, 1) < ST.NO_DATA_GIVE_UP_SECONDS)
check("still gives up on real silence: 09:32:01 is 121s of MARKET-HOURS silence",
      budget_elapsed(9, 28, 1, 9, 32, 1) >= ST.NO_DATA_GIVE_UP_SECONDS)
check("a 4-minute-early subscribe gets the same full budget",
      abs(budget_elapsed(9, 26, 0, 9, 31, 0) - 60) < 0.001)
check("pre-market silence never counts (09:29 is before the bell)",
      budget_elapsed(9, 28, 1, 9, 29, 0) <= 0)
ssrc = open(repo_file("src", "data", "stream.py")).read()
check("the intraday -inf shortcut is gone", "if now >= open_at:\n                return float" not in ssrc)
check("...replaced by a close-time boundary", "close_at" in ssrc)

# ===================================================================
print("\n=== 3. EXIT LABELS: built from the trade's OWN config ===")
# Every burst trade in the history was stamped "FINAL_EXIT_-1.0%" while
# actually running the burst's -0.35% rule. Those strings go into
# trade_history.csv and are read back by replay/grid/be-outcomes.
burst = copy.deepcopy(CFG)
burst["trading"]["final_exit_loss_pct"] = -0.35
burst["trading"]["first_exit_loss_pct"] = -0.3
bt = TradeManager("AI", 10.4586, 400, burst)
bt.price_history = [10.4586]
res = None
for px in (10.45, 10.42, 10.415, 10.40, 10.38):
    from src.strategy.strategy import Strategy
res = bt.check_final_exit(10.40)
check("a burst position exits on ITS OWN -0.35%, not -1.0%", res == 400, res)

sstrat = open(repo_file("src", "strategy", "strategy.py")).read()
check("the reason string is built from config, not hardcoded",
      '_final_label = f"FINAL_EXIT_{' in sstrat)
check("...and the hardcoded literals are gone from the checks list",
      '("FINAL_EXIT_-1.0%", trade.check_final_exit)' not in sstrat)
check("first_exit_done matches by PREFIX so a burst partial is recorded",
      'startswith("FIRST_EXIT_")' in sstrat)
check("is_partial_exit treats FIRST_EXIT_-0.3% as partial",
      is_partial_exit("FIRST_EXIT_-0.3%", 30, 100) is True)
check("...and a full sale as full", is_partial_exit("FINAL_EXIT_-0.35%", 100, 100) is False)
check("is_stop_loss_exit catches every threshold variant",
      all(is_stop_loss_exit(r) for r in
          ("FINAL_EXIT_-1.0%", "FINAL_EXIT_-0.35%", "FIRST_EXIT_-0.5%",
           "FIRST_EXIT_-0.3%", "TRAILING_STOP", "FLATTEN_ALL")))
check("...and does NOT call a take-profit a stop loss",
      not is_stop_loss_exit("TAKE_PROFIT_1.0%") and not is_stop_loss_exit("BREAKEVEN_STOP"))

# ===================================================================
print("\n=== 4. PHANTOM COOLDOWN: a failure to fill is not a loss ===")
# WDAY submitted 8x and RBLX 4x in five minutes. drop_phantom cleared tracking
# and nothing else; reentry_cooldown_after_loss_only let each straight back in.
class B:
    def __init__(s, held=None): s.held = held or {}
    def get_positions(s):
        # dict, matching AlpacaBroker.get_positions - {symbol: position}
        return {k: types.SimpleNamespace(symbol=k, qty=str(v)) for k, v in s.held.items()}
    def cancel_open_orders(s, sym): return 0
    def submit_market_order(s, sym, qty, side="sell"): return types.SimpleNamespace(id="x")
    def submit_limit_order(s, sym, qty, px, side="sell"): return types.SimpleNamespace(id="x")


ex = Executor(B({}), copy.deepcopy(CFG))
ex.open_entries["WDAY"] = 203.59
check("a phantom exit returns the sentinel",
      ex.submit_exit_order("WDAY", 40, "FIRST_EXIT_-0.5%", 203.0) is PHANTOM_EXIT)
check("...and ARMS a cooldown on the drop", ex.phantom_cooldown_remaining("WDAY") > 0)
check("...for about phantom_reentry_cooldown_minutes",
      abs(ex.phantom_cooldown_remaining("WDAY")
          - CFG["trading"]["phantom_reentry_cooldown_minutes"] * 60) < 5)
check("the ordinary cooldown honours it too",
      ex.reentry_cooldown_remaining("WDAY") > 0)
check("an untouched symbol is free", ex.phantom_cooldown_remaining("NVDA") == 0)
check("reentry_cooldown_after_loss_only is still true - this is NOT that switch",
      CFG["trading"]["reentry_cooldown_after_loss_only"] is True)
check("the entry path checks the phantom cooldown BEFORE the skippable one",
      msrc.index('getattr(executor, "phantom_cooldown_remaining"')
      < msrc.index("cooldown_left = 0 if skip_cooldown"))
check("...and the burst's skip_reentry_cooldown cannot bypass it",
      "skip_cooldown" not in msrc.split("phantom_left =")[1].split("return False")[0])
check("...and a missing method degrades to no-cooldown rather than raising "
      "into the entry path",
      'lambda _s: 0.0' in msrc)

print("\n--- attempt cap ---")
ex2 = Executor(B({}), copy.deepcopy(CFG))
ex2._equity = 100000.0; ex2._buying_power = 100000.0
cap = CFG["trading"]["max_entry_attempts_per_symbol_per_day"]
for _ in range(cap):
    ex2._count_entry_attempt("WDAY")
ok, why = ex2.pre_entry_check(10, 100.0, symbol="WDAY")
check(f"refused after {cap} submissions", ok is False, why)
check("...naming the cap", "max_entry_attempts_per_symbol_per_day" in (why or ""), why)
ok2, _ = ex2.pre_entry_check(10, 100.0, symbol="RBLX")
check("a different symbol is unaffected", ok2 is True)
check("attempts counted per DAY", ex2.entry_attempts_today("WDAY") == cap)
check("8 WDAY submissions could not happen under this cap", cap < 8)

# ===================================================================
print("\n=== 5. DYNAMIC STOPS: ATR, capped so it can only tighten ===")
eng = DynamicStops(CFG, history={}, atr_by_symbol={"Q": 0.4, "N": 0.9, "W": 3.0})
static = CFG["trading"]["final_exit_loss_pct"]
for sym, atr, want in (("Q", 0.4, -0.4), ("N", 0.9, -0.9)):
    got, why = eng.stop_for(sym, 0.0)
    check(f"ATR {atr}% -> stop {want}% (tighter than the static {static}%)",
          abs(got - want) < 1e-9, (got, why))
got, why = eng.stop_for("W", 0.0)
check("ATR 3.0% is CAPPED at the static stop, never looser",
      got == static and "CAPPED" in why, (got, why))
got, why = eng.stop_for("UNKNOWN", 0.0)
check("no ATR and no history -> the static stop, never riskier", got == static, (got, why))
off = copy.deepcopy(CFG); off["trading"]["dynamic_stops"]["enabled"] = False
check("disabled -> static, unchanged",
      DynamicStops(off, atr_by_symbol={"W": 3.0}).stop_for("W", 0.0)[0] == static)
check("it is ON in the shipped config", CFG["trading"]["dynamic_stops"]["enabled"] is True)
check("wired at screener completion", "_DYNAMIC_STOPS[\"engine\"] = _build_dynamic_stops" in msrc)
check("...and applied at entry", "_resolved_exit_cfg = _dynamic_exit_config(" in msrc)

# ===================================================================
print("\n=== 6. VOLATILITY SIZING: equal dollar swing, scaled DOWN only ===")
M._DYNAMIC_STOPS["engine"] = DynamicStops(
    CFG, history={}, atr_by_symbol={"Q": 0.4, "MID": 1.5, "W": 3.0, "X": 6.0})
exq = types.SimpleNamespace(equity=100000.0, regime_size_multiplier=1.0)
base = M._position_size(CFG, exq, 100.0, symbol="MID")
check("a reference-ATR name gets full size",
      base == M._position_size(CFG, exq, 100.0, symbol=None), base)
wild = M._position_size(CFG, exq, 100.0, symbol="W")
check("ATR 3.0% (2x the reference) is halved", abs(wild - base / 2) <= 1, (base, wild))
check("...so the dollar swing at 1 ATR is equalised",
      abs(wild * 100 * 3.0 - base * 100 * 1.5) < 200, (wild * 300, base * 150))
extreme = M._position_size(CFG, exq, 100.0, symbol="X")
check("ATR 6.0% is floored, reduced not refused", 0 < extreme < wild, extreme)
check("...at about min_multiplier",
      abs(extreme - base * CFG["trading"]["volatility_sizing"]["min_multiplier"]) <= 2)
quiet = M._position_size(CFG, exq, 100.0, symbol="Q")
check("a QUIET name is NOT scaled up - the slot share is the exposure guarantee",
      quiet == base, (quiet, base))
check("an unknown symbol is unaffected", M._position_size(CFG, exq, 100.0, symbol="ZZ") == base)
M._DYNAMIC_STOPS["engine"] = None

# ===================================================================
print("\n=== 7. REGIME: continuous, with hysteresis ===")
rc = CFG["trading"]["regime_sizing"]
check("check_time moved to 09:40 (09:45 never ran on 09-02)", rc["check_time"] == "09:40")
cad = rc["cadence"]
check("09:30-09:40 evaluates every poll", cad["opening_seconds"] == 0)
check("09:40-10:00 every minute", cad["morning_seconds"] == 60)
check("after 10:00 every five minutes", cad["afternoon_seconds"] == 300)
for h, m, want in ((9, 31, 0), (9, 50, 60), (10, 30, 300)):
    check(f"cadence at {h}:{m:02d} is {want}s", M._regime_interval(rc, at(h, m), ET) == want,
          M._regime_interval(rc, at(h, m), ET))
check("before the open there is nothing to read", M._regime_interval(rc, at(9, 15), ET) is None)

st = {}
check("not due before the open", M._regime_due(rc, st, at(9, 15), ET) is False)
check("due at the open", M._regime_due(rc, st, at(9, 31), ET) is True)
st["last_eval"] = at(9, 50)
check("not due 30s later in the 60s band", M._regime_due(rc, st, at(9, 50, 30), ET) is False)
check("due 61s later", M._regime_due(rc, st, at(9, 51, 1), ET) is True)

print("\n--- hysteresis ---")
need = rc["confirmations"]
check("confirmations is at least 2", need >= 2, need)


def regime(state, spy, qqq, breadth=None, now=None):
    # _update_vwap accumulates [sum(typical*vol), sum(vol)], so VWAP is
    # slot[0]/slot[1] - here SPY 100.00 and QQQ 200.00.
    acc = {"SPY": [100.0 * 100, 100.0], "QQQ": [200.0 * 100, 100.0]}
    sh = [(None, 100.0 * (1 + spy / 100))]
    qh = [(None, 200.0 * (1 + qqq / 100))]
    return M._regime_multiplier(CFG, state, breadth or {}, sh, now or at(9, 50), ET,
                                vwap_acc=acc, qqq_history=qh)


s = {}
m, l = regime(s, +0.5, +0.5, now=at(9, 31))
check("first reading is adopted immediately (nothing to whipsaw against)", l == "bullish", (m, l))
m, l = regime(s, -0.5, -0.5, now=at(9, 41))
check("one bearish reading does NOT flip it", l == "bullish", (m, l))
check("...and size is unchanged", m == rc["bullish_multiplier"], m)
m, l = regime(s, -0.5, -0.5, now=at(9, 42))
check(f"a {need}th consecutive bearish reading DOES flip it", l == "bearish", (m, l))
check("...to the bearish multiplier", m == rc["bearish_multiplier"], m)
s2 = {}
regime(s2, +0.5, +0.5, now=at(9, 31))
regime(s2, -0.5, -0.5, now=at(9, 41))
m, l = regime(s2, +0.5, +0.5, now=at(9, 42))
check("an interrupted run abandons the part-built case", l == "bullish", (m, l))
m, l = regime(s2, -0.5, -0.5, now=at(9, 43))
check("...and the count restarts rather than resuming", l == "bullish", (m, l))

# ===================================================================
print("\n=== 8. CHOP: the fourth label, read from OUR names not SPY ===")
# 2026-08-28: SPY flat (-0.018%) while the average signal returned -1.045%.
# A market-level detector calls that a normal day.
choppy = {"mean_move": -0.02, "dispersion": 0.9, "choppy_symbols": 7}
ok, why = M._chop_reading(CFG, choppy)
check("flat mean + wide dispersion = CHOPPY", ok, why)
check("...and says why, with both numbers", "dispersion" in (why or "") and "flat" in (why or ""))
check("strong mean is not chop",
      M._chop_reading(CFG, {"mean_move": 0.8, "dispersion": 0.9})[0] is False)
check("falling hard is not chop (that is bearish)",
      M._chop_reading(CFG, {"mean_move": -0.7, "dispersion": 0.5})[0] is False)
check("flat AND quiet is not chop - nothing is happening",
      M._chop_reading(CFG, {"mean_move": 0.05, "dispersion": 0.2})[0] is False)
check("no measurement yet -> not chop, rather than a guess",
      M._chop_reading(CFG, {"mean_move": None, "dispersion": None})[0] is False)
check("thin evidence never fabricates a reading",
      M._chop_reading(CFG, {})[0] is False)

s3 = {}
m, l = regime(s3, +0.5, +0.5, breadth=choppy, now=at(9, 31))
check("a flat index with a choppy watchlist reads CHOPPY, not bullish", l == "choppy", (m, l))
check("...at the choppy multiplier", m == rc["choppy_multiplier"], m)
s4 = {}
regime(s4, -0.5, -0.5, breadth=choppy, now=at(9, 31))
check("BEARISH keeps precedence over choppy - a falling tape is worse",
      s4.get("label") == "bearish", s4)

print("\n--- the chop exit ladder ---")
normal_tiers = [t["gain_pct"] for t in CFG["trading"]["take_profit_tiers"]]
chop_tiers = [t["gain_pct"] for t in rc["chop"]["take_profit_tiers"]]
check("the chop ladder is LOWER at every tier",
      all(c < n for c, n in zip(chop_tiers, normal_tiers)), (chop_tiers, normal_tiers))
check("its first tier is at or below +0.5% - the whole move on a chop day",
      chop_tiers[0] <= 0.5, chop_tiers)
out = M._chop_exit_config(CFG, {"label": "choppy"}, None)
check("a choppy regime lowers the ladder for a new entry",
      [t["gain_pct"] for t in out["trading"]["take_profit_tiers"]] == chop_tiers)
check("a normal regime leaves the entry untouched",
      M._chop_exit_config(CFG, {"label": "bullish"}, None) is None)
check("...and no regime at all is also untouched",
      M._chop_exit_config(CFG, {}, None) is None)

print("\n--- breadth_halt is retired, its measurement is not ---")
check("the halt function no longer exists", not hasattr(M, "_breadth_halt"))
check("the measurement does", hasattr(M, "_measure_breadth"))
check("the retired config key is gone", "breadth_halt" not in CFG["trading"])
check("the measurement block remains", CFG["trading"]["breadth"]["enabled"] is True)
check("nothing in the entry path reads a halt flag", "if halted:" not in msrc)

# ===================================================================
print("\n=== 9. REGIME-DRIVEN STOP TIGHTENING (monotonic) ===")
# 09-02: nine longs each travelled independently to its own -1.0% stop for one
# shared reason that was knowable while they were open.
tm = TradeManager("NOW", 142.64, 58, copy.deepcopy(CFG))
be = rc["bearish_exits"]
note = tm.tighten_for_regime(be["final_exit_loss_pct"], be["trailing_stop_pct"],
                             be["breakeven_trigger_pct"])
check("tightening reports what changed", bool(note), note)
check("the final stop is tighter",
      tm.config["trading"]["final_exit_loss_pct"] == be["final_exit_loss_pct"])
check("the trail is tighter",
      tm.config["trading"]["trailing_stop_pct"] == be["trailing_stop_pct"])
check("re-applying is idempotent",
      tm.tighten_for_regime(be["final_exit_loss_pct"], be["trailing_stop_pct"],
                            be["breakeven_trigger_pct"]) is None)
check("it REFUSES to loosen - never moves a stop away from a losing position",
      tm.tighten_for_regime(-5.0, 9.0, 9.0) is None)
check("...and the values are unchanged after that attempt",
      tm.config["trading"]["final_exit_loss_pct"] == be["final_exit_loss_pct"]
      and tm.config["trading"]["trailing_stop_pct"] == be["trailing_stop_pct"])
check("NOW would have exited at -0.5% (141.90), not -1.46% (140.555)",
      tm.check_final_exit(141.90) == 58 and tm.check_final_exit(142.00) == 0)
check("the tightening does not leak into the shared config",
      CFG["trading"]["final_exit_loss_pct"] == -1.0)
check("it fires once per bearish transition, not every poll",
      'regime_state.get("tightened_at") != _label' in msrc)

# ===================================================================
print("\n=== 10. TRAIL TIGHTENING: tiers ratchet, stalls tighten ===")
tt = CFG["trading"]["trail_tightening"]
tm2 = TradeManager("T", 100.0, 100, copy.deepcopy(CFG))
full = CFG["trading"]["trailing_stop_pct"]
check("a fresh position uses the configured trail", tm2.effective_trail_pct() == full)
tm2.take_profit_tiers_done = {0}
check("one filled tier pulls it in",
      abs(tm2.effective_trail_pct() - (full - tt["tighten_per_tier_pct"])) < 1e-9,
      tm2.effective_trail_pct())
tm2.take_profit_tiers_done = {0, 1}
check("two tiers pull it in twice",
      abs(tm2.effective_trail_pct() - (full - 2 * tt["tighten_per_tier_pct"])) < 1e-9)
tm2.take_profit_tiers_done = set(range(20))
check("it never goes below min_trail_pct - a trail inside the spread exits on nothing",
      tm2.effective_trail_pct() >= tt["min_trail_pct"], tm2.effective_trail_pct())

tm3 = TradeManager("S", 100.0, 100, copy.deepcopy(CFG))
tm3.update_trailing_stop(100.5)
check("a new high resets the stall clock", tm3.last_high_at is not None)
check("...and the trail is still full while it is working",
      tm3.effective_trail_pct() == full)
tm3.last_high_at = tm3.entry_time - datetime.timedelta(minutes=tt["stall_after_minutes"] + 1)
check("a stalled position tightens to stall_trail_pct",
      tm3.effective_trail_pct() == tt["stall_trail_pct"], tm3.effective_trail_pct())
check("...so it exits sooner: 100.15 off a 100.50 peak",
      tm3.update_trailing_stop(100.35) == 0 and tm3.update_trailing_stop(100.15) == 100)
tm4 = TradeManager("B", 100.0, 100, copy.deepcopy(CFG))
tm4.take_profit_tiers_done = {0, 1}
tm4.last_high_at = tm4.entry_time - datetime.timedelta(minutes=30)
check("when both tighteners apply, the TIGHTER wins",
      tm4.effective_trail_pct() == min(tt["stall_trail_pct"],
                                       full - 2 * tt["tighten_per_tier_pct"]))
off2 = copy.deepcopy(CFG); off2["trading"]["trail_tightening"]["enabled"] = False
tm5 = TradeManager("O", 100.0, 100, off2)
tm5.take_profit_tiers_done = {0, 1}
tm5.last_high_at = tm5.entry_time - datetime.timedelta(minutes=30)
check("disabled -> exactly the old fixed behaviour", tm5.effective_trail_pct() == full)

# ===================================================================
print("\n=== 11. MARKETABLE LIMIT EXITS, escalating to market ===")
mle = CFG["trading"]["marketable_limit_exits"]
calls = []


class B2(B):
    def submit_limit_order(s, sym, qty, px, side="sell"):
        calls.append(("limit", px, side)); return types.SimpleNamespace(id="L")
    def submit_market_order(s, sym, qty, side="sell"):
        calls.append(("market", None, side)); return types.SimpleNamespace(id="M")


ex3 = Executor(B2({"AAA": 100}), copy.deepcopy(CFG))
ex3.open_entries["AAA"] = 100.0
ex3.submit_exit_order("AAA", 100, "FINAL_EXIT_-1.0%", 99.0)
check("a sell is routed as a LIMIT", calls and calls[0][0] == "limit", calls)
check("...priced THROUGH the reference, so it still crosses",
      calls[0][1] < 99.0, calls[0][1])
check("...by about slippage_pct",
      abs(calls[0][1] - 99.0 * (1 - mle["slippage_pct"] / 100)) < 0.02, calls[0][1])
for _ in range(mle["max_attempts"]):
    ex3.open_entries["AAA"] = 100.0
    ex3.submit_exit_order("AAA", 100, "FINAL_EXIT_-1.0%", 99.0)
check("after max_attempts unfilled limits it ESCALATES to market - any exit "
      "beats a position the stop has condemned",
      calls[-1][0] == "market", calls[-1])
off3 = copy.deepcopy(CFG); off3["trading"]["marketable_limit_exits"]["enabled"] = False
calls.clear()
ex4 = Executor(B2({"BBB": 50}), off3); ex4.open_entries["BBB"] = 10.0
ex4.submit_exit_order("BBB", 50, "FLATTEN_ALL", 9.9)
check("disabled -> plain market orders, unchanged", calls[0][0] == "market", calls)

# ===================================================================
print("\n=== 12. DAILY LOSS LIMIT: percent of equity, hard ceiling ===")
dll = CFG["trading"]["daily_loss_limit"]


def limit_at(equity, cfg=None):
    e = Executor.__new__(Executor)
    e.config = cfg or CFG; e._equity = equity; e._logged_loss_limit = None
    # daily_loss_limit_usd() also reads these now (the P&L line on its log,
    # and the once-per-~10min throttle) - a bare __new__() stub must set them
    # too, or the AttributeError is swallowed by the function's own
    # fail-safe except and silently returns the WRONG (static fallback)
    # number instead of raising where the mistake would be obvious.
    e._realized_pnl_today = 0.0
    e._unrealized_pl_cached = 0.0
    e._last_loss_limit_log_at = 0.0
    return e.daily_loss_limit_usd()


_pct = dll["pct_of_equity"] / 100.0
_below_floor_equity = dll["floor_usd"] / _pct * 0.5      # comfortably under the floor
_mid_band_equity = (dll["floor_usd"] + dll["ceiling_usd"]) / 2 / _pct  # lands strictly between

check("comfortably under the floor -> the floor",
      limit_at(_below_floor_equity) == dll["floor_usd"], limit_at(_below_floor_equity))
check("mid-band equity -> the raw percentage, not clamped",
      abs(limit_at(_mid_band_equity) - _mid_band_equity * _pct) < 1, limit_at(_mid_band_equity))
check("$200k -> CAPPED at the ceiling, does not double",
      limit_at(200000) == dll["ceiling_usd"], limit_at(200000))
check("$500k -> still the ceiling. The account growing never authorises "
      "larger losses on its own",
      limit_at(500000) == dll["ceiling_usd"], limit_at(500000))
check("$20k -> the floor, so a drawdown cannot shrink it to nothing",
      limit_at(20000) == dll["floor_usd"], limit_at(20000))
check("equity unknown -> falls back to the fixed limit, never to NO limit",
      limit_at(0) == CFG["trading"]["max_daily_loss_usd"], limit_at(0))
fixed = copy.deepcopy(CFG); fixed["trading"]["daily_loss_limit"]["mode"] = "fixed"
check("mode: fixed uses max_daily_loss_usd unchanged",
      limit_at(200000, fixed) == CFG["trading"]["max_daily_loss_usd"])
esrc = open(repo_file("src", "executor", "executor.py")).read()
check("the velocity warnings read the SAME computed number",
      "max_loss = abs(self.daily_loss_limit_usd() or 0)" in esrc)
check("...and so does the hard check",
      "max_loss = self.daily_loss_limit_usd()" in esrc)

# ===================================================================
print("\n=== 13. ALERTS: the machinery finally has call sites ===")
class N:
    def __init__(s, fail=False): s.sent = []; s.fail = fail
    def send_alert(s, sub, txt):
        if s.fail: raise RuntimeError("channel down")
        s.sent.append((sub, txt)); return True


n = N()
alerts.session_ended(CFG, n, reason="daily_loss_limit", realized=-403.66,
                     unrealized=-113.41, entries=22, trades=13, open_positions=0)
check("session end is sent", len(n.sent) == 1)
check("...with realized AND unrealized broken out",
      "$-403.66" in n.sent[0][1] and "$-113.41" in n.sent[0][1])
check("...and the total", "$-517.07" in n.sent[0][1], n.sent[0][1])
check("...and the reason in the subject", "daily_loss_limit" in n.sent[0][0])

n2 = N()
alerts.loss_limit_hit(CFG, n2, daily_pnl=-517.07, limit=-500, entries=22, elapsed_minutes=8)
check("the loss limit alert names the loss and the limit",
      "$-517.07" in n2.sent[0][1] and "$-500.00" in n2.sent[0][1])
check("...and the burn rate", "/min" in n2.sent[0][1], n2.sent[0][1])

n3 = N()
alerts.positions_left_open(CFG, n3, symbols=["NOW", "WDAY"], when="time_stop")
check("positions left open names them and the remedy",
      "NOW" in n3.sent[0][1] and "flatten-now.py" in n3.sent[0][1])

n4 = N()
alerts.preflight(CFG, n4, status="PASS", passed=31, failed=0, warnings=2)
check("preflight is sent on PASS too - silence must not look like health",
      len(n4.sent) == 1 and "PASS" in n4.sent[0][0])

check("a dead channel NEVER raises into the caller",
      alerts.session_ended(CFG, N(fail=True), reason="x", realized=0, unrealized=0,
                           entries=0, trades=0) is False)
check("no notifier at all is also safe",
      alerts.crashed(CFG, None, error=RuntimeError("boom")) is False)
off4 = copy.deepcopy(CFG); off4["notifications"]["alerts"]["enabled"] = False
n5 = N()
alerts.session_ended(off4, n5, reason="x", realized=0, unrealized=0, entries=0, trades=0)
check("the master switch suppresses everything", n5.sent == [])
check("alerts are ON in the shipped config",
      CFG["notifications"]["alerts"]["enabled"] is True)
check("finish_day sends one", "_AL.session_ended(" in msrc)
check("the loss limit sends one BEFORE flattening",
      msrc.index("_AL.loss_limit_hit(") < msrc.index("flattened = executor.flatten_all_positions()"))
check("the crash handler alerts BEFORE cleanup that can itself fail",
      msrc.index("_AL.crashed(") < msrc.index("executor.flatten_all_positions()\n            executor.save_trades_log()"))

# ===================================================================
print("\n=== 14. EARLIER ENTRIES: baseline from the opening print ===")
check("the baseline is seeded from the bar's own open", 'seed = bar.get("open")' in msrc)
check("...falling back to the observed price when the bar has none",
      "baseline[symbol] = seed or price" in msrc)
check("...and only spends a poll establishing it when there is no open",
      "if not seed:" in msrc)
check("the fast poll covers the burst window, not just open positions",
      'state.get("positions_open") or state.get("burst_open")' in msrc)
check("burst_open is published each poll",
      'poll_state["burst_open"] = bool(ob) and not opening_state.get("done")' in msrc)
fp = CFG["trading"]["opening_fast_poll"]
check("the fast interval is 2-3s", 2 <= fp["seconds"] <= 3, fp)
check("...and only for the opening minutes", fp["minutes_after_open"] <= 20, fp)

# ===================================================================
print("\n=== 15. EXTENSION GATE + BURST SUPPRESSION ===")
# WDAY's 3-minute change was +0.95% (under the 1.25% ceiling, so it passed)
# while its move since the open was +1.344%.
cap2 = CFG["trading"]["max_extension_from_open_pct"]
check("the extension cap is distinct from rapid_increase_max_pct",
      cap2 != CFG["trading"]["rapid_increase_max_pct"], (cap2, CFG["trading"]["rapid_increase_max_pct"]))
check("WDAY at +1.344% from the open would be refused", 1.344 > cap2)
check("CRM at +1.105% too", 1.105 > cap2)
check("RBLX at +1.575% too", 1.575 > cap2)
check("a fresh +0.4% mover is NOT refused", 0.4 <= cap2)
check("the gate measures from the burst BASELINE, i.e. the open",
      '_base = (opening_state.get("baseline") or {}).get(symbol)' in msrc)
check("the burst records what it measured and declined",
      'state["refused"] = sorted(' in msrc)
check("...and the normal window honours it",
      'symbol in (opening_state.get("refused") or [])' in msrc)
check("both lapse at refused_suppressed_until, so a later move is a new signal",
      "refused_suppressed_until" in msrc
      and CFG["trading"]["opening_burst"]["refused_suppressed_until"] == "09:45")

# ===================================================================
print("\n=== 16. NOTHING REGRESSED: old guards still hold ===")
t = CFG["trading"]
check("sector cap tightened 3 -> 2 (three XLK names did 66% of 09-02's damage)",
      t["max_positions_per_sector"] == 2, t["max_positions_per_sector"])
check("leveraged/basket ETF exclusions still on",
      t["exclude_leveraged_etfs"] is True and t["exclude_basket_etfs"] is True)
check("the stream cap is still 14", t["stream_max_subscriptions"] == 14)
check("trade ticks still on", t["use_trade_ticks_for_entry"] is True)
check("index slots still reserved", t["stream_reserve_index_slots"] is True)
check("the correlation limiter is still enabled", t["correlation_limit"]["enabled"] is True)
check("loss-velocity warnings still enabled", t["loss_velocity_warning"]["enabled"] is True)
check("the phantom guard is still in the exit path", "return PHANTOM_EXIT" in esrc)
check("flatten_all_positions still reads the position SIGN",
      'side = "buy" if raw_qty < 0 else "sell"' in esrc)
check("...and still guards each position separately",
      esrc.count("except Exception") >= 5)
check("the burst still runs its own tighter exit profile",
      t["opening_burst"]["exits"]["final_exit_loss_pct"] == -0.35)
check("multifactor_rank still OFF (it inverted move order)",
      t["opening_burst"]["multifactor_rank"] is False)
check("max_daily_entries unchanged", t["max_daily_entries"] == 50)
check("PDT floor still enforced", "25,000" in esrc or "25000" in esrc)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
