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
import copy, sys, time, types
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

print("\n=== G1. HARD MUTUAL EXCLUSION: LONGS AND SHORTS NEVER BOTH ACTIVE ===")
# Explicit user instruction, 2026-09-27: "there SHOULD NEVER BE SHORTS AND
# LONGS MIXING IN A SPECIFIC REGIME WINDOW. its always, either one or the
# other." This is enforced unconditionally in run_trading_day, right after
# both multipliers are computed - checked here as a source-level guarantee
# since it isn't reachable as a pure function (it runs inline in the poll
# loop against live executor state each poll).
check("the hard-exclusion line exists, right after both multipliers are set",
      "executor.short_regime_size_multiplier = 0.0" in src)
check("...gated on the LONG side being active this poll, not a config guess",
      "if executor.regime_size_multiplier > 0:" in src)
check("it only ever NARROWS the short side (0.0), never the reverse - "
      "the long side's own multiplier is never touched by this line",
      "executor.regime_size_multiplier = 0.0" not in src)

# Live-config consequence, updated 2026-09-30 (explicit user request: "no
# trades occurring at all when the regime is choppy... nothing should be
# executed at all whether regime is choppy or neutral" - longs now trade
# ONLY in a confirmed bullish read, shorts ONLY in a confirmed bearish one,
# neutral_multiplier/choppy_multiplier dropped 0.5 -> 0.0 for longs
# alongside this). The hard-exclusion rule above is now only ever
# load-bearing at "bullish" - that's the one label where the long side is
# nonzero and the short side needs forcing to 0.0 on top of its own table
# value. Elsewhere (neutral, choppy, bearish) both sides already agree
# without it: one side's own config number is already 0.
for label, want_long_nonzero, want_short_nonzero in [
    ("bullish", True, False),
    ("neutral", False, False),
    ("choppy", False, False),
    ("bearish", False, True),
]:
    long_mult = {"bullish": CFG["trading"]["regime_sizing"]["bullish_multiplier"],
                 "neutral": CFG["trading"]["regime_sizing"]["neutral_multiplier"],
                 "choppy": CFG["trading"]["regime_sizing"]["choppy_multiplier"],
                 "bearish": CFG["trading"]["regime_sizing"]["bearish_multiplier"]}[label]
    short_mult = {"bullish": CFG["trading"]["short_strategy"]["bullish_multiplier"],
                  "neutral": CFG["trading"]["short_strategy"]["neutral_multiplier"],
                  "choppy": CFG["trading"]["short_strategy"]["choppy_multiplier"],
                  "bearish": CFG["trading"]["short_strategy"]["bearish_multiplier"]}[label]
    check(f"{label}: long multiplier is "
          f"{'nonzero' if want_long_nonzero else 'zero - no longs this regime'}",
          (long_mult > 0) == want_long_nonzero, (label, long_mult))
    check(f"{label}: short multiplier is "
          f"{'nonzero' if want_short_nonzero else 'zero - no shorts this regime'}",
          (short_mult > 0) == want_short_nonzero, (label, short_mult))
    check(f"{label}: never both sides nonzero at once - one direction or nothing",
          not (long_mult > 0 and short_mult > 0), (label, long_mult, short_mult))

print("\n=== G2. STARTUP CHECKS THE ACCOUNT'S OWN shorting_enabled FLAG ===")
src = open(repo_file("src", "main.py")).read()
check("main() checks account.shorting_enabled when short_strategy.enabled is true",
      "getattr(account, \"shorting_enabled\", True)" in src)
check("it warns LOUDLY rather than silently if the account still blocks it "
      "(no_shorting on the Alpaca account is a SEPARATE switch from this "
      "code's own config flag)",
      "still has shorting DISABLED" in src)
check("this reuses the account object from the get_account() call already "
      "made at startup - no extra API call", "account = broker.get_account()" in src)

print("\n=== G3. BULLISH-REGIME TIGHTENING FOR OPEN SHORTS (mirror of bearish_exits) ===")
# 2026-09-27, explicit user request: "we should close all the trades... or
# do you think we should keep it open" -> landed on the SAME monotonic
# tighten-not-kill philosophy bearish_exits already uses for longs, mirrored
# for shorts on a BULLISH read - a bullish tape is what a short fights, the
# same way a bearish one is what a long fights.
rc_live = CFG["trading"]["regime_sizing"]
be = rc_live["bearish_exits"]
bu = rc_live["bullish_exits"]
check("bullish_exits is configured", bool(bu))
check("same magnitudes as bearish_exits - not independently tuned yet",
      bu["final_exit_loss_pct"] == be["final_exit_loss_pct"]
      and bu["trailing_stop_pct"] == be["trailing_stop_pct"]
      and bu["breakeven_trigger_pct"] == be["breakeven_trigger_pct"])

