"""
The 2026-09-01 bug: submit_entry_order records a position the instant its
order is SUBMITTED, not once it FILLS. When an exit check fired against a
symbol whose entry was still "working, 0/N filled" (or had somehow already
been fully closed), submit_exit_order cancelled the working entry and sold
the full tracked qty against a position that never actually existed - a
short, rejected by no_shorting, retried every poll for the rest of the
session because nothing ever asked whether there was still something to
sell. NOW, PLTR, MSTR, RGTI, SOXL all hit this the same day.

Covers the fix in three pieces: the phantom guard in submit_exit_order (0
shares held -> PHANTOM_EXIT, no order submitted, bookkeeping cleaned up),
the qty-correction path (fewer shares held than tracked -> sell what is
actually there, and the caller reads the correction back), and
Strategy.drop_phantom (removes a phantom without running confirm_exit's P&L
math, which assumes a real fill happened).
"""
import copy
import time
from _repo import REPO, CONFIG, repo_file
from src.executor.executor import Executor, PHANTOM_EXIT
from src.strategy.strategy import Strategy, TradeManager

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


CFG = {"trading": {"max_concurrent_positions": 10, "max_total_exposure_fraction": 0.9,
                   "max_daily_loss_usd": 100000, "use_take_profit": False}}


class Pos:
    def __init__(self, sym, qty, px=100.0):
        self.symbol = sym
        self.qty = str(qty)
        self.market_value = str(qty * px)
        self.avg_entry_price = str(px)
        self.current_price = str(px)


class Order:
    id = "o1"
    qty = "0"


class Broker:
    """A broker whose real holdings can differ from what the executor
    tracks - modelling an entry that never filled (0) or partially filled
    (less than requested), independent of what submit_market_order is asked
    to sell."""
    def __init__(self, holdings=None):
        self.holdings = dict(holdings or {})   # symbol -> actual qty held
        self.sell_calls = []
        self.cancelled = []

    def get_positions(self):
        return {s: Pos(s, q) for s, q in self.holdings.items() if q}

    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return 1

    def submit_market_order(self, symbol, qty, side="buy"):
        self.sell_calls.append((symbol, qty, side))
        if side == "sell":
            self.holdings[symbol] = max(0, self.holdings.get(symbol, 0) - qty)
        elif side == "buy":
            self.holdings[symbol] = self.holdings.get(symbol, 0) + qty
        return Order()


def mk_executor(broker, symbol, tracked_qty, price=100.0):
    e = Executor(broker, copy.deepcopy(CFG))
    e.open_entries[symbol] = price
    e._open_symbols.add(symbol)
    e._entry_recorded_at[symbol] = 0.0
    e._pending_cost[symbol] = tracked_qty * price
    return e


print("=== 1. BROKER HOLDS ZERO -> PHANTOM_EXIT, NOTHING SOLD ===")
b = Broker(holdings={})   # entry never filled
e = mk_executor(b, "NOW", tracked_qty=28)
result = e.submit_exit_order("NOW", 28, "FIRST_EXIT_-0.5%", price=99.5)
check("returns the PHANTOM_EXIT sentinel, not an order or None",
      result is PHANTOM_EXIT, result)
check("no sell order was ever submitted", b.sell_calls == [], b.sell_calls)
check("the working entry order is still cancelled first",
      "NOW" in b.cancelled, b.cancelled)
check("the phantom is dropped from _open_symbols", "NOW" not in e._open_symbols)
check("...and from open_entries", "NOW" not in e.open_entries)
check("...and from _entry_recorded_at", "NOW" not in e._entry_recorded_at)
check("...and from _pending_cost", "NOW" not in e._pending_cost)

print("\n=== 2. BROKER HOLDS FEWER THAN TRACKED -> SELLS WHAT EXISTS ===")
b2 = Broker(holdings={"CRM": 15})   # tracked 16, broker only has 15
e2 = mk_executor(b2, "CRM", tracked_qty=16)
result2 = e2.submit_exit_order("CRM", 16, "FIRST_EXIT_-0.5%", price=257.83)
check("a real order is returned, not a sentinel", result2 is not None and result2 is not PHANTOM_EXIT)
check("the sell was submitted for the ACTUAL 15, not the requested 16",
      b2.sell_calls == [("CRM", 15, "sell")], b2.sell_calls)
check("the correction is readable via exit_qty_actually_submitted",
      e2.exit_qty_actually_submitted("CRM", default=16) == 15)
check("...and it is a ONE-TIME read - a second call falls back to default",
      e2.exit_qty_actually_submitted("CRM", default=16) == 16)

print("\n=== 3. BROKER HOLDS EXACTLY WHAT IS TRACKED -> NO CORRECTION ===")
b3 = Broker(holdings={"DKS": 20})
e3 = mk_executor(b3, "DKS", tracked_qty=20)
result3 = e3.submit_exit_order("DKS", 20, "TAKE_PROFIT_1%", price=135.0)
check("sells exactly the tracked qty", b3.sell_calls == [("DKS", 20, "sell")], b3.sell_calls)
check("exit_qty_actually_submitted falls back to default (no correction happened)",
      e3.exit_qty_actually_submitted("DKS", default=20) == 20)

