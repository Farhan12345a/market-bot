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

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