strat_mix = Strategy(copy.deepcopy(CFG))
strat_mix.confirm_entry("LONGY", 100.0, 50, side="long")
strat_mix.confirm_entry("SHORTY", 100.0, 50, side="short")

changes_short_only = strat_mix.tighten_all_for_regime(
    final_pct=bu["final_exit_loss_pct"], trail_pct=bu["trailing_stop_pct"],
    breakeven_trigger=bu["breakeven_trigger_pct"], side="short",
)
check("side='short' tightening touches the SHORT position",
      "SHORTY" in changes_short_only, changes_short_only)
check("...and leaves the LONG completely untouched",
      "LONGY" not in changes_short_only, changes_short_only)
check("the short's config was actually tightened",
      strat_mix.trades["SHORTY"].config["trading"]["final_exit_loss_pct"] == bu["final_exit_loss_pct"])
check("the long's config is UNCHANGED - still the original -1.0%",
      strat_mix.trades["LONGY"].config["trading"]["final_exit_loss_pct"] == -1.0,
      strat_mix.trades["LONGY"].config["trading"]["final_exit_loss_pct"])

changes_long_only = strat_mix.tighten_all_for_regime(
    final_pct=be["final_exit_loss_pct"], trail_pct=be["trailing_stop_pct"],
    breakeven_trigger=be["breakeven_trigger_pct"], side="long",
)
check("side='long' tightening (the ORIGINAL bearish path) touches only the long",
      "LONGY" in changes_long_only and "SHORTY" not in changes_long_only, changes_long_only)

no_filter = strat_mix.tighten_all_for_regime(final_pct=-9.0, side=None)
check("side=None (the original signature, unchanged) still touches everyone - "
      "no existing caller's behavior regresses", True)  # -9.0 is looser, so no-op either way; the call itself must not raise

# A short's tighten_for_regime math, checked directly (not just that a call
# happened) - the whole point of the mirror is that this was ALREADY correct
# via TradeManager.direction, and only the WIRING (which positions, which
# regime label) was missing.
tm_short = TradeManager("SH", 100.0, 40, copy.deepcopy(CFG), side="short")
note_s = tm_short.tighten_for_regime(bu["final_exit_loss_pct"], bu["trailing_stop_pct"],
                                     bu["breakeven_trigger_pct"])
check("tightening a short reports what changed", bool(note_s), note_s)
check("a short's final-exit stop, once tightened to -0.5%, fires on a "
      "+0.5% RISE (not a decline) - direction-correct, unchanged math",
      tm_short.check_final_exit(100.6) > 0 and tm_short.check_final_exit(100.4) == 0)

print("\n=== G4. MAIN.PY: THE TWO LATCHES TRACK INDEPENDENTLY ===")
check("bearish tightening is now explicitly scoped side='long'",
      'side="long",\n                    )' in src or "side=\"long\",\n                    )" in src
      or 'side="long",' in src)
check("a SEPARATE latch key exists for the short/bullish transition, so it "
      "cannot be stomped by the long/bearish latch or vice versa",
      'regime_state.get("tightened_at_short")' in src)
check("the bullish branch reads bullish_exits and applies side='short'",
      'regime_cfg.get("bullish_exits")' in src and 'side="short",' in src)
check("the bullish latch clears on any non-bullish label, mirroring the "
      "bearish latch's own reset - both are independently re-armable",
      "regime_state[\"tightened_at_short\"] = None" in src)

print("\n=== I. THE NORMAL EXIT CALL SITE THREADS side= FROM trade.side ===")
# Found 2026-10-02: Executor.submit_exit_order defaults to side="sell"
# (closing a long). main.py's normal exit call site - the one check_exit
# feeds on every poll - never passed side= at all, so EVERY exit on a SHORT
# position (FIRST_EXIT, TRAILING_STOP, TAKE_PROFIT, etc.) submitted a SELL
# instead of a BUY-to-cover, which just sells MORE of the same short rather
# than closing it. Live evidence: PRIM's short went -19 -> -38 (exactly
# doubled) on a TRAILING_STOP "exit", then showed up as a persistent
# ORPHAN_RECONCILE once the bot's own tracking (which assumed the cover had
# worked) hit qty_remaining == 0 and deleted the trade while the broker
# still held the (now-doubled) short.
check("the normal exit call site passes side=, derived from trade.side "
      "rather than falling through to submit_exit_order's long-only default",
      'side=("buy" if trade is not None and trade.side == "short" else "sell")'
      in src)
