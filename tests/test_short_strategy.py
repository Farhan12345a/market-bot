"""
The config-gated short strategy (trading.short_strategy.enabled), added
2026-09-27 on explicit user request: "literally just mirroring how we have
the current setup but for short, doing the inverse of everything."

OFF BY DEFAULT. Covers the mechanism end to end: TradeManager's direction
flip (every exit rule mirrored for a short position), Strategy.confirm_entry
threading side through, Executor.submit_entry_order opening a short (which
way the marketable-limit band leans, which way cash moves), retry_unfilled_
entries recognizing a filled short (a NEGATIVE broker quantity, not
positive), close_orphaned_position's short-handling gated on the same flag,
and _short_regime_multiplier mirroring the long side's regime table.
"""
import copy, sys
from _repo import REPO, CONFIG, repo_file
import yaml
import src.main as M
from src.strategy.strategy import Strategy, TradeManager
from src.executor.executor import Executor

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def mk(entry=100.0, qty=100, side="long", cfg=None):
    c = copy.deepcopy(cfg or CFG)
    t = TradeManager("Z", entry, qty, c, side=side)
    st = Strategy(c)
    st.trades["Z"] = t
    t.price_history = [entry] * 5
    return st, t


def px(base, pct):
    return base * (1 + pct / 100)


print("=== A. TRADEMANAGER: DIRECTION FLIPS EVERY EXIT RULE ===")
st, t = mk(side="short")
check("side recorded", t.side == "short")
check("direction is -1 for a short", t.direction == -1)
check("direction is +1 for a long (default, unchanged)", mk()[1].direction == 1)

# A short profits when price FALLS. first_exit_loss_pct is -0.7 by default -
# for a short that means the STOP fires when price RISES 0.7%, not falls.
_, tshort = mk(side="short")
check("a short's first-exit stop fires on a RISE past the threshold",
      tshort.check_first_exit(px(100.0, 0.8)) > 0)
check("...and does NOT fire on a price DECLINE (the favorable direction)",
      tshort.check_first_exit(px(100.0, -0.8)) == 0)

_, tlong = mk(side="long")
check("a long's first-exit stop still fires on a DECLINE, unchanged",
      tlong.check_first_exit(px(100.0, -0.8)) > 0)
check("...and not on a rise", tlong.check_first_exit(px(100.0, 0.8)) == 0)

# take-profit: a short's gain is a price DECLINE.
_, tshort2 = mk(side="short")
tshort2.config["trading"]["use_take_profit"] = True
qty_tp, reason_tp = tshort2.check_take_profit(px(100.0, -1.0))
check("a short's take-profit fires on a decline", qty_tp > 0, (qty_tp, reason_tp))
qty_tp2, _ = tshort2.check_take_profit(px(100.0, 1.0))
check("...not on a rise", qty_tp2 == 0)

# trailing stop: for a short, highest_price tracks the LOWEST price seen
# (the favorable extreme), and the stop sits ABOVE it.
_, tshort3 = mk(side="short")
check("initial trailing anchor is the entry price", tshort3.highest_price == 100.0)
tshort3.update_trailing_stop(px(100.0, -2.0))  # price falls - favorable for a short
check("the anchor moved to the new LOW, not stayed at entry",
      tshort3.highest_price == px(100.0, -2.0), tshort3.highest_price)
# trail_pct default 0.75%; anchor is now at -2.0%, so the trail level sits at
# 98.0 * 1.0075 = 98.735 (-1.265%) - below that, no fire yet.
exit_qty = tshort3.update_trailing_stop(px(100.0, -1.4))
check("trailing stop has NOT fired yet, price is still below the trail level",
      exit_qty == 0, exit_qty)
exit_qty2 = tshort3.update_trailing_stop(px(100.0, -1.0))
check("trailing stop FIRES once price rises back through the trail level",
      exit_qty2 > 0, exit_qty2)

# breakeven: arms on a favorable peak, floor sits on the favorable side of entry.
_, tshort4 = mk(side="short")
tshort4.config["trading"]["use_breakeven_floor"] = True
tshort4.config["trading"]["breakeven_tiers"] = [{"trigger_pct": 0.5, "floor_pct": 0.05}]
tshort4.highest_since_entry = 100.0
tshort4.lowest_since_entry = px(100.0, -0.6)   # peaked (favorably) at -0.6%
check("armed by a favorable (downward) peak past the trigger",
      tshort4.check_breakeven_stop(px(100.0, -0.04)) > 0)

