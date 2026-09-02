"""
The two SIGN bugs, found 2026-08-28 and unfixed until 2026-09-02.

1. Executor.flatten_all_positions threw away the sign and called
   submit_exit_order, which was hardcoded to side="sell". On a long that
   closes the position; on a SHORT it doubles it. The 16:00 time stop runs
   through that path, so any short held at 16:00 got bigger instead of
   closing.

2. main.reconcile_existing_positions did int(abs(float(position.qty))), so a
   short was adopted as a long of the same size and every downstream rule ran
   backwards on it. On 2026-08-28 CRWD at -39 @ 212.74 against a ~228 market
   read as +7.2% PROFIT and would have fired a take-profit SELL - which
   shorts 39 more, and (because submit_exit_order cancels working orders for
   the symbol first) would also have cancelled the buy-to-cover queued
   against it.

Both cost ~$1,015 between them. no_shorting on the account has been the only
thing holding them harmless since; that is a backstop, not a fix.
"""
import copy
import types
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.executor.executor import Executor, PHANTOM_EXIT
from src.strategy.strategy import Strategy

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


CFG = {"trading": {"max_concurrent_positions": 10, "max_total_exposure_fraction": 0.9,
                   "max_daily_loss_usd": 100000, "use_take_profit": False}}


class Pos:
    def __init__(self, sym, qty, avg=100.0, cur=100.0):
        self.symbol = sym
        self.qty = str(qty)                 # may be NEGATIVE - that's the point
        self.avg_entry_price = str(avg)
        self.current_price = str(cur)
        self.market_value = str(float(qty) * cur)
        self.unrealized_pl = "0"


class Order:
    id = "o1"


class Broker:
    def __init__(self, positions=None):
        self.positions = dict(positions or {})
        self.orders = []
        self.cancelled = []

    def get_positions(self):
        return dict(self.positions)

    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return 0

    def submit_market_order(self, symbol, qty, side="buy"):
        self.orders.append((symbol, qty, side))
        return Order()

    def get_account(self):
        return types.SimpleNamespace(cash="90000", equity="90000", buying_power="90000")

    def get_filled_sell_orders_since(self, symbol, since):
        return []


print("=== 1. FLATTEN A SHORT: BUYS TO COVER, DOES NOT SELL DEEPER ===")
b = Broker({"CRWD": Pos("CRWD", -39, avg=212.74, cur=228.0)})
ex = Executor(b, copy.deepcopy(CFG))
flat = ex.flatten_all_positions()
check("an order was submitted for the short", len(b.orders) == 1, b.orders)
check("the side is BUY, not sell - this is the whole bug",
      b.orders and b.orders[0][2] == "buy", b.orders)
check("the quantity is the POSITIVE magnitude", b.orders and b.orders[0][1] == 39, b.orders)
check("it reports as flattened", flat == ["CRWD"], flat)

print("\n=== 2. FLATTEN A LONG: UNCHANGED BEHAVIOUR ===")
b2 = Broker({"HOOD": Pos("HOOD", 79, avg=100.0, cur=101.0)})
ex2 = Executor(b2, copy.deepcopy(CFG))
flat2 = ex2.flatten_all_positions()
check("a long still sells", b2.orders == [("HOOD", 79, "sell")], b2.orders)
check("and still reports flattened", flat2 == ["HOOD"], flat2)

print("\n=== 3. MIXED BOOK: EACH POSITION GETS ITS OWN SIDE ===")
b3 = Broker({"LONG1": Pos("LONG1", 10), "SHORT1": Pos("SHORT1", -5)})
ex3 = Executor(b3, copy.deepcopy(CFG))
ex3.flatten_all_positions()
sides = {sym: side for sym, _, side in b3.orders}
check("the long sells and the short covers, in the same sweep",
      sides == {"LONG1": "sell", "SHORT1": "buy"}, b3.orders)

print("\n=== 4. P&L IS NOT INVERTED ON A COVER ===")
# Short 10 @ 100, bought back at 90 -> the short MADE $100. Recorded as a
# loss, it would corrupt the daily report, trade_history.csv, session-metrics
# and the daily-loss limit's own accounting.
b4 = Broker({"SH": Pos("SH", -10, avg=100.0, cur=90.0)})
ex4 = Executor(b4, copy.deepcopy(CFG))
ex4.open_entries["SH"] = 100.0
ex4.submit_exit_order("SH", 10, "FLATTEN_ALL", price=90.0, side="buy")
rec = ex4.trades_log[-1]
check("covering below the short's entry is a PROFIT", rec["pl"] == 100.0, rec["pl"])
check("...and the percentage agrees", round(rec["pl_pct"], 6) == 10.0, rec["pl_pct"])