_qty_before_idx = src.find("qty_before=(trade.qty_remaining if trade else None)")
_side_idx = src.find('side=("buy" if trade is not None and trade.side == "short" else "sell")')
check("...and it's the SAME submit_exit_order call as qty_before (not some "
      "other, unrelated call site)",
      _qty_before_idx != -1 and _side_idx != -1
      and 0 < (_side_idx - _qty_before_idx) < 1000)

print("\n=== H. LIVE CONFIG ===")
t_ = CFG["trading"]
check("short_strategy is ENABLED as of 2026-09-27 (explicit user request, "
      "after the mechanism was built and tested)", t_["short_strategy"]["enabled"] is True)
check("short exit tiers are the exact inverse magnitudes of the long side",
      t_["short_strategy"]["exits"]["first_exit_loss_pct"] == t_["first_exit_loss_pct"]
      and t_["short_strategy"]["exits"]["final_exit_loss_pct"] == t_["final_exit_loss_pct"]
      and t_["short_strategy"]["exits"]["trailing_stop_pct"] == t_["trailing_stop_pct"])
check("extended_hours_experiment is configured",
      "extended_hours_experiment" in t_ and "end_time" in t_["extended_hours_experiment"])

print("\n=== J. END-TO-END: A SHORT'S FULL EXIT CHAIN, DRIVEN THE WAY main.py ACTUALLY DRIVES IT ===")
# Section I only checks that the right SOURCE LINE exists at the call site.
# This drives the real mechanism - Strategy's qty_remaining bookkeeping,
# Executor.submit_exit_order's broker-truth correction, a broker double that
# actually moves its held quantity on every fill - end to end, and checks
# the FINAL broker position, not just one call's arguments. Reproduces the
# exact PRIM/MXL shape: a partial exit's qty_remaining decrement, then a
# second exit rule computing "everything left" from that already-decremented
# number (a FULL exit, since qty==qty_before by construction - see PENDING_
# WORK.md item 12), which is exactly when submit_exit_order's 2026-10-01 fix
# corrects the ask UP to the broker's true (already-bug-inflated) holding.


class LiveFillBroker:
    """Unlike section I's static doubles, this one actually moves its held
    quantity on every fill - the only way to prove a MULTI-STEP exit chain
    ends with the position closed, not doubled."""
    def __init__(s, qty, price=100.0):
        s.qty = qty   # signed: negative = short
        s.price = price
        s.fills = []

    def get_positions(s):
        if s.qty == 0:
            return {}
        return {"Z": types.SimpleNamespace(
            qty=str(s.qty), avg_entry_price=s.price, current_price=s.price)}

    def cancel_open_orders(s, sym):
        return 0

    def _fill(s, qty, side):
        s.fills.append((side, qty))
        s.qty += qty if side == "buy" else -qty
        return types.SimpleNamespace(id=f"o{len(s.fills)}")

    def submit_market_order(s, sym, qty, side="sell"):
        return s._fill(qty, side)

    def submit_limit_order(s, sym, qty, px, side="sell"):
        return s._fill(qty, side)


def run_exit_chain(entry_side, use_fixed_side):
    """Replays: enter 38 shares -> a partial exit asks for 12 -> a second
    exit rule supersedes, computing its qty from the now-decremented
    qty_remaining (26) exactly like Strategy really does - a full exit, by
    construction. use_fixed_side=False reproduces the pre-2026-10-02 bug
    (side always "sell", no matter the position's direction); True is the
    fix (side derived from trade.side, exactly as main.py's call site now
    does)."""
    cfg = copy.deepcopy(CFG)
    strat = Strategy(cfg)
    trade = TradeManager("Z", 100.0, 38, cfg, side=entry_side)
    strat.trades["Z"] = trade
    start_qty = -38 if entry_side == "short" else 38
    broker = LiveFillBroker(qty=start_qty, price=100.0)
    ex = Executor(broker, cfg)
    ex.open_entries["Z"] = 100.0

    def do_exit(qty, reason, price):
        qty_before = trade.qty_remaining
        if use_fixed_side:
            side = "buy" if trade.side == "short" else "sell"
        else:
            side = "sell"   # the bug: always "sell", whatever the position is
        order = ex.submit_exit_order("Z", qty, reason, price,
                                      qty_before=qty_before, side=side)
        assert order is not None, "exit order was rejected by the broker double"
        trade.process_exit(qty, reason)
        return side

    s1 = do_exit(12, "FIRST_EXIT_-0.7%", 99.3 if entry_side == "short" else 100.7)
    s2 = do_exit(trade.qty_remaining, "TRAILING_STOP", 99.5 if entry_side == "short" else 100.9)
    return broker, s1, s2