print("\n=== 4. get_positions() FAILING NEVER BLOCKS A REAL EXIT ===")
class BrokenPositionsBroker(Broker):
    def get_positions(self):
        raise ConnectionError("simulated API failure")

b4 = BrokenPositionsBroker(holdings={"AAPL": 10})
e4 = mk_executor(b4, "AAPL", tracked_qty=10)
result4 = e4.submit_exit_order("AAPL", 10, "TRAILING_STOP", price=190.0)
check("still submits the exit using the tracked qty when verification fails",
      result4 is not None and result4 is not PHANTOM_EXIT)
check("used the originally tracked qty, unverified", b4.sell_calls == [("AAPL", 10, "sell")], b4.sell_calls)

print("\n=== 5. Strategy.drop_phantom REMOVES WITHOUT P&L MATH ===")
st = Strategy(copy.deepcopy(CFG))
st.trades["NOW"] = TradeManager("NOW", 10.50, 28, copy.deepcopy(CFG))
check("tracked before the drop", "NOW" in st.trades)
st.drop_phantom("NOW")
check("gone after the drop", "NOW" not in st.trades)
check("dropping an untracked symbol is a silent no-op, not an error",
      st.drop_phantom("GHOST") is None)

print("\n=== 6. main.py IS ACTUALLY WIRED TO ALL OF THIS ===")
src = open(repo_file("src", "main.py")).read()
check("PHANTOM_EXIT is imported",
      "from src.executor.executor import Executor, PHANTOM_EXIT" in src)
check("the exit call site checks for it before the generic success branch",
      "if order is PHANTOM_EXIT:" in src)
check("a phantom calls drop_phantom, not confirm_exit",
      "strategy.drop_phantom(symbol)" in src)
check("a real exit reads the corrected qty rather than trusting exit_info blindly",
      "executor.exit_qty_actually_submitted(symbol, exit_info[\"qty\"])" in src)

print("\n=== 7. retry_unconfirmed_exits CANCELS THE STUCK ORDER FIRST (2026-09-04, TSLA) ===")
# The real Alpaca failure this reproduces: a marketable-limit exit's order is
# STILL working at the broker, reserving shares ("held_for_orders"). A fresh
# market order for the FULL qty is rejected - "insufficient qty available" -
# until that working order is cancelled. submit_exit_order already cancels
# first for exactly this reason; retry_unconfirmed_exits bypasses
# submit_exit_order and, before this fix, never did - it retried forever
# against the same rejection because nothing ever cleared the stuck order.
class HeldForOrdersBroker(Broker):
    def __init__(self, holdings=None):
        super().__init__(holdings)
        self.cancelled_before_submit = False

    def cancel_open_orders(self, symbol):
        self.cancelled_before_submit = True
        return super().cancel_open_orders(symbol)

    def submit_market_order(self, symbol, qty, side="buy"):
        if not self.cancelled_before_submit:
            raise Exception(
                '{"code":40310000,"message":"insufficient qty available for '
                'order (requested: 3, available: 1)"}')
        return super().submit_market_order(symbol, qty, side=side)

b7 = HeldForOrdersBroker(holdings={"TSLA": 3})
e7 = mk_executor(b7, "TSLA", tracked_qty=3)
e7._pending_exit_verify["TSLA"] = {"ts": time.monotonic() - 999, "qty": 3, "side": "sell"}
forced = e7.retry_unconfirmed_exits(grace_seconds=15)
check("the stuck order is cancelled before the forced market exit is submitted",
      b7.cancelled_before_submit)
check("the forced exit succeeds once the reservation is cleared",
      forced == [("TSLA", 3)], forced)
check("TSLA is cleared from pending verification", "TSLA" not in e7._pending_exit_verify)
check("the sell was actually submitted for the full 3 shares",
      b7.sell_calls == [("TSLA", 3, "sell")], b7.sell_calls)

print("\n=== 8. retry_unconfirmed_exits BACKS OFF AFTER A FAILURE, NOT EVERY POLL ===")
class AlwaysRejectsBroker(Broker):
    def submit_market_order(self, symbol, qty, side="buy"):
        raise Exception("simulated persistent rejection")

b8 = AlwaysRejectsBroker(holdings={"XYZ": 5})
e8 = mk_executor(b8, "XYZ", tracked_qty=5)
e8._pending_exit_verify["XYZ"] = {"ts": time.monotonic() - 999, "qty": 5, "side": "sell"}
e8.retry_unconfirmed_exits(grace_seconds=15)
first_retry_ts = e8._pending_exit_verify["XYZ"]["ts"]
check("still pending after a failed attempt", "XYZ" in e8._pending_exit_verify)
check("immediately retrying again does nothing - still inside the cooldown",
      e8.retry_unconfirmed_exits(grace_seconds=15) == [])
