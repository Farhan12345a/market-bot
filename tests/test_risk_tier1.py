"""
Tier-1 risk controls (2026-09-02).

Four guards, each aimed at a failure the account has already seen or is one
bug away from seeing:

  1. PRE-TRADE RATE LIMITS - the runaway-loop guard. Every other limit in this
     codebase bounds the STATE the account ends up in, checked once a poll. A
     loop that submits inside one poll does its damage between two snapshots.
  2. TWO-TIER LOSS RESPONSE - the day was binary (fine, or over) and warnings
     did nothing but log.
  3. SLIPPAGE PERSISTENCE - entry slippage was computed and discarded since
     2026-08-20; exit slippage was never measured.
  4. NO ENTRIES ON STALE DATA - the bot fell back to REST (~15 min delayed) and
     carried on buying.
"""
import copy
import types
import yaml
from _repo import CONFIG, repo_file
import src.main as M
from src.executor.executor import Executor
from src.analytics import trade_recorder as TR

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


class B:
    def __init__(s, held=None): s.held = held or {}
    def get_positions(s):
        return {k: types.SimpleNamespace(symbol=k, qty=str(v), market_value=str(abs(v) * 100),
                                         avg_entry_price="100.0") for k, v in s.held.items()}
    def cancel_open_orders(s, sym): return 0
    def submit_market_order(s, sym, qty, side="sell"):
        return types.SimpleNamespace(id="m", filled_avg_price=None)
    def submit_limit_order(s, sym, qty, px, side="sell", extended_hours=False):
        return types.SimpleNamespace(id="l", filled_avg_price=None)


def ex(cfg=None, equity=100000.0):
    e = Executor(B({}), copy.deepcopy(cfg or CFG))
    e._equity = equity
    e._buying_power = equity
    return e


# ===================================================================
print("=== 1. RATE LIMITS: the runaway-loop guard ===")
rl = CFG["trading"]["rate_limits"]
check("shipped enabled", rl["enabled"] is True)

e = ex()
ok, why = e.rate_limit_check(10, 100.0)
check("a normal order passes", ok is True, why)

# Orders per minute. 2026-09-02 fired 22 entries in seven minutes and nothing
# structural refused any of them.
e = ex()
n = rl["max_orders_per_minute"]
for _ in range(n):
    e._note_order_submitted(10, 100.0)
ok, why = e.rate_limit_check(10, 100.0)
check(f"the {n + 1}th order in the window is REFUSED", ok is False, why)
check("...naming the limit that fired", "max_orders_per_minute" in (why or ""), why)
check("...and saying it is about the rate, not this order",
      "runaway" in (why or ""), why)