b5 = Broker({"SH": Pos("SH", -10, avg=100.0, cur=110.0)})
ex5 = Executor(b5, copy.deepcopy(CFG))
ex5.open_entries["SH"] = 100.0
ex5.submit_exit_order("SH", 10, "FLATTEN_ALL", price=110.0, side="buy")
rec5 = ex5.trades_log[-1]
check("covering above the short's entry is a LOSS", rec5["pl"] == -100.0, rec5["pl"])

print("\n=== 5. A LONG EXIT'S P&L IS UNTOUCHED BY THE NEW PARAMETER ===")
b6 = Broker({"L": Pos("L", 10, avg=100.0, cur=110.0)})
ex6 = Executor(b6, copy.deepcopy(CFG))
ex6.open_entries["L"] = 100.0
ex6.submit_exit_order("L", 10, "TAKE_PROFIT_1%", price=110.0)   # default side="sell"
rec6 = ex6.trades_log[-1]
check("selling a long above entry is still a profit", rec6["pl"] == 100.0, rec6["pl"])
check("default side is still sell", b6.orders[-1][2] == "sell", b6.orders)

print("\n=== 6. BUYING POWER MOVES THE RIGHT WAY ===")
# Selling a long RETURNS cash; buying to cover SPENDS it. Same magnitude,
# opposite direction - the cache feeds pre_entry_check for any entry checked
# later in the same poll.
b7 = Broker({"L": Pos("L", 10)})
ex7 = Executor(b7, copy.deepcopy(CFG))
ex7._buying_power = 10000.0
ex7.open_entries["L"] = 100.0
ex7.submit_exit_order("L", 10, "FLATTEN_ALL", price=100.0)
check("selling a long adds the proceeds", ex7._buying_power == 11000.0, ex7._buying_power)

b8 = Broker({"S": Pos("S", -10)})
ex8 = Executor(b8, copy.deepcopy(CFG))
ex8._buying_power = 10000.0
ex8.open_entries["S"] = 100.0
ex8.submit_exit_order("S", 10, "FLATTEN_ALL", price=100.0, side="buy")
check("covering a short spends it", ex8._buying_power == 9000.0, ex8._buying_power)

print("\n=== 7. RECONCILE REFUSES TO ADOPT A SHORT ===")


class Strat:
    def __init__(self): self.trades = {}; self.config = copy.deepcopy(CFG)


b9 = Broker({"CRWD": Pos("CRWD", -39, avg=212.74, cur=228.0),
             "NVDA": Pos("NVDA", 12, avg=170.0, cur=172.0)})
ex9 = Executor(b9, copy.deepcopy(CFG))
st9 = Strategy(copy.deepcopy(CFG))
M.reconcile_existing_positions(b9, st9, ex9)
check("the SHORT is not adopted - it would run every long-only rule backwards",
      "CRWD" not in st9.trades, list(st9.trades))
check("the long beside it IS still adopted - refusing one must not skip the rest",
      "NVDA" in st9.trades, list(st9.trades))
check("...with the right positive qty", st9.trades["NVDA"].entry_qty == 12, st9.trades.get("NVDA"))
check("the refused short leaves no half-written executor state either",
      "CRWD" not in ex9.open_entries, ex9.open_entries)

print("\n=== 8. THE OLD abs() IS GONE FROM BOTH SITES ===")
msrc = open(repo_file("src", "main.py")).read()
esrc = open(repo_file("src", "executor", "executor.py")).read()
check("reconcile reads the RAW qty before deciding",
      "raw_qty = float(position.qty)" in msrc)
check("...and refuses on a negative one", "REFUSING to adopt a SHORT" in msrc)
check("flatten derives the side from the sign",
      'side = "buy" if raw_qty < 0 else "sell"' in esrc)
check("submit_exit_order takes a side and no longer hardcodes it",
      'def submit_exit_order' in esrc and 'side="sell"):' in esrc
      and 'submit_market_order(symbol, qty, side=side)' in esrc)

print("\n=== 9. THE PHANTOM GUARD STILL WORKS ON THE SHORT PATH ===")
# A cover for something the broker doesn't hold is just as much a phantom as
# a sell for something it doesn't hold - the guard keys off holdings, not side.
b10 = Broker({})
ex10 = Executor(b10, copy.deepcopy(CFG))
ex10.open_entries["GONE"] = 100.0
r10 = ex10.submit_exit_order("GONE", 10, "FLATTEN_ALL", price=100.0, side="buy")
check("zero holdings -> PHANTOM_EXIT, no cover submitted",
      r10 is PHANTOM_EXIT and b10.orders == [], (r10, b10.orders))

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
