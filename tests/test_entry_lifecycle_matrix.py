"""
COMPREHENSIVE ENTRY-LIFECYCLE MATRIX.

2026-09-14's explicit directive, after the third week-in-a-row order-
lifecycle bug: stop testing one incident's exact shape and start testing
the whole boundary. Every incident so far (2026-09-09 BE/ASTS/COHR/HUT,
2026-09-11 HPE, 2026-09-14 FOUR/NBIS/VRT/AAOI/CIEN/AXTI) was the SAME
invariant breaking in a different timing shape:

    THE BROKER NEVER HOLDS REAL SHARES FOR A SYMBOL THE BOT ISN'T TRACKING.

This file drives many symbols through many realistic fill-timing shapes -
immediate fills, fills mid-grace, fills only after a forced retry, fills at
the exact instant the bot tries to cancel and walk away, symbols that never
fill at all, and a cancel call failing at the worst possible moment - all
through a REAL sequence of polls with controlled elapsed time, and checks
that one invariant after every single poll rather than only at the end.
"""
import copy
import time
from _repo import REPO, CONFIG, repo_file
from src.executor.executor import Executor

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


CFG = {"trading": {"max_concurrent_positions": 50, "max_total_exposure_fraction": 0.9,
                   "max_daily_loss_usd": 100000, "use_take_profit": False}}


class Order:
    id = "o1"


class Pos:
    def __init__(self, sym, qty):
        self.symbol = sym
        self.qty = str(qty)


class MatrixBroker:
    """
    A broker whose fills are driven explicitly by the test, not by internal
    timers - the test controls exactly when each symbol's shares land,
    including the race window that actually bit production: `fill_on_cancel`
    lands shares the INSTANT cancel_open_orders is called for that symbol -
    the 2026-09-14 FOUR/NBIS/VRT/AAOI/CIEN/AXTI shape. Everything else is
    driven by ordinary mutation of `.holdings` between poll calls.
    """
    def __init__(self):
        self.holdings = {}
        self.cancelled = []
        self.limit_calls = []
        self.market_calls = []
        self.fail_cancel_for = set()
        self.fill_on_cancel = {}   # symbol -> qty
        self._cancel_calls = {}   # symbol -> count, so fill_on_cancel can
        # target the SECOND cancel (the give-up one) rather than the first
        # (the pre-retry one) - see cancel_open_orders below.

    def get_positions(self):
        return {s: Pos(s, q) for s, q in self.holdings.items() if q}

    def cancel_open_orders(self, symbol):
        if symbol in self.fail_cancel_for:
            raise Exception(f"simulated cancel failure for {symbol}")
        self.cancelled.append(symbol)
        self._cancel_calls[symbol] = self._cancel_calls.get(symbol, 0) + 1
        # retry_unfilled_entries calls cancel_open_orders TWICE for a symbol
        # that needs a forced retry and then still doesn't fill: once before
        # submitting the retry (line ~940), once more when giving up on it
        # (line ~870). The 2026-09-14 incident shape is specifically the fill
        # landing during the SECOND (give-up) cancel - firing on the first
        # would test a different, already-handled race instead.
        qty = None
        if symbol in self.fill_on_cancel and self._cancel_calls[symbol] >= 2:
            qty = self.fill_on_cancel.pop(symbol)
        if qty:
            self.holdings[symbol] = self.holdings.get(symbol, 0) + qty
        return 1

    def get_latest_quote(self, symbol):
        return None   # forces the plain market fallback on the forced retry

    def submit_limit_order(self, symbol, qty, limit_price, side="buy"):
        self.limit_calls.append((symbol, qty, limit_price, side))
        return Order()

    def submit_market_order(self, symbol, qty, side="buy"):
        self.market_calls.append((symbol, qty, side))
        return Order()


def invariant_ok(broker, executor, symbols):
    """THE invariant: broker holds shares for a symbol => it's tracked."""
    problems = []
    for s in symbols:
        held = broker.holdings.get(s, 0)
        if held > 0 and s not in executor._open_symbols:
            problems.append((s, held))
    return problems


# --------------------------------------------------------------------------
# One shared executor and broker, six symbols, six different fill shapes.
# --------------------------------------------------------------------------
b = MatrixBroker()
e = Executor(b, copy.deepcopy(CFG))

SYMBOLS = ["IMMEDIATE", "GRACE_FILL", "RETRY_THEN_FILLS", "FILLS_ON_CANCEL",
           "TRUE_PHANTOM", "CANCEL_FAILS_BUT_RESOLVED"]