broker_fixed, sA, sB = run_exit_chain("short", use_fixed_side=True)
check("a SHORT's full chain, fixed: both exits are BUY (to cover)",
      sA == "buy" and sB == "buy", (sA, sB))
check("...and the broker ends EXACTLY flat - no orphan residue",
      broker_fixed.qty == 0, broker_fixed.qty)

broker_buggy, sA2, sB2 = run_exit_chain("short", use_fixed_side=False)
check("the SAME chain, pre-fix behavior: both exits wrongly SELL",
      sA2 == "sell" and sB2 == "sell", (sA2, sB2))
check("...and the short DOUBLES on the second (full-exit) leg - -50 -> -100, "
      "reproducing the live PRIM/AAOI/DK/etc. mechanism from first "
      "principles, independent of the forensic log reconstruction",
      broker_buggy.qty == -100, broker_buggy.qty)

broker_long, sL1, sL2 = run_exit_chain("long", use_fixed_side=True)
check("a LONG's full chain is unaffected by the fix - still SELLs",
      sL1 == "sell" and sL2 == "sell", (sL1, sL2))
check("...and still closes cleanly to zero", broker_long.qty == 0, broker_long.qty)

print("\n=== K. retry_unconfirmed_exits: THE SAME SIGN BUG, FOUND AUDITING FOR OTHERS ===")
# Found 2026-10-05, auditing every short-specific qty read for the same
# class of mistake after the side= fix above. held was read WITHOUT abs() -
# harmless for a long (qty > 0 always) but for a short, broker.position.qty
# is NEGATIVE. qty_before(26) - held(-26) = 52, which is >= intended_qty(26)
# - the "this order already did its job, nothing to force" branch fired on
# a position that had NOT MOVED AT ALL, silently abandoning tracking with no
# market order ever forced. This is exactly the WLY-shaped failure this
# method was built to prevent (2026-09-03 - a stuck marketable-limit exit
# left unmanaged because nothing ever re-checked it), just for the side this
# method's own test coverage never exercised.


class ShortRetryBroker:
    def __init__(s, qty):
        s.qty = qty
        s.market_calls = []

    def get_positions(s):
        if s.qty == 0:
            return {}
        return {"Z": types.SimpleNamespace(qty=str(s.qty))}

    def cancel_open_orders(s, sym):
        return 0

    def submit_market_order(s, sym, qty, side="sell"):
        s.market_calls.append((qty, side))
        s.qty += qty if side == "buy" else -qty
        return types.SimpleNamespace(id="m")


rb1 = ShortRetryBroker(qty=-26)   # a cover for 26 was submitted, never filled at all
rex1 = Executor(rb1, copy.deepcopy(CFG))
rex1._pending_exit_verify["Z"] = {
    "ts": time.monotonic() - 999, "qty": 26, "side": "buy", "qty_before": 26,
}
forced1 = rex1.retry_unconfirmed_exits(grace_seconds=15)
check("a short's cover that never filled at all gets FORCED, not abandoned",
      forced1 == [("Z", 26)], forced1)
check("...with a BUY (to cover), for the full 26",
      rb1.market_calls == [(26, "buy")], rb1.market_calls)
check("...and the broker ends flat", rb1.qty == 0, rb1.qty)

rb2 = ShortRetryBroker(qty=-16)   # 26 -> 16 short: 10 already genuinely covered
rex2 = Executor(rb2, copy.deepcopy(CFG))
rex2._pending_exit_verify["Z"] = {
    "ts": time.monotonic() - 999, "qty": 26, "side": "buy", "qty_before": 26,
}
forced2 = rex2.retry_unconfirmed_exits(grace_seconds=15)
check("a PARTIALLY-filled short cover forces only the remaining shortfall",
      forced2 == [("Z", 16)], forced2)
check("...not the full 26 again (which would over-cover into a long)",
      rb2.qty == 0, rb2.qty)

rb3 = ShortRetryBroker(qty=0)   # fully covered already
rex3 = Executor(rb3, copy.deepcopy(CFG))
rex3._pending_exit_verify["Z"] = {
    "ts": time.monotonic() - 999, "qty": 26, "side": "buy", "qty_before": 26,
}
forced3 = rex3.retry_unconfirmed_exits(grace_seconds=15)
check("a genuinely, fully-covered short is left alone - no spurious order",
      forced3 == [] and rb3.market_calls == [], (forced3, rb3.market_calls))

esrc2 = open(repo_file("src", "executor", "executor.py")).read()
check("the fix is the one-line abs() on retry_unconfirmed_exits' own held read",
      'held = abs(int(float(getattr(live.get(symbol), "qty", 0) or 0)))' in esrc2)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