# Notional per minute is a separate ceiling: a few huge orders are as
# dangerous as many small ones.
e = ex()
# Fill the minute's notional budget with orders that are each individually
# fine, so the ceiling that fires is the per-MINUTE one and not the per-ORDER
# one. Two orders of $12,000 each are legal alone; a third is not.
per_order = rl["max_notional_per_order"] * 0.8
n_fill = int(rl["max_notional_per_minute"] // per_order)
for _ in range(n_fill):
    e._note_order_submitted(per_order / 100.0, 100.0)
ok, why = e.rate_limit_check(per_order / 100.0, 100.0)
check("a legal-sized order is refused once the MINUTE's notional is spent",
      ok is False, why)
check("...naming that limit rather than the per-order one",
      "max_notional_per_minute" in (why or ""), why)

# Per-order sanity. A bad price or a bad qty produces one absurd order rather
# than many, so the rate window would never see it.
e = ex()
ok, why = e.rate_limit_check(rl["max_shares_per_order"] + 1, 1.0)
check("a single absurd SHARE count is refused", ok is False, why)
check("...naming max_shares_per_order", "max_shares_per_order" in (why or ""), why)
e = ex()
ok, why = e.rate_limit_check(1, rl["max_notional_per_order"] + 1)
check("a single absurd NOTIONAL is refused", ok is False, why)

# The window must actually roll, or one busy minute disables the bot for good.
e = ex()
import time as _t
for _ in range(rl["max_orders_per_minute"]):
    e._order_times.append((_t.monotonic() - (rl["window_seconds"] + 5), 1000.0))
ok, why = e.rate_limit_check(10, 100.0)
check("orders OLDER than the window do not count - it rolls, it does not latch",
      ok is True, why)

off = copy.deepcopy(CFG); off["trading"]["rate_limits"]["enabled"] = False
e = ex(off)
for _ in range(100):
    e._note_order_submitted(10, 100.0)
check("disabled -> inert", e.rate_limit_check(10, 100.0)[0] is True)

print("\n--- wired into the entry path, and NOT into the exit path ---")
e = ex()
for _ in range(rl["max_orders_per_minute"]):
    e._note_order_submitted(10, 100.0)
ok, why = e.pre_entry_check(10, 100.0, symbol="AAA")
check("pre_entry_check refuses once the rate is breached", ok is False, why)
esrc = open(repo_file("src", "executor", "executor.py")).read()
check("...and it is checked FIRST, before the costlier checks",
      esrc.index("ok, why = self.rate_limit_check(qty, price, is_opening_burst=")
      < esrc.index('max_attempts = self.config["trading"].get("max_entry_attempts'))
check("an EXIT is counted but never blocked - an unclosed position is the "
      "larger risk",
      "Exits are never rate-blocked" in esrc)
check("entries are recorded into the window", "self._note_order_submitted(qty, price)" in esrc)

e2 = ex()
e2.open_entries["AAA"] = 100.0
e2.broker = B({"AAA": 100})
for _ in range(rl["max_orders_per_minute"] * 3):
    e2._note_order_submitted(10, 100.0)
res = e2.submit_exit_order("AAA", 100, "FINAL_EXIT_-1.0%", 99.0)
check("...so a heavily rate-limited account can STILL exit", res is not None)

# ===================================================================
print("\n=== 2. TWO-TIER LOSS RESPONSE ===")
lt = CFG["trading"]["loss_tiers"]
check("shipped enabled", lt["enabled"] is True)
check("tiers are fractions, not dollars - so they keep meaning the same thing "
      "as the account grows",
      all(0 < t["at_fraction"] < 1 for t in lt["tiers"]), lt["tiers"])
check("...and they only ever reduce", all(0 < t["size_multiplier"] < 1 for t in lt["tiers"]))
check("...and none of them is 0 - stopping the day is the hard limit's job",
      all(t["size_multiplier"] > 0 for t in lt["tiers"]))


def mult_at(loss_usd, equity=100000.0, cfg=None):
    e = ex(cfg, equity)
    e.daily_pnl = -loss_usd
    return e.loss_tier_multiplier()


limit = ex().daily_loss_limit_usd()
check(f"the computed limit at $100k equity is ${limit:,.0f}", limit == 1000, limit)
check("a green day is untouched", mult_at(-50.0) == 1.0)
check("flat is untouched", mult_at(0.0) == 1.0)
check("a shallow loss is untouched", mult_at(limit * 0.25) == 1.0)
check("at 50% of the limit -> half size", mult_at(limit * 0.5) == 0.5, mult_at(limit * 0.5))
check("at 75% -> quarter size", mult_at(limit * 0.75) == 0.25, mult_at(limit * 0.75))
check("past the limit it is still the deepest tier, never 0",
      mult_at(limit * 2) == 0.25, mult_at(limit * 2))
check("the tiers scale WITH the account: $200k equity, same fractions",
      mult_at(ex(equity=200000.0).daily_loss_limit_usd() * 0.5, equity=200000.0) == 0.5)
off2 = copy.deepcopy(CFG); off2["trading"]["loss_tiers"]["enabled"] = False
check("disabled -> inert", mult_at(limit * 0.9, cfg=off2) == 1.0)

msrc = open(repo_file("src", "main.py")).read()
check("_position_size applies it", "executor.loss_tier_multiplier()" in msrc)
check("...MULTIPLIED with the regime scalar, not replacing it - two "
      "independent reasons to be smaller should compound",
      "regime_mult *= executor.loss_tier_multiplier()" in msrc)

# The composition is the point, so assert the arithmetic rather than trusting it.
e3 = ex()
e3.daily_pnl = -limit * 0.5          # 0.5x from the loss tier
e3.regime_size_multiplier = 0.5      # 0.5x from a choppy regime
full = M._position_size(CFG, ex(), 100.0)
both = M._position_size(CFG, e3, 100.0)
check("a choppy tape AND a half-spent day compound to 0.25x",
      abs(both - full * 0.25) <= 1, (full, both))

# ===================================================================
print("\n=== 3. SLIPPAGE IS PERSISTED, NOT JUST LOGGED ===")
check("entry slippage is kept on entry_meta, not only logged",
      'meta["entry_slippage_pct"]' in esrc)
check("...measured against the ORIGINAL signal price, so repeated "
      "corrections do not compound",
      'meta["signal_price"] = meta.get("signal_price", recorded)' in esrc)
check("exit slippage is computed from the broker's FILL",
      '"filled_avg_price"' in esrc and 'trade_record["exit_slippage_pct"]' in esrc)
check("...signed so NEGATIVE is adverse for either side",
      "direction * (fill_px - price)" in esrc)
check("P&L prefers the fill over the decision price",
      "price_for_pnl = fill_px or price" in esrc)
check("a missing fill leaves slippage BLANK, not zero - an unmeasured cost is "
      "not a zero cost",
      'trade_record["exit_slippage_pct"] = None' in esrc)

for col in ("entry_slippage_pct", "decision_price", "fill_price", "exit_slippage_pct"):
    check(f"trade_history carries {col}", f'"{col}"' in esrc)
    if col != "decision_price":
        check(f"...and so does trade_context", col in TR.CONTEXT_FIELDS)
check("the new columns are APPENDED, never inserted mid-schema",
      TR.CONTEXT_FIELDS[-3:] == ["entry_slippage_pct", "exit_slippage_pct", "fill_price"],
      TR.CONTEXT_FIELDS[-3:])
check("older rows are declared as a legacy schema so they still parse",
      "v3: before the slippage columns" in esrc)

row = TR.build_context_row("AAA", {"entry_time": "2026-09-02T09:31:00"},
                           {"exit_price": 99.0, "entry_slippage_pct": -0.45,
                            "exit_slippage_pct": -0.46, "fill_price": 98.9})
check("a built row carries entry slippage", row.get("entry_slippage_pct") == -0.45, row.get("entry_slippage_pct"))
check("...and exit slippage", row.get("exit_slippage_pct") == -0.46)
row2 = TR.build_context_row("BBB", {}, {"exit_price": 10.0})
check("...and blanks rather than zeroes what was not measured",
      row2.get("entry_slippage_pct") == "" and row2.get("exit_slippage_pct") == "",
      (row2.get("entry_slippage_pct"), row2.get("exit_slippage_pct")))

# ===================================================================
print("\n=== 4. NO ENTRIES ON STALE DATA ===")
check("shipped enabled", CFG["trading"]["require_fresh_data_for_entry"]["enabled"] is True)
check("the entry path checks stream health", "_stream.is_healthy()" in msrc)
check("...and refuses rather than logging and continuing",
      "entry refused - the price stream is not serving" in msrc)
check("...naming the ~15 minute REST delay so the cost is explicit",
      "delays\\nby ~15 minutes" in msrc or "~15 minutes" in msrc)
check("EXITS are deliberately untouched - a stale price is a terrible reason "
      "to hold a position",
      "EXITS ARE DELIBERATELY UNAFFECTED" in msrc)
check("it warns ONCE, not once per symbol per poll",
      '_fresh.get("_warned")' in msrc)
check("the guard sits before the cooldown and sizing work it would waste",
      msrc.index("require_fresh_data_for_entry")
      < msrc.index("cooldown_left = 0 if skip_cooldown"))
check("no stream object at all (tests, REST-only mode) does NOT block entries "
      "- absence of a stream is not evidence of stale data",
      "if _stream is not None and not _healthy:" in msrc)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