QTY = {"IMMEDIATE": 10, "GRACE_FILL": 8, "RETRY_THEN_FILLS": 6,
       "FILLS_ON_CANCEL": 7, "TRUE_PHANTOM": 5, "CANCEL_FAILS_BUT_RESOLVED": 3}

for s in SYMBOLS:
    e.open_entries[s] = 100.0
    e._open_symbols.add(s)
    e._entry_recorded_at[s] = 0.0
    e._pending_cost[s] = QTY[s] * 100.0
    # Fresh timestamp - "just submitted", not yet past any grace period.
    e._pending_entry_verify[s] = {"ts": time.monotonic(), "qty": QTY[s]}

# IMMEDIATE already filled before anything else runs - the trivial/control case.
b.holdings["IMMEDIATE"] = QTY["IMMEDIATE"]
# FILLS_ON_CANCEL: lands the instant this executor tries to cancel it while
# giving up on its forced retry - the exact FOUR/NBIS/VRT/AAOI/CIEN/AXTI shape.
b.fill_on_cancel["FILLS_ON_CANCEL"] = QTY["FILLS_ON_CANCEL"]
# CANCEL_FAILS_BUT_RESOLVED: the cancel call itself raises, but the shares
# land anyway (independently) - the final re-check must still catch it.
b.fail_cancel_for.add("CANCEL_FAILS_BUT_RESOLVED")

print("=== POLL 1 (t=0s): everything is fresh except IMMEDIATE - nothing "
      "should be force-retried yet ===")
filled1, abandoned1 = e.retry_unfilled_entries(grace_seconds=12)
check("only IMMEDIATE resolves this poll - everything else is still pending",
      "IMMEDIATE" not in e._pending_entry_verify
      and all(s in e._pending_entry_verify for s in SYMBOLS if s != "IMMEDIATE"),
      list(e._pending_entry_verify))
check("no forced retry was attempted for anyone yet - all still within grace",
      b.limit_calls == [] and b.market_calls == [],
      (b.limit_calls, b.market_calls))
check("invariant holds after poll 1", invariant_ok(b, e, SYMBOLS) == [],
      invariant_ok(b, e, SYMBOLS))

print("\n=== Between polls: GRACE_FILL lands (a genuinely-fast fill, no "
      "retry ever needed) ===")
b.holdings["GRACE_FILL"] = QTY["GRACE_FILL"]

print("\n=== POLL 2 (age everyone still pending past the 12s grace): forces "
      "retries for everything still unfilled, and picks up GRACE_FILL "
      "via the ordinary held>0 path first ===")
for s in SYMBOLS:
    if s in e._pending_entry_verify:
        e._pending_entry_verify[s]["ts"] = time.monotonic() - 13
filled2, abandoned2 = e.retry_unfilled_entries(grace_seconds=12)
check("GRACE_FILL resolved via the ordinary held>0 path, no forced retry "
      "was ever submitted for it",
      "GRACE_FILL" not in e._pending_entry_verify
      and not any(c[0] == "GRACE_FILL" for c in b.market_calls + b.limit_calls),
      (b.market_calls, b.limit_calls))
check("RETRY_THEN_FILLS, FILLS_ON_CANCEL, TRUE_PHANTOM and "
      "CANCEL_FAILS_BUT_RESOLVED all got a forced retry submitted",
      all(any(c[0] == s for c in b.market_calls + b.limit_calls)
          for s in ("RETRY_THEN_FILLS", "FILLS_ON_CANCEL", "TRUE_PHANTOM",
                    "CANCEL_FAILS_BUT_RESOLVED")),
      (b.market_calls, b.limit_calls))
check("all four are RE-ARMED (retried=True), not abandoned or trusted yet",
      all(e._pending_entry_verify.get(s, {}).get("retried") is True
          for s in ("RETRY_THEN_FILLS", "FILLS_ON_CANCEL", "TRUE_PHANTOM",
                    "CANCEL_FAILS_BUT_RESOLVED")),
      {s: e._pending_entry_verify.get(s) for s in
       ("RETRY_THEN_FILLS", "FILLS_ON_CANCEL", "TRUE_PHANTOM", "CANCEL_FAILS_BUT_RESOLVED")})
check("invariant holds after poll 2", invariant_ok(b, e, SYMBOLS) == [],
      invariant_ok(b, e, SYMBOLS))