check("the retry timestamp did not reset to now - a real cooldown is enforced",
      first_retry_ts < time.monotonic() - 1, first_retry_ts)

print("\n=== 9. retry_unfilled_entries FORCES A MARKET BUY (2026-09-08 opening burst) ===")
# The real incident: the opening burst took 9 entries, only 1 (ORCL) filled.
# The other 8 sat as unfilled marketable-limit buys - 0 shares held - and
# were only ever discovered, and dropped, by the EXIT side's phantom guard
# minutes later. This is the entry-side fix: verify the fill, force ONE
# market attempt if it never crossed.
b9 = Broker(holdings={})   # nothing filled yet
e9 = mk_executor(b9, "NBIS", tracked_qty=5)
e9._pending_entry_verify["NBIS"] = {"ts": time.monotonic() - 999, "qty": 5}
filled9, abandoned9 = e9.retry_unfilled_entries(grace_seconds=15)
check("the stuck entry order is cancelled before the forced buy",
      "NBIS" in b9.cancelled, b9.cancelled)
check("a market BUY is submitted for the full tracked qty",
      b9.sell_calls == [("NBIS", 5, "buy")], b9.sell_calls)
check("reported as filled, not abandoned",
      filled9 == [("NBIS", 5)] and abandoned9 == [], (filled9, abandoned9))
check("NBIS is cleared from pending verification", "NBIS" not in e9._pending_entry_verify)
check("executor-side tracking is left INTACT for a successful forced fill "
      "(no phantom cleanup needed)", "NBIS" in e9._open_symbols)

print("\n=== 10. retry_unfilled_entries GIVES UP AFTER ONE FAILED ATTEMPT ===")
# Deliberately NOT the same policy as the exit side, which retries forever
# with a cooldown - an exit must eventually complete, an unfilled entry has
# no such obligation. One forced try, then drop the phantom.
b10 = AlwaysRejectsBroker(holdings={})
e10 = mk_executor(b10, "AXTI", tracked_qty=10)
e10._pending_entry_verify["AXTI"] = {"ts": time.monotonic() - 999, "qty": 10}
filled10, abandoned10 = e10.retry_unfilled_entries(grace_seconds=15)
check("reported as abandoned, not filled",
      filled10 == [] and abandoned10 == ["AXTI"], (filled10, abandoned10))
check("AXTI is cleared from pending verification", "AXTI" not in e10._pending_entry_verify)
check("executor-side bookkeeping is cleaned up like any other phantom",
      "AXTI" not in e10._open_symbols and "AXTI" not in e10.open_entries
      and "AXTI" not in e10._entry_recorded_at and "AXTI" not in e10._pending_cost)
check("does NOT retry again on the very next call - it already gave up",
      e10.retry_unfilled_entries(grace_seconds=15) == ([], []))

print("\n=== 11. A PARTIAL OR FULL FILL NEEDS NO FORCED RETRY AT ALL ===")
b11 = Broker(holdings={"IONQ": 3})   # tracked 5, broker already shows 3
e11 = mk_executor(b11, "IONQ", tracked_qty=5)
e11._pending_entry_verify["IONQ"] = {"ts": time.monotonic() - 999, "qty": 5}
filled11, abandoned11 = e11.retry_unfilled_entries(grace_seconds=15)
check("a partial fill is left alone - the exit side's own qty correction "
      "handles selling only what is actually held",
      filled11 == [] and abandoned11 == [], (filled11, abandoned11))
check("no forced order was ever submitted", b11.sell_calls == [], b11.sell_calls)
check("cleared from pending anyway - it is resolved, just not by force",
      "IONQ" not in e11._pending_entry_verify)

print("\n=== 12. STILL INSIDE THE GRACE PERIOD -> LEFT ALONE ===")
b12 = Broker(holdings={})
e12 = mk_executor(b12, "MRVL", tracked_qty=8)
e12._pending_entry_verify["MRVL"] = {"ts": time.monotonic(), "qty": 8}  # just now
filled12, abandoned12 = e12.retry_unfilled_entries(grace_seconds=15)
check("nothing forced yet - still well inside the grace window",
      filled12 == [] and abandoned12 == [], (filled12, abandoned12))
check("still pending for the next poll", "MRVL" in e12._pending_entry_verify)
check("no order submitted prematurely", b12.sell_calls == [], b12.sell_calls)

print("\n=== 13. main.py IS WIRED TO THE ENTRY-SIDE SAFETY NET TOO ===")
check("retry_unfilled_entries is actually called from the poll loop",
      "executor.retry_unfilled_entries()" in src)
check("an abandoned entry is dropped from Strategy, not left half-tracked",
      "strategy.drop_phantom(_sym)" in src)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