# excursions: MFE for a short is how far price fell; MAE is how far it rose.
_, tshort5 = mk(side="short")
tshort5.highest_since_entry = px(100.0, 0.3)   # adverse for a short
tshort5.lowest_since_entry = px(100.0, -0.9)   # favorable for a short
mfe, mae = tshort5.excursions()
check("MFE is positive (the favorable, downward move)", mfe > 0, mfe)
check("MAE is negative (the adverse, upward move)", mae < 0, mae)

print("\n=== B. STRATEGY.CONFIRM_ENTRY THREADS side THROUGH ===")
strat = Strategy(CFG)
strat.confirm_entry("XYZ", 50.0, 10, side="short")
check("the TradeManager was built with side=short", strat.trades["XYZ"].side == "short")
strat.confirm_entry("ABC", 50.0, 10)
check("default call is still a long", strat.trades["ABC"].side == "long")

print("\n=== C. EXECUTOR.SUBMIT_ENTRY_ORDER: OPENING A SHORT ===")


class Order:
    id = "o1"


class FakeBroker:
    def __init__(self):
        self.limit_calls = []
        self.market_calls = []

    def submit_limit_order(self, symbol, qty, limit_price, side="buy"):
        self.limit_calls.append((symbol, qty, limit_price, side))
        return Order()

    def submit_market_order(self, symbol, qty, side="buy"):
        self.market_calls.append((symbol, qty, side))
        return Order()


ECFG = copy.deepcopy(CFG)
ECFG["trading"]["marketable_limit_entries"] = {
    "enabled": True, "slippage_pct": 0.3, "spread_multiple": 1.5, "max_slippage_pct": 0.6,
}
b = FakeBroker()
ex = Executor(b, ECFG)
order = ex.submit_entry_order("SHRT", 20, 100.0, entry_method="RAPID_DECREASE_CANDIDATE", side="sell")
check("a real order came back", order is not None)
check("the broker call was routed side=sell, not buy",
      b.limit_calls and b.limit_calls[0][3] == "sell", b.limit_calls)
_, _, limit_px, _ = b.limit_calls[0]
check("the marketable limit is BELOW the reference price for a short "
      "(crosses the BID, not the ask)", limit_px < 100.0, limit_px)
check("entry_meta records side=short", ex.entry_meta["SHRT"]["side"] == "short")
check("cash INCREASED (short-sale proceeds), not decreased",
      ex._buying_power > 0, ex._buying_power)
check("exposure is still a positive magnitude regardless of side",
      ex._total_exposure_usd == 20 * 100.0, ex._total_exposure_usd)

b2 = FakeBroker()
ex2 = Executor(b2, ECFG)
ex2.submit_entry_order("LNG", 20, 100.0, entry_method="RAPID_INCREASE_IMMEDIATE", side="buy")
check("a normal long entry still routes side=buy, unchanged",
      b2.limit_calls[0][3] == "buy", b2.limit_calls)
check("...with the limit ABOVE the reference price, unchanged",
      b2.limit_calls[0][2] > 100.0, b2.limit_calls[0][2])
check("cash DECREASED for a long entry, unchanged", ex2._buying_power < 0, ex2._buying_power)

print("\n=== D. retry_unfilled_entries RECOGNIZES A FILLED SHORT ===")


class Pos:
    def __init__(self, qty):
        self.qty = str(qty)


class RetryBroker:
    """Models a short entry that HAS filled (broker holds a negative qty)."""
    def __init__(self, qty_held):
        self.qty_held = qty_held
        self.market_calls = []

    def get_positions(self):
        return {"SHRT": Pos(self.qty_held)} if self.qty_held else {}

    def cancel_open_orders(self, symbol):
        return 0

    def get_latest_quote(self, symbol):
        return None   # no live quote -> forces the market-order fallback

    def submit_market_order(self, symbol, qty, side="buy"):
        self.market_calls.append((symbol, qty, side))
        return Order()


b3 = RetryBroker(qty_held=-20)   # the short DID fill - broker holds -20
ex3 = Executor(b3, ECFG)
ex3._open_symbols.add("SHRT")
ex3._pending_entry_verify["SHRT"] = {"ts": 0.0, "qty": 20, "decision_price": 100.0, "side": "sell"}
filled, abandoned = ex3.retry_unfilled_entries(grace_seconds=0)
check("a filled short is recognized as filled and popped from pending, "
      "not endlessly retried", "SHRT" not in ex3._pending_entry_verify)
check("nothing was force-retried for it", filled == [] and abandoned == [], (filled, abandoned))