print("\n=== Between polls: RETRY_THEN_FILLS lands normally (the forced "
      "retry worked as designed) ===")
b.holdings["RETRY_THEN_FILLS"] = QTY["RETRY_THEN_FILLS"]

print("\n=== POLL 3 (no time manipulation - everyone re-armed in poll 2 is "
      "still fresh): RETRY_THEN_FILLS confirms immediately via held>0, the "
      "others are still inside their own fresh grace period ===")
filled3, abandoned3 = e.retry_unfilled_entries(grace_seconds=12)
check("RETRY_THEN_FILLS resolved as soon as it was actually held, well "
      "before its retry's own grace period even expired",
      "RETRY_THEN_FILLS" not in e._pending_entry_verify)
check("FILLS_ON_CANCEL, TRUE_PHANTOM, CANCEL_FAILS_BUT_RESOLVED untouched "
      "still - not yet past their OWN grace window",
      all(s in e._pending_entry_verify for s in
          ("FILLS_ON_CANCEL", "TRUE_PHANTOM", "CANCEL_FAILS_BUT_RESOLVED")))
check("invariant holds after poll 3", invariant_ok(b, e, SYMBOLS) == [],
      invariant_ok(b, e, SYMBOLS))

print("\n=== POLL 4 (age the retry's own grace past expiry): the moment of "
      "truth for FILLS_ON_CANCEL, TRUE_PHANTOM and CANCEL_FAILS_BUT_RESOLVED ===")
# CANCEL_FAILS_BUT_RESOLVED fills independently of the (failing) cancel -
# proves the final re-check catches a real fill even when cancel itself blew up.
b.holdings["CANCEL_FAILS_BUT_RESOLVED"] = QTY["CANCEL_FAILS_BUT_RESOLVED"]
for s in ("FILLS_ON_CANCEL", "TRUE_PHANTOM", "CANCEL_FAILS_BUT_RESOLVED"):
    e._pending_entry_verify[s]["ts"] = time.monotonic() - 13
filled4, abandoned4 = e.retry_unfilled_entries(grace_seconds=12)
check("FILLS_ON_CANCEL (2026-09-14 shape) is NOT abandoned - the fill "
      "that landed exactly during cancel_open_orders was caught",
      "FILLS_ON_CANCEL" not in abandoned4, abandoned4)
check("...and it is still tracked as a real position",
      "FILLS_ON_CANCEL" in e._open_symbols)
check("CANCEL_FAILS_BUT_RESOLVED is ALSO not abandoned - the cancel call "
      "itself failed, but the final re-check still found the real fill",
      "CANCEL_FAILS_BUT_RESOLVED" not in abandoned4, abandoned4)
check("...and it is still tracked too",
      "CANCEL_FAILS_BUT_RESOLVED" in e._open_symbols)
check("TRUE_PHANTOM IS abandoned - it genuinely never filled, this is the "
      "correct outcome for a real phantom",
      "TRUE_PHANTOM" in abandoned4, abandoned4)
check("...and cleanly removed from tracking",
      "TRUE_PHANTOM" not in e._open_symbols)
check("invariant holds after poll 4 - the one that matters most",
      invariant_ok(b, e, SYMBOLS) == [], invariant_ok(b, e, SYMBOLS))

print("\n=== FINAL STATE: every symbol accounted for correctly ===")
check("IMMEDIATE: real position, tracked", "IMMEDIATE" in e._open_symbols)
check("GRACE_FILL: real position, tracked", "GRACE_FILL" in e._open_symbols)
check("RETRY_THEN_FILLS: real position, tracked", "RETRY_THEN_FILLS" in e._open_symbols)
check("FILLS_ON_CANCEL: real position, tracked", "FILLS_ON_CANCEL" in e._open_symbols)
check("CANCEL_FAILS_BUT_RESOLVED: real position, tracked",
      "CANCEL_FAILS_BUT_RESOLVED" in e._open_symbols)
check("TRUE_PHANTOM: correctly abandoned, NOT tracked",
      "TRUE_PHANTOM" not in e._open_symbols)
check("TRUE_PHANTOM: broker genuinely holds nothing for it",
      b.holdings.get("TRUE_PHANTOM", 0) == 0)
check("nothing left dangling in _pending_entry_verify",
      e._pending_entry_verify == {}, e._pending_entry_verify)
check("FINAL invariant check across every symbol, every dollar amount held",
      invariant_ok(b, e, SYMBOLS) == [], invariant_ok(b, e, SYMBOLS))

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