b4 = RetryBroker(qty_held=0)   # genuinely still unfilled
ex4 = Executor(b4, ECFG)
ex4._open_symbols.add("SHRT")
ex4._pending_entry_verify["SHRT"] = {"ts": 0.0, "qty": 20, "decision_price": 100.0, "side": "sell"}
filled4, abandoned4 = ex4.retry_unfilled_entries(grace_seconds=0)
check("a genuinely unfilled short still triggers the forced retry",
      len(filled4) == 1, filled4)
check("...and the forced retry is submitted side=sell, never side=buy "
      "(which would erroneously COVER a position that was never opened)",
      b4.market_calls == [("SHRT", 20, "sell")], b4.market_calls)

print("\n=== E. close_orphaned_position: SHORT HANDLING GATED ON short_strategy.enabled ===")


class Pos2:
    def __init__(self, qty, px=50.0):
        self.qty = str(qty)
        self.avg_entry_price = str(px)
        self.current_price = str(px)


class OrphanBroker:
    def __init__(self, qty):
        self.qty = qty
        self.buy_calls = []

    def get_positions(self):
        return {"ORPH": Pos2(self.qty)}

    def cancel_open_orders(self, symbol):
        return 0

    def submit_market_order(self, symbol, qty, side="buy"):
        self.buy_calls.append((symbol, qty, side))
        return Order()

    def submit_limit_order(self, symbol, qty, limit_price, side="buy"):
        return None


off_cfg = copy.deepcopy(CFG)
off_cfg["trading"]["short_strategy"] = {"enabled": False}
ob = OrphanBroker(qty=-15)
oe = Executor(ob, off_cfg)
result = oe.close_orphaned_position("ORPH")
check("with short_strategy OFF, a negative orphan is left alone (loud path only)",
      result is None)
check("no cover order submitted", ob.buy_calls == [], ob.buy_calls)

on_cfg = copy.deepcopy(CFG)
on_cfg["trading"]["short_strategy"] = {"enabled": True}
ob2 = OrphanBroker(qty=-15)
oe2 = Executor(ob2, on_cfg)
result2 = oe2.close_orphaned_position("ORPH")
check("with short_strategy ON, a persistent untracked short IS closed (covered)",
      result2 is not None)
check("it was COVERED (side=buy), not sold again",
      ob2.buy_calls == [("ORPH", 15, "buy")], ob2.buy_calls)

print("\n=== F. main.py: THE RECONCILE GATE ALSO RESPECTS short_strategy.enabled ===")
src = open(repo_file("src", "main.py")).read()
check("the short-orphan gate checks the SAME config flag",
      '_short_enabled = ((config.get("trading") or {}).get("short_strategy") or {}).get("enabled", False)'
      in src)
check("...and only lets a negative held-qty through when it is set",
      "if _held < 0 and not _short_enabled:" in src)

print("\n=== G. _short_regime_multiplier: THE MIRROR OF THE LONG TABLE ===")
sc = copy.deepcopy(CFG)
sc["trading"]["short_strategy"] = {
    "enabled": True, "bearish_multiplier": 1.0, "bullish_multiplier": 0.0,
}
check("bearish -> full short size (mirror of the long side standing down)",
      M._short_regime_multiplier(sc, "bearish") == 1.0)
check("bullish -> no new shorts (mirror of the long side's full size)",
      M._short_regime_multiplier(sc, "bullish") == 0.0)
check("no label yet -> 1.0, no opinion, same convention as the long side",
      M._short_regime_multiplier(sc, None) == 1.0)
sc_off = copy.deepcopy(CFG)
sc_off["trading"]["short_strategy"] = {"enabled": False}
check("disabled -> always 1.0 regardless of label",
      M._short_regime_multiplier(sc_off, "bearish") == 1.0)

print("\n=== H. LIVE CONFIG ===")
t_ = CFG["trading"]
check("short_strategy is shipped OFF by default", t_["short_strategy"]["enabled"] is False)
check("short exit tiers are the exact inverse magnitudes of the long side",
      t_["short_strategy"]["exits"]["first_exit_loss_pct"] == t_["first_exit_loss_pct"]
      and t_["short_strategy"]["exits"]["final_exit_loss_pct"] == t_["final_exit_loss_pct"]
      and t_["short_strategy"]["exits"]["trailing_stop_pct"] == t_["trailing_stop_pct"])
check("extended_hours_experiment is configured",
      "extended_hours_experiment" in t_ and "end_time" in t_["extended_hours_experiment"])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
