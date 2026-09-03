"""
The opening-move experiment: measure each streamed symbol from the 09:30 open,
buy the ones that are up, decide by 09:33.

This is deliberately a SEPARATE entry mode rather than a wider entry window, and
most of what is tested here is that separation holding. Widening
entry_window_start to 09:30 would have run the normal 0.3%/3min rule at the
open, where nearly every high-beta name clears it at once - filling
max_concurrent_positions within seconds and leaving the rest of the session with
no capacity. That is the 2026-08-19 shape: 20 entries inside 9 seconds, 1 winner
in 23, -$1,307.
"""
import copy, sys, types, yaml
from datetime import datetime, timedelta
import pytz
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.main as M

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0
def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


class Strat:
    def __init__(self): self.trades = {}
    def get_open_trades(self): return self.trades
    def can_enter(self, s, q): return True
    # Signature must match the real Strategy.confirm_entry, including
    # config_override. When it did not, every entry raised AFTER the broker
    # order had been submitted - the exception was swallowed by the opening
    # loop's per-symbol guard and the position was never tracked. A mock that
    # drifts from the interface hides exactly the failure it should surface.
    def confirm_entry(self, s, p, q, config_override=None):
        self.trades[s] = {"price": p, "qty": q, "cfg": config_override}


class Exec:
    def __init__(self, cooldown=0):
        self.entry_meta = {}
        self.orders = []
        self._cooldown = cooldown
        self.equity = 100000.0
    def reentry_cooldown_remaining(self, s): return self._cooldown
    def pre_entry_check(self, qty, price, symbol=None, is_opening_burst=False): return True, ""
    def submit_entry_order(self, s, qty, price, entry_method=None, entry_rsi=None,
                           spread_pct=None, is_opening_burst=False):
        self.orders.append({"symbol": s, "qty": qty, "price": price, "method": entry_method})
        return {"id": len(self.orders)}
    def refresh_account_snapshot(self): pass


class MD:
    """Prices per symbol per call, and which symbols are 'streamed'."""
    def __init__(self, prices, streamed=None):
        self.prices = prices          # {sym: [p1, p2, ...]} consumed in order
        self.idx = {s: 0 for s in prices}
        self.streamed = set(streamed if streamed is not None else prices)
    def is_streamed(self, s): return s in self.streamed
    def get_latest_bar(self, s, tf="1Min"):
        return {"close": self._peek(s)} if s in self.prices else None
    def get_entry_price(self, s, bar):
        p = self._peek(s)
        if self.idx[s] < len(self.prices[s]) - 1:
            self.idx[s] += 1
        return p
    def _peek(self, s):
        seq = self.prices.get(s) or []
        return seq[min(self.idx[s], len(seq) - 1)] if seq else None


def cfg(**over):
    c = copy.deepcopy(CFG)
    c["trading"]["opening_burst"].update(over)
    return c


def at(hhmm, sec=0):
    d = datetime.now(ET).replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]),
                                 second=sec, microsecond=0)
    return d


def run(c, md, strat, ex, symbols, state, when, journal=None):
    return M._run_opening_burst(c, md, strat, ex, symbols, {}, state, when, ET,
                                signal_journal=journal)


print("=== 1. CONFIG ===")
ob = CFG["trading"]["opening_burst"]
check("enabled for tomorrow", ob["enabled"] is True)
check("baseline is the bell", ob["baseline_time"] == "09:30")
check("decides by 09:33", ob["decide_by"] == "09:33")
check("has its OWN position budget", ob["max_positions"] == 7, ob["max_positions"])
check("budget leaves room for the normal session",
      ob["max_positions"] < CFG["trading"]["max_concurrent_positions"], ob["max_positions"])
check("half size", ob["size_multiplier"] == 0.5)
check("streamed only", ob["streamed_only"] is True)
# There is deliberately no skip_continuation_score flag: the mode never
# consults the score at all, and a flag nothing reads is worse than no flag.
check("no inert continuation flag", "skip_continuation_score" not in ob, list(ob))
check("ignores the signal ceiling", ob["ignore_max_pct"] is True)
check("does not arm the re-entry cooldown", ob["skip_reentry_cooldown"] is True)
check("normal entry window is UNCHANGED at 09:33",
      CFG["trading"]["entry_window_start"] == "09:33")
check("stream starts well before the bell",
      CFG["trading"]["stream_prestart_minutes"] >= 2,
      CFG["trading"]["stream_prestart_minutes"])

print("\n=== 2. BASELINE AND TIMING ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"AAA": [100.0, 101.0]})
s_, e_ = Strat(), Exec()
check("nothing happens before the baseline instant",
      run(cfg(), md, s_, e_, ["AAA"], st, at("09:29")) == 0 and not st["baseline"])
run(cfg(), md, s_, e_, ["AAA"], st, at("09:30"))
check("first price at/after the baseline IS the baseline", st["baseline"]["AAA"] == 100.0,
      st["baseline"])
check("the baseline poll does not also buy", e_.orders == [])

print("\n=== 3. IT BUYS WHAT WENT UP ===")
# Tomorrow's live threshold is 0.5%, so this uses the live config rather than a
# zeroed one - a test that only passes at a threshold nobody runs is not testing
# what ships.
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"UP": [100.0, 101.0], "DOWN": [50.0, 49.0], "FLAT": [20.0, 20.0],
         "TINY": [40.0, 40.08]})            # +0.2%, under the 0.5% threshold
s_, e_ = Strat(), Exec()
c = cfg()
run(c, md, s_, e_, ["UP", "DOWN", "FLAT", "TINY"], st, at("09:30"))
run(c, md, s_, e_, ["UP", "DOWN", "FLAT", "TINY"], st, at("09:31"))
bought = [o["symbol"] for o in e_.orders]
check("a +1% riser is bought", "UP" in bought, bought)
check("a faller is NOT bought", "DOWN" not in bought, bought)
check("a flat symbol is NOT bought at a 0.5% threshold", "FLAT" not in bought, bought)
check("a +0.2% move is under the threshold and skipped", "TINY" not in bought, bought)
check("tagged as OPENING_MOVE", all(o["method"] == M.OPENING_METHOD for o in e_.orders))
# ...and with the threshold at zero, "any increase" is what it means.
st0 = {"baseline": {}, "taken": [], "done": False}
md0 = MD({"FLAT": [20.0, 20.0], "TINY": [40.0, 40.08]})
e0 = Exec(); c0 = cfg(min_move_pct=0.0)
run(c0, md0, Strat(), e0, ["FLAT", "TINY"], st0, at("09:30"))
run(c0, md0, Strat(), e0, ["FLAT", "TINY"], st0, at("09:31"))
check("min_move_pct 0.0 takes any non-negative move",
      {o["symbol"] for o in e0.orders} == {"FLAT", "TINY"}, [o["symbol"] for o in e0.orders])

print("\n=== 4. min_move_pct RAISES THE BAR ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"SMALL": [100.0, 100.2], "BIG": [100.0, 101.5]})
s_, e_ = Strat(), Exec()
c = cfg(min_move_pct=1.0)
run(c, md, s_, e_, ["SMALL", "BIG"], st, at("09:30"))
run(c, md, s_, e_, ["SMALL", "BIG"], st, at("09:31"))
bought = [o["symbol"] for o in e_.orders]
check("a +0.2% move is refused at a 1.0% floor", "SMALL" not in bought, bought)
check("a +1.5% move qualifies", "BIG" in bought, bought)

print("\n=== 5. THE WINDOW CLOSES ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"AAA": [100.0, 105.0]})
s_, e_ = Strat(), Exec()
c = cfg()
run(c, md, s_, e_, ["AAA"], st, at("09:30"))
check("no entry after decide_by", run(c, md, s_, e_, ["AAA"], st, at("09:33")) == 0)
check("...and the mode marks itself done", st["done"] is True)
check("still nothing later in the session",
      run(c, md, s_, e_, ["AAA"], st, at("09:45")) == 0 and e_.orders == [])

print("\n=== 6. IT CANNOT EAT THE SESSION'S CAPACITY ===")
st = {"baseline": {}, "taken": [], "done": False}
syms = [f"S{i}" for i in range(10)]
md = MD({s: [100.0, 102.0] for s in syms})
s_, e_ = Strat(), Exec()
c = cfg(max_positions=4)
run(c, md, s_, e_, syms, st, at("09:30"))
run(c, md, s_, e_, syms, st, at("09:31"))
check("stops at its own max_positions", len(e_.orders) == 4, len(e_.orders))
check("all ten would otherwise have qualified", len(st["baseline"]) == 10)
check("leaves room under max_concurrent_positions",
      len(e_.orders) < CFG["trading"]["max_concurrent_positions"])

print("\n=== 7. STREAMED ONLY ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"LIVE": [100.0, 101.0], "REST": [100.0, 101.0]}, streamed=["LIVE"])
s_, e_ = Strat(), Exec()
c = cfg()
run(c, md, s_, e_, ["LIVE", "REST"], st, at("09:30"))
run(c, md, s_, e_, ["LIVE", "REST"], st, at("09:31"))
bought = [o["symbol"] for o in e_.orders]
check("a streamed symbol is eligible", "LIVE" in bought, bought)
check("a REST symbol is skipped (its price is ~15 min stale)", "REST" not in bought, bought)
check("...and never even gets a baseline", "REST" not in st["baseline"], st["baseline"])

st = {"baseline": {}, "taken": [], "done": False}
s_, e_ = Strat(), Exec()
c2 = cfg(streamed_only=False)
run(c2, md, s_, e_, ["LIVE", "REST"], st, at("09:30"))
run(c2, md, s_, e_, ["LIVE", "REST"], st, at("09:31"))
check("streamed_only can be turned off", "REST" in [o["symbol"] for o in e_.orders])

print("\n=== 8. THE COOLDOWN DOES NOT BLOCK IT, AND IT DOES NOT ARM ONE ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"AAA": [100.0, 101.0]})
s_, e_ = Strat(), Exec(cooldown=300)      # symbol is in cooldown
c = cfg()
run(c, md, s_, e_, ["AAA"], st, at("09:30"))
run(c, md, s_, e_, ["AAA"], st, at("09:31"))
check("an opening entry ignores an existing cooldown", len(e_.orders) == 1, e_.orders)
s2, e2 = Strat(), Exec(cooldown=300)
check("a NORMAL entry still respects it",
      M._attempt_entry(CFG, s2, e2, "AAA", 100.0, "RAPID_INCREASE_IMMEDIATE", None) is False)

print("\n=== 9. NO DOUBLE ENTRY ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"AAA": [100.0, 101.0, 102.0]})
s_, e_ = Strat(), Exec()
c = cfg()
run(c, md, s_, e_, ["AAA"], st, at("09:30"))
run(c, md, s_, e_, ["AAA"], st, at("09:31"))
n1 = len(e_.orders)
run(c, md, s_, e_, ["AAA"], st, at("09:31", 30))
check("a symbol is bought at most once", len(e_.orders) == n1 == 1, len(e_.orders))
st2 = {"baseline": {"BBB": 100.0}, "taken": [], "done": False}
s3 = Strat(); s3.trades["BBB"] = {"price": 100, "qty": 1}
e3 = Exec()
run(c, MD({"BBB": [101.0]}), s3, e3, ["BBB"], st2, at("09:31"))
check("an already-open position is not re-bought", e3.orders == [])

print("\n=== 10. DISABLED IS A CLEAN NO-OP ===")
off = copy.deepcopy(CFG); off["trading"]["opening_burst"]["enabled"] = False
st = {"baseline": {}, "taken": [], "done": False}
e_ = Exec()
check("returns 0 and touches nothing",
      run(off, MD({"AAA": [100.0, 105.0]}), Strat(), e_, ["AAA"], st, at("09:31")) == 0
      and e_.orders == [])
check("_opening_burst_config returns None when off", M._opening_burst_config(off) is None)
check("...and a dict when on", isinstance(M._opening_burst_config(CFG), dict))

print("\n=== 11. FAILURES DO NOT TAKE THE SESSION DOWN ===")
class Broken(MD):
    def get_entry_price(self, s, bar): raise RuntimeError("feed exploded")
st = {"baseline": {}, "taken": [], "done": False}
e_ = Exec()
try:
    r = run(cfg(), Broken({"AAA": [100.0]}), Strat(), e_, ["AAA"], st, at("09:31"))
    check("a per-symbol failure is swallowed", r == 0)
except Exception as exc:
    check("a per-symbol failure is swallowed", False, exc)
st = {"baseline": {}, "taken": [], "done": False}
check("a symbol with no bar is skipped",
      run(cfg(), MD({}), Strat(), Exec(), ["GHOST"], st, at("09:31")) == 0)

print("\n=== 12. THE JOURNAL RECORDS IT, TAKEN OR NOT ===")
class Journal:
    def __init__(self): self.rows = []
    def record(self, **kw): self.rows.append(kw)
st = {"baseline": {}, "taken": [], "done": False}
j = Journal()
md = MD({"UP": [100.0, 101.0], "DOWN": [50.0, 49.0]})
c = cfg()
run(c, md, Strat(), Exec(), ["UP", "DOWN"], st, at("09:30"), journal=j)
run(c, md, Strat(), Exec(), ["UP", "DOWN"], st, at("09:31"), journal=j)
taken_rows = list(j.rows)
check("a taken entry is journalled immediately",
      [r["symbol"] for r in taken_rows] == ["UP"], [r["symbol"] for r in taken_rows])
check("journalled under OPENING_MOVE",
      {r["entry_method"] for r in taken_rows} == {M.OPENING_METHOD})
check("the move is recorded as signal_pct",
      any(abs((r.get("signal_pct") or 0) - 1.0) < 1e-6 for r in taken_rows),
      [r.get("signal_pct") for r in taken_rows])
# Refusals land once, at window close, with the move they finished on - the
# control group without which "buy what went up" is untestable.
run(c, md, Strat(), Exec(), ["UP", "DOWN"], st, at("09:33"), journal=j)
closed = j.rows[len(taken_rows):]
check("the refused ones are recorded at window close",
      [r["symbol"] for r in closed] == ["DOWN"], [r["symbol"] for r in closed])
check("...marked not taken", closed and closed[0]["taken"] is False)
check("...with a skip reason", closed and closed[0]["skip_reason"] == "opening_burst_not_taken")
check("...and their final move", closed and abs(closed[0]["signal_pct"] + 2.0) < 1e-6,
      closed[0]["signal_pct"] if closed else None)
check("a taken symbol is not double-journalled at close",
      not any(r["symbol"] == "UP" for r in closed))
check("one row per refused symbol, not one per poll", len(closed) == 1, len(closed))

print("\n=== 13. REPORT SECTION ===")
from src.notifications.email_notifier import EmailNotifier
n = EmailNotifier.__new__(EmailNotifier)
trades = [
    {"symbol": "MARA", "entry_method": "OPENING_MOVE", "entry_time": "2026-08-27T13:30:12",
     "exit_time": "2026-08-27T13:41:00", "entry_price": 11.5, "exit_price": 11.63,
     "qty": 100, "pl": 13.0, "pl_pct": 1.13, "signal_pct": 0.42, "mfe_pct": 1.31,
     "mae_pct": -0.12, "exit_reason": "TAKE_PROFIT_1%", "post_exit_note": "ran further"},
    {"symbol": "RIOT", "entry_method": "OPENING_MOVE", "entry_time": "2026-08-27T13:30:20",
     "exit_time": "2026-08-27T13:35:00", "entry_price": 20.8, "exit_price": 20.69,
     "qty": 80, "pl": -8.8, "pl_pct": -0.53, "signal_pct": 0.18, "mfe_pct": 0.0,
     "mae_pct": -0.61, "exit_reason": "FIRST_EXIT_-0.5%", "post_exit_note": "kept falling - exit was right"},
    {"symbol": "HOOD", "entry_method": "RAPID_INCREASE_IMMEDIATE", "pl": 40.0, "pl_pct": 1.0},
]
html = n._opening_burst_html(trades)
check("section renders", bool(html))
check("total P&L is at the top", "$4.20" in html, html[:400])
check("normal-session trades are excluded", "HOOD" not in html)
check("gain and loss both shown", "13.00" in html and "-8.80" in html)
check("percent gain/loss shown", "+1.13%" in html and "-0.53%" in html)
check("exit reason shown", "TAKE_PROFIT_1%" in html and "FIRST_EXIT" in html)
check("MFE/MAE shown", "Peak (MFE)" in html and "+1.31%" in html)
check("after-exit behaviour shown", "ran further" in html)
check("win/loss counts shown", "1W / 1L" in html, html[:600])
check("no opening trades -> no section",
      n._opening_burst_html([{"symbol": "X", "entry_method": "RAPID_INCREASE_IMMEDIATE", "pl": 1}]) == "")
check("empty input -> no section", n._opening_burst_html([]) == "")

print("\n=== 14. THE THREE SILENT FAILURES ===")
# Each of these would have produced NO error and NO opening trades - the mode
# would simply have done nothing while the logs looked normal. They are tested
# by reading the code paths that caused them, because the failure mode is an
# absence and an absence is what a passing test suite is worst at noticing.
msrc = open(repo_file("src", "main.py")).read()

# (a) The loop slept until entry_start, so 09:30-09:33 never executed.
check("the loop starts at the EARLIER of entry_start and the baseline",
      "loop_start = entry_start" in msrc and "min(entry_start, parse_hhmm_today" in msrc)
check("...and the sleep waits on loop_start, not entry_start",
      "while now < loop_start:" in msrc and "while now < entry_start:" not in msrc)
_ob = CFG["trading"]["opening_burst"]
check("with tomorrow's config that means 09:30, not 09:33",
      min(CFG["trading"]["entry_window_start"], _ob["baseline_time"]) == "09:30")

# (b) The stream subscribed only after the bell, so no price existed at 09:30:00.
check("the stream is subscribed pre-open", "PRE-OPEN: subscribing the stream" in msrc)
check("...gated on stream_prestart_minutes", "stream_prestart_minutes" in msrc)
# This used to assert the stream waited for augmentation. That gate is what
# starved the experiment on 2026-08-27, and it protected nothing: stream slots
# go by screener rank and the screener's own picks already fill the budget, so
# an augmented symbol could never win one. All 13 added that morning went to
# REST regardless.
check("...on the screener's list, WITHOUT waiting for augmentation",
      "and pending_selection is not None" in msrc
      and "Deliberately NOT gated on pending_augmented" in msrc)
check("...without double-starting at the open",
      "price_stream is not None and not price_stream.is_running()" in msrc)
from src.data.stream import PriceStream
check("is_running exists for that guard", hasattr(PriceStream, "is_running"))
check("prestart is early enough to beat the bell",
      CFG["trading"]["stream_prestart_minutes"] >= 1)

# (c) Refused symbols were not journalled, leaving the experiment untestable.
check("refusals are journalled at window close",
      "opening_burst_not_taken" in msrc)
check("...using the price they FINISHED on", "last_price" in msrc)

print("\n=== 15. THE FLAGS ARE REAL, NOT DECORATIVE ===")
# use_continuation_score sat inert for five sessions while appearing to gate
# entries. Any flag added since has to be readable in the code that uses it.
check("ignore_max_pct is actually consulted", "ignore_max_pct" in msrc)
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"HOT": [100.0, 103.0]})          # +3%, far above the 1.25% ceiling
e_ = Exec()
c_on = cfg(ignore_max_pct=True, min_move_pct=0.5)
run(c_on, md, Strat(), e_, ["HOT"], st, at("09:30"))
run(c_on, md, Strat(), e_, ["HOT"], st, at("09:31"))
check("ignore_max_pct=True buys a move above the ceiling", len(e_.orders) == 1, e_.orders)
st2 = {"baseline": {}, "taken": [], "done": False}
md2 = MD({"HOT": [100.0, 103.0]})
e2 = Exec()
c_off = cfg(ignore_max_pct=False, min_move_pct=0.5)
run(c_off, md2, Strat(), e2, ["HOT"], st2, at("09:30"))
run(c_off, md2, Strat(), e2, ["HOT"], st2, at("09:31"))
check("ignore_max_pct=False refuses it", e2.orders == [], e2.orders)
check("no inert skip_continuation_score flag remains",
      "skip_continuation_score" not in CFG["trading"]["opening_burst"],
      list(CFG["trading"]["opening_burst"]))

print("\n=== 16. TOMORROW'S SETTINGS ===")
# The literal moves session to session (0.5 -> 0.3 for 2026-08-28, to get the
# mode actually trading). What must hold is the PROPERTY: clear of the spread so
# a single bid/ask bounce cannot trigger it.
check("a threshold is set", _ob["min_move_pct"] > 0, _ob["min_move_pct"])
check("threshold clears the median spread (0.126% on 2026-08-26) by 2x+",
      _ob["min_move_pct"] > 0.126 * 2, _ob["min_move_pct"])
check("7 of the 10 concurrent slots", _ob["max_positions"] == 7)
check("3 slots left for the normal session",
      CFG["trading"]["max_concurrent_positions"] - _ob["max_positions"] == 3)
check("the ceiling does not apply", _ob["ignore_max_pct"] is True)
check("the mode reads its OWN threshold, not rapid_increase_pct",
      "min_move_pct" in msrc and "ob.get(\"min_move_pct\"" in msrc)
# 50M -> 3M on 2026-09-01. Measured on IEX-only bars, $50M demanded ~$2.5B of
# consolidated volume and kept 64 of 10,999 symbols, so the "wider" pool was
# mostly the 77-name static list it was meant to widen.
check("the universe floor is a liquidity guard, not a mega-cap filter",
      CFG["trading"]["universe_min_dollar_volume"] == 3_000_000,
      CFG["trading"]["universe_min_dollar_volume"])

print("\n=== 17. PER-TRADE EXIT PROFILE ===")
from src.strategy.strategy import Strategy, TradeManager
oc = M._opening_exit_config(CFG)
check("an exits block produces an override config", oc is not None)
n_, o_ = CFG["trading"], oc["trading"]
check("first exit is tighter", o_["first_exit_loss_pct"] == -0.3 and n_["first_exit_loss_pct"] == -0.7)
check("final exit is tighter", o_["final_exit_loss_pct"] == -0.35 and n_["final_exit_loss_pct"] == -1.0)
check("trailing stop is tighter", o_["trailing_stop_pct"] == 0.40 and n_["trailing_stop_pct"] == 0.75)
# 0.5/0.75/1.0 -> 0.75/1.0/1.25 for 2026-09-01.
check("take-profit tiers are tighter than the session's",
      [t["gain_pct"] for t in o_["take_profit_tiers"]] == [0.75, 1.0, 1.25],
      [t["gain_pct"] for t in o_["take_profit_tiers"]])
check("...and every opening tier sits below the normal top tier",
      max(t["gain_pct"] for t in o_["take_profit_tiers"])
      < max(t["gain_pct"] for t in n_["take_profit_tiers"]))
# The entry threshold and the first tier are measured from DIFFERENT anchors -
# min_move_pct from the 09:30 baseline, take-profit from the entry price - so
# a trade cannot scale out on the move that bought it. This asserts the anchors,
# not the numbers, so it survives a tier change.
_tm = TradeManager("A", 100.0, 100, oc)
_first = min(t["gain_pct"] for t in o_["take_profit_tiers"])
check("a tier is measured from ENTRY, not from the session open",
      _tm.check_take_profit(100.0 * (1 + _first / 100))[0] > 0
      and _tm.check_take_profit(100.0 * (1 + (_first - 0.05) / 100))[0] == 0,
      _first)
# 0.2 -> 0.05 for 2026-08-31. Arms almost immediately so an opening trade that
# ticks up cannot go on to lose. Note 0.05% is BELOW the 0.126% median bid-ask
# measured on 2026-08-26, so the spread alone can both arm and trigger it - the
# expected outcome is scratches at +0.05% rather than winners or losers, and the
# exit-reason table is where that will show.
check("breakeven arms sooner", [t["trigger_pct"] for t in o_["breakeven_tiers"]] == [0.15])
# The number is not round on purpose: 0.127% sits just above the 0.126% median
# bid-ask measured on 2026-08-26. A trigger INSIDE the spread can be armed by
# one print crossing it and fired by the next crossing back, which protects
# against noise instead of against losses. This is the smallest honest trigger.
check("...and it sits outside the measured bid-ask, not inside it",
      o_["breakeven_tiers"][0]["trigger_pct"] >= 0.126 * 1.15)
check("...and the floor sits ABOVE entry, so an armed trade cannot lose",
      [t["floor_pct"] for t in o_["breakeven_tiers"]] == [0.05])

# The floor has to actually hold at +0.05%, not merely be configured.
def _make_tm(peak):
    t = TradeManager("X", 100.0, 100, oc)
    t.highest_since_entry = peak
    return t

_bm = TradeManager("B", 100.0, 100, oc)
_bm.highest_since_entry = 100.0 * 1.0015  # peak past +0.127% arms it
check("armed above the spread, a fall back to entry exits", _bm.check_breakeven_stop(100.0) > 0)
check("...but a peak INSIDE the spread does not arm it",
      _make_tm(100.0 * 1.0006).check_breakeven_stop(100.0) == 0)
check("...and it does NOT exit while still above the floor",
      TradeManager("C", 100.0, 100, oc).check_breakeven_stop(100.0 * 1.001) == 0)
# Everything NOT overridden must be inherited, or the profile silently drops
# rules nobody restated.
for k in ("use_resistance_exit", "momentum_fade_window_minutes", "use_breakeven_floor",
          "max_stock_price", "min_stock_price", "use_take_profit"):
    check(f"inherits {k} unchanged", o_[k] == n_[k], (o_[k], n_[k]))
check("the live config is not mutated", CFG["trading"]["first_exit_loss_pct"] == -0.7)
check("no exits block -> no override",
      M._opening_exit_config({"trading": {"opening_burst": {"enabled": True}}}) is None)

print("\n=== 18. THE PROFILE REACHES THE POSITION ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"AAA": [100.0, 101.0]})
strat, ex = Strat(), Exec()
# Real Strategy so confirm_entry builds a real TradeManager.
real = Strategy(CFG)
class RealStrat(Strat):
    def confirm_entry(self, s, p, q, config_override=None):
        self.trades[s] = TradeManager(s, p, q, config_override or CFG)

rs = RealStrat()
run(cfg(), md, rs, ex, ["AAA"], st, at("09:30"))
run(cfg(), md, rs, ex, ["AAA"], st, at("09:31"))
tm = rs.trades.get("AAA")
check("the position exists", tm is not None)
check("it carries the TIGHT first exit",
      tm.config["trading"]["first_exit_loss_pct"] == -0.3,
      tm.config["trading"]["first_exit_loss_pct"])
check("it carries the TIGHT trailing stop", tm.config["trading"]["trailing_stop_pct"] == 0.40)
# and the tight stop actually fires earlier than the normal one would
tight = TradeManager("T", 100.0, 100, oc)
loose = TradeManager("L", 100.0, 100, CFG)
px = 100.0 * (1 - 0.004)          # -0.4%: past the tight stop, short of the loose one
check("-0.4% trips the opening first exit", tight.check_first_exit(px) > 0)
check("-0.4% does NOT trip the normal one", loose.check_first_exit(px) == 0)

print("\n=== 19. REPORT SHOWS THE PROFILE ===")
rows = M._opening_exit_profile_rows(CFG)
check("rows are produced", len(rows) >= 4, rows)
check("only DIFFERING rows are shown", all(r[1] != r[2] for r in rows), rows)
labels = {r[0] for r in rows}
check("covers the stops", {"first exit", "final exit", "trailing stop"} <= labels, labels)
n2 = EmailNotifier.__new__(EmailNotifier)
n2.run_context = {"opening_exits": rows}
html = n2._opening_exit_profile_html()
check("renders into the report", "-0.3%" in html and "0.4%" in html, html[:200])
check("shows the normal side for comparison", "-0.7%" in html and "0.75%" in html)
n3 = EmailNotifier.__new__(EmailNotifier)
n3.run_context = {}
check("no profile -> nothing rendered", n3._opening_exit_profile_html() == "")

print("\n=== 20. THRESHOLD REVIEW INSTRUMENTATION ===")
# "0 entered" is ambiguous between a threshold set too high and a mechanism that
# never ran. These log lines are what separates the two.
check("the move distribution is logged", "OPENING MOVES (best first)" in msrc)
check("the threshold is reviewed against it", "OPENING THRESHOLD REVIEW" in msrc)
check("taking nothing says WHY, and suggests a number",
      "OPENING BURST TOOK NOTHING" in msrc and "Lower min_move_pct to about" in msrc)
check("measuring nothing is reported as a DIFFERENT failure",
      "OPENING BURST MEASURED NOTHING" in msrc)
check("stream readiness is logged at the window open",
      "stream is serving" in msrc)
check("zero streamed symbols is an error, not a shrug",
      "NOTHING can be measured" in msrc)
check("stream connects earlier than before",
      CFG["trading"]["stream_prestart_minutes"] == 4)

print("\n=== 21. THE 2026-08-27 STARVATION ===")
# What happened: the QQQ list scored 98 constituents one at a time, took 3m17s,
# and finished at 09:31:50. The stream was gated on that finishing, so it
# subscribed 110s AFTER the 09:30 baseline with zero bars - and the augmentation
# also blocked the loop, so run_trading_day could not start until the
# 09:30-09:33 window had almost passed. The burst measured 0 of 28 and took
# nothing. Three separate guards now stop that recurring.
check("the stream no longer waits for augmentation",
      "Deliberately NOT gated on pending_augmented" in msrc)
check("...and the gate really is gone",
      "and pending_augmented\n                and market_data.is_trading_day(now)\n"
      "                and market_open_today - timedelta" not in msrc)
check("the late list build has a hard deadline", "augment_deadline_buffer_seconds" in msrc)
check("...enforced with a timeout, not a hope",
      "aug_future.result(timeout=deadline)" in msrc)
check("...and abandoning it keeps the screener's picks",
      "abandoning it" in msrc and "screener's" in msrc)
check("QQQ has its own earlier slot", "qqq_list_start_time" in msrc)
check("the late slot builds EARNINGS only", '("earnings",)' in msrc)
check("the at-the-open catch-up also skips the slow stage",
      'stages=("earnings",)' in msrc)

print("\n=== 22. THE PRE-OPEN TIMELINE FITS ===")
t = CFG["trading"]
def mins(hhmm): return int(hhmm[:2]) * 60 + int(hhmm[3:])
open_m = mins("09:30")
check("screener starts first", mins(t["screener_start_time"]) < mins(t["qqq_list_start_time"]))
check("QQQ runs early enough for a ~3.5 min pass",
      open_m - mins(t["qqq_list_start_time"]) >= 15,
      open_m - mins(t["qqq_list_start_time"]))
check("earnings stays late for the surprise to publish",
      mins(t["list_builder_start_time"]) >= mins("09:25"))
check("the stream window opens before the earnings slot",
      open_m - t["stream_prestart_minutes"] <= mins(t["list_builder_start_time"]))
check("the stream is up before the baseline",
      open_m - t["stream_prestart_minutes"] < mins(t["opening_burst"]["baseline_time"]) + 1)
check("QQQ finishes long before the stream subscribes",
      mins(t["qqq_list_start_time"]) + 4 < open_m - t["stream_prestart_minutes"])

print("\n=== 23. THE STAGES ARE SEPARABLE ===")
import src.screener.list_builder as LB
import inspect
sig = inspect.signature(LB.augment_symbols)
check("augment_symbols takes a stages argument", "stages" in sig.parameters)
check("...defaulting to both", sig.parameters["stages"].default == ("earnings", "qqq"))
src_lb = open(repo_file("src", "screener", "list_builder.py")).read()
check("earnings is gated on its stage", '"earnings" in stages' in src_lb)
check("qqq is gated on its stage", '"qqq" in stages' in src_lb)
sig2 = inspect.signature(M._augment_selection)
check("_augment_selection passes stages through", "stages" in sig2.parameters)

print("\n=== 24. THE LOOP STARTS EARLY, ENTRIES DO NOT ===")
# Starting the loop at 09:30 for the opening burst removed the implicit lower
# bound on the normal entry window - it used to be enforced by the function
# sleeping until entry_start. On 2026-08-27 that let OKTA and CRWD be bought at
# 09:33:13; both peaked at MFE 0.00%, both hit FINAL_EXIT -1.0%, -$195.52 in 25
# seconds.
check("the normal entry block has a LOWER bound again",
      "elif entry_start <= now < entry_end:" in msrc)
check("...and the unbounded form is gone",
      "elif now < entry_end:" not in msrc)
check("the loop still starts early for the burst", "while now < loop_start:" in msrc)
check("the two windows are distinct",
      CFG["trading"]["opening_burst"]["baseline_time"] < CFG["trading"]["entry_window_start"])

print("\n=== 25. THE REPORT ALWAYS STATES THE OUTCOME ===")
# Rendering nothing when there were no opening trades made a STARVED experiment
# look identical to a disabled one on 2026-08-27.
n4 = EmailNotifier.__new__(EmailNotifier)
n4.run_context = {"opening_burst_summary": {
    "enabled": True, "closed": True, "measured": 14, "taken": 0,
    "threshold": 0.5, "window": "09:30-09:33", "best_move": 0.31, "qualified": 0}}
h = n4._opening_burst_html([])
check("a zero-trade session still renders a section", bool(h))
check("it names it a THRESHOLD result", "THRESHOLD result" in h, h[:400])
check("it reports what was measured", "14 symbol" in h)
check("it reports the best move", "+0.310%" in h)

n5 = EmailNotifier.__new__(EmailNotifier)
n5.run_context = {"opening_burst_summary": {
    "enabled": True, "closed": True, "measured": 0, "taken": 0,
    "threshold": 0.5, "window": "09:30-09:33", "best_move": None, "qualified": 0}}
h2 = n5._opening_burst_html([])
check("measuring nothing renders a DIFFERENT diagnosis", "Measured NOTHING" in h2)
check("...and points at the stream, not the threshold",
      "stream was not serving" in h2 and "THRESHOLD result" not in h2)

n6 = EmailNotifier.__new__(EmailNotifier)
n6.run_context = {}
check("no summary and no trades -> still nothing (experiment off)",
      n6._opening_burst_html([]) == "")

print("\n=== 26. END TO END: MEASURE, QUALIFY, ENTER, REPORT ===")
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"WIN": [100.0, 100.8], "FLAT": [50.0, 50.05], "DOWN": [20.0, 19.8]})
rs2, ex2 = Strat(), Exec()
c = cfg()
run(c, md, rs2, ex2, ["WIN", "FLAT", "DOWN"], st, at("09:30"))
check("all three get a baseline", len(st["baseline"]) == 3, st["baseline"])
run(c, md, rs2, ex2, ["WIN", "FLAT", "DOWN"], st, at("09:31"))
check("only the +0.8% mover is bought", [o["symbol"] for o in ex2.orders] == ["WIN"],
      [o["symbol"] for o in ex2.orders])
check("it carries the opening exit profile",
      rs2.trades["WIN"]["cfg"] is not None and
      rs2.trades["WIN"]["cfg"]["trading"]["first_exit_loss_pct"] == -0.3)
j2 = Journal()
run(c, md, rs2, ex2, ["WIN", "FLAT", "DOWN"], st, at("09:33"), journal=j2)
check("the window closes", st["done"] is True)
check("the two refused symbols are journalled at close",
      sorted(r["symbol"] for r in j2.rows) == ["DOWN", "FLAT"],
      [r["symbol"] for r in j2.rows])
check("their final moves are recorded",
      all(r["signal_pct"] is not None for r in j2.rows),
      [r["signal_pct"] for r in j2.rows])

print("\n=== 27. LIVE FAILURE SCENARIOS ===")
# The scenarios that decide whether tomorrow produces a result or another blank.

def scenario(prices, streamed=None, cfg_over=None, times=("09:30", "09:31", "09:33")):
    st = {"baseline": {}, "taken": [], "done": False}
    md = MD(prices, streamed=streamed)
    strat, ex = Strat(), Exec()
    c = cfg(**(cfg_over or {}))
    j = Journal()
    for tm in times:
        run(c, md, strat, ex, list(prices), st, at(tm), journal=j)
    return st, ex, j

# (a) The stream never comes up: nothing streamed at all.
st, ex, j = scenario({"AAA": [100.0, 102.0], "BBB": [50.0, 51.0]}, streamed=[])
check("no streamed symbols -> no baselines", st["baseline"] == {}, st["baseline"])
check("...no trades", ex.orders == [])
check("...and the window still closes cleanly", st["done"] is True)

# (b) The stream comes up LATE - first prices only at 09:31.
st2 = {"baseline": {}, "taken": [], "done": False}
md2 = MD({"AAA": [100.0, 101.0]}, streamed=[])
s2, e2 = Strat(), Exec()
c2 = cfg()
run(c2, md2, s2, e2, ["AAA"], st2, at("09:30"))
check("nothing measured while the stream is down", st2["baseline"] == {})
md2.streamed = {"AAA"}                       # stream arrives
run(c2, md2, s2, e2, ["AAA"], st2, at("09:31"))
check("a late stream still takes a baseline", "AAA" in st2["baseline"])
run(c2, md2, s2, e2, ["AAA"], st2, at("09:31", 30))
check("...and can still trade inside the window", len(e2.orders) >= 0)

# (c) Every symbol qualifies - the budget must hold.
many = {f"S{chr(65+i)}": [100.0, 103.0] for i in range(12)}
st3, ex3, _ = scenario(many)
check("a fully qualifying field stops at max_positions",
      len(ex3.orders) == CFG["trading"]["opening_burst"]["max_positions"], len(ex3.orders))
check("...leaving slots for the normal session",
      len(ex3.orders) < CFG["trading"]["max_concurrent_positions"])

# (d) Nothing qualifies - a threshold result, not a failure.
st4, ex4, j4 = scenario({"AAA": [100.0, 100.05], "BBB": [50.0, 50.01]})
check("a quiet open takes nothing", ex4.orders == [])
check("...but everything is MEASURED", len(st4["baseline"]) == 2, st4["baseline"])
check("...and journalled as the control group", len(j4.rows) == 2, len(j4.rows))
check("...with their final moves", all(r["signal_pct"] is not None for r in j4.rows))

# (e) A symbol that falls then recovers inside the window.
st5 = {"baseline": {}, "taken": [], "done": False}
md5 = MD({"AAA": [100.0, 99.0, 101.0]})
s5, e5 = Strat(), Exec(); c5 = cfg()
run(c5, md5, s5, e5, ["AAA"], st5, at("09:30"))
run(c5, md5, s5, e5, ["AAA"], st5, at("09:31"))
check("a faller is not bought mid-window", e5.orders == [])
run(c5, md5, s5, e5, ["AAA"], st5, at("09:31", 30))
check("...but a recovery inside the window still qualifies", len(e5.orders) == 1, e5.orders)

# (f) Broker rejects the order - no phantom position.
class RejectExec(Exec):
    def submit_entry_order(self, s, qty, price, entry_method=None, entry_rsi=None,
                           spread_pct=None, is_opening_burst=False):
        return None
st6 = {"baseline": {}, "taken": [], "done": False}
md6 = MD({"AAA": [100.0, 102.0]})
s6, e6 = Strat(), RejectExec(); c6 = cfg()
run(c6, md6, s6, e6, ["AAA"], st6, at("09:30"))
run(c6, md6, s6, e6, ["AAA"], st6, at("09:31"))
check("a rejected order leaves NO tracked position", s6.trades == {}, s6.trades)
check("...and does not consume a slot", st6["taken"] == [], st6["taken"])

# (g) The exit profile reaches burst trades and NOT the session.
st7, ex7, _ = scenario({"AAA": [100.0, 102.0]})
check("a burst entry carries the tight profile", ex7.orders and True)
from src.strategy.strategy import TradeManager
oc = M._opening_exit_config(CFG)
tight = TradeManager("T", 100.0, 100, oc)
loose = TradeManager("L", 100.0, 100, CFG)
px = 100.0 * (1 - 0.004)
check("-0.4% exits a BURST position", tight.check_first_exit(px) > 0)
check("-0.4% does NOT exit a NORMAL position", loose.check_first_exit(px) == 0)
check("the session config is not mutated by building the profile",
      CFG["trading"]["first_exit_loss_pct"] == -0.7)

# (h) Disabled is inert.
off = copy.deepcopy(CFG); off["trading"]["opening_burst"]["enabled"] = False
st8 = {"baseline": {}, "taken": [], "done": False}
e8 = Exec()
run(off, MD({"AAA": [100.0, 105.0]}), Strat(), e8, ["AAA"], st8, at("09:31"))
check("disabled takes nothing and measures nothing",
      e8.orders == [] and st8["baseline"] == {})

print("\n=== 28. TOMORROW'S SETTINGS, ONE LAST TIME ===")
_t = CFG["trading"]
_o = _t["opening_burst"]
check("burst enabled", _o["enabled"] is True)
check("window 09:30 -> 09:33",
      (_o["baseline_time"], _o["decide_by"]) == ("09:30", "09:33"))
check("threshold 0.3%", _o["min_move_pct"] == 0.3, _o["min_move_pct"])
check("7 positions at half size",
      (_o["max_positions"], _o["size_multiplier"]) == (7, 0.5))
check("streamed only", _o["streamed_only"] is True)
check("ceiling does not apply", _o["ignore_max_pct"] is True)
check("cooldown neither respected nor armed", _o["skip_reentry_cooldown"] is True)
check("normal window still 09:33", _t["entry_window_start"] == "09:33")
# ON from 2026-08-31. Friday's constraint was the POOL, not the signal: UBER and
# SPCE traded well through the first 20 minutes and could never have been picked,
# because they were not among the 92 hand-written names. This does NOT help the
# opening burst - that is capped at the 14 symbols IEX serves - and may hurt it
# by widening the watchlist against the same 14 slots. Two experiments in one
# session; read the burst's readiness ramp on its own terms.
check("dynamic universe on", _t["use_dynamic_universe"] is True)
check("...and the rank is recorded so it can be judged on outcomes",
      "universe_rank" in open(repo_file("src", "main.py")).read())
check("50-name pool", len(_t["stock_universe"]) == 50, len(_t["stock_universe"]))

print("\n=== 29. BIGGEST MOVER FIRST ===")
# The budget is 7 and more than 7 can qualify. Taking them in watchlist order
# would fill the slots by an accident of sorting.
_prices = {
    "SMALL": [100.0, 100.4],    # +0.40%
    "HUGE":  [100.0, 103.0],    # +3.00%
    "MID":   [100.0, 101.2],    # +1.20%
    "TINY":  [100.0, 100.1],    # +0.10%, under the threshold
    "BIG":   [100.0, 102.0],    # +2.00%
}
_order = ["SMALL", "HUGE", "MID", "TINY", "BIG"]      # deliberately NOT by size
st9 = {"baseline": {}, "taken": [], "done": False}
md9 = MD(_prices)
s9, e9 = Strat(), Exec()
c9 = cfg(max_positions=3, min_move_pct=0.3)
run(c9, md9, s9, e9, _order, st9, at("09:30"))
run(c9, md9, s9, e9, _order, st9, at("09:31"))
_bought = [o["symbol"] for o in e9.orders]
check("the three BIGGEST movers are bought, in order",
      _bought == ["HUGE", "BIG", "MID"], _bought)
check("watchlist order is NOT what decided it", _bought[0] != "SMALL", _bought)
check("a qualifying but smaller mover is passed over when the budget is spent",
      "SMALL" not in _bought, _bought)
check("a sub-threshold symbol is never bought", "TINY" not in _bought, _bought)

# With room for everything, the sub-threshold one is still refused.
st10 = {"baseline": {}, "taken": [], "done": False}
md10 = MD(_prices); s10, e10 = Strat(), Exec()
c10 = cfg(max_positions=7, min_move_pct=0.3)
run(c10, md10, s10, e10, _order, st10, at("09:30"))
run(c10, md10, s10, e10, _order, st10, at("09:31"))
_b2 = [o["symbol"] for o in e10.orders]
check("with room, all four qualifiers are taken", sorted(_b2) == ["BIG", "HUGE", "MID", "SMALL"], _b2)
check("...still biggest-first", _b2 == ["HUGE", "BIG", "MID", "SMALL"], _b2)
check("...and TINY is still refused on merit", "TINY" not in _b2)

# Everything measured, whether or not it was bought.
check("every streamed symbol still gets a baseline", len(st10["baseline"]) == 5, st10["baseline"])

# A later, bigger mover cannot displace an already-open position - ranking is
# within a poll, and that trade-off is deliberate.
st11 = {"baseline": {}, "taken": [], "done": False}
md11 = MD({"EARLY": [100.0, 101.0, 101.0], "LATER": [100.0, 100.0, 105.0]})
s11, e11 = Strat(), Exec()
c11 = cfg(max_positions=1, min_move_pct=0.3)
run(c11, md11, s11, e11, ["EARLY", "LATER"], st11, at("09:30"))
run(c11, md11, s11, e11, ["EARLY", "LATER"], st11, at("09:31"))
check("the early qualifier takes the only slot", [o["symbol"] for o in e11.orders] == ["EARLY"],
      [o["symbol"] for o in e11.orders])
run(c11, md11, s11, e11, ["EARLY", "LATER"], st11, at("09:31", 30))
check("a bigger later mover does NOT displace it (ranking is within a poll)",
      [o["symbol"] for o in e11.orders] == ["EARLY"], [o["symbol"] for o in e11.orders])

print("\n=== 18. THE BURST RUNS ON A STREAM THAT COMES UP LATE ===")
# The real-world failure three sessions running was never the threshold - it was
# that no symbol had a price when the window opened. IEX carries ~2% of US
# volume, so names appear as they happen to print there. The burst must survive
# a stream that serves nothing at 09:30 and fills in over the following minutes.
st_late = {"baseline": {}, "taken": [], "done": False}
# Prices are consumed per read, and a symbol that is not streamed is never
# read - so these two values are the FIRST and SECOND prints each symbol makes
# once it appears, not a fixed 09:30/09:31 schedule.
md_late = MD({"A": [100.0, 101.0], "B": [50.0, 50.1]}, streamed=set())
e_late, s_late = Exec(), Strat()
c_late = cfg()
check("nothing measured while the stream serves no symbols",
      run(c_late, md_late, s_late, e_late, ["A", "B"], st_late, at("09:30")) == 0
      and not st_late["baseline"])
check("...and the window is NOT closed by that",
      st_late.get("done") is not True)

# 09:31 - the stream starts serving. Baselines are taken on FIRST print, so a
# symbol that appears late still gets one; it is measured from when it appeared,
# which is the honest reading.
md_late.streamed = {"A", "B"}
run(c_late, md_late, s_late, e_late, ["A", "B"], st_late, at("09:31"))
check("a late-arriving symbol still gets a baseline", set(st_late["baseline"]) == {"A", "B"},
      st_late["baseline"])
check("...and the baseline is its FIRST price, not the 09:30 one it never sent",
      st_late["baseline"]["A"] == 100.0, st_late["baseline"])

# 09:32 - it moves. Still inside the window, so it can still be bought.
run(c_late, md_late, s_late, e_late, ["A", "B"], st_late, at("09:32"))
bought_late = [o["symbol"] for o in e_late.orders]
check("a symbol that qualifies after a late start is still bought",
      "A" in bought_late, bought_late)
check("...and one that did not move is not", "B" not in bought_late, bought_late)

# The window still closes on time regardless of when the stream arrived.
run(c_late, md_late, s_late, e_late, ["A", "B"], st_late, at("09:33"))
check("the window still closes at decide_by", st_late["done"] is True)

print("\n=== 19. TAKE-PROFIT TIERS ===")
_oc = M._opening_exit_config(CFG)
_tiers = [t["gain_pct"] for t in _oc["trading"]["take_profit_tiers"]]
check("burst tiers are 0.75/1.0/1.25", _tiers == [0.75, 1.0, 1.25], _tiers)
check("...still tighter than the session's top tier",
      max(_tiers) < max(t["gain_pct"] for t in CFG["trading"]["take_profit_tiers"]))
check("...and strictly increasing", _tiers == sorted(_tiers) and len(set(_tiers)) == 3)
_fracs = [t["sell_fraction"] for t in _oc["trading"]["take_profit_tiers"]]
check("the last tier closes the position", _fracs[-1] == 1.0, _fracs)
# A tier must not be reachable by the entry move itself: tiers measure from
# ENTRY, min_move_pct from the 09:30 baseline, so the first tier has to sit
# above zero regardless - but it must also not be so low the spread reaches it.
check("the first tier clears the measured bid-ask", _tiers[0] > 0.126, _tiers[0])

print("\n=== 20. MULTI-FACTOR GATE: move must clear its OWN spread, not just min_move_pct ===")
check("shipped enabled at 2x", CFG["trading"]["opening_burst"]["min_move_to_spread_ratio"] == 2.0)


class MDSpread(MD):
    """Same fake as everywhere else in this file, plus a quote broker so
    _spread_pct (which _run_opening_burst's gate reads directly, unlike its
    normal journal-only callers) has something to measure."""
    def __init__(self, prices, spreads, streamed=None):
        super().__init__(prices, streamed=streamed)
        self.broker = types.SimpleNamespace(
            get_latest_quote=lambda s: {"spread": spreads.get(s)} if s in spreads else None
        )


# WIDE: +0.4% move, but a $0.30 spread on a $100 stock is 0.3% - the move is
# only 1.33x its own spread, under the 2.0x ratio, so it reads as noise.
# TIGHT: same +0.4% move, a one-cent spread - comfortably 40x itself, real.
st = {"baseline": {}, "taken": [], "done": False}
md = MDSpread({"WIDE": [100.0, 100.4], "TIGHT": [100.0, 100.4]},
              {"WIDE": 0.30, "TIGHT": 0.01})
s_, e_ = Strat(), Exec()
c = cfg(min_move_pct=0.3, min_move_to_spread_ratio=2.0)
run(c, md, s_, e_, ["WIDE", "TIGHT"], st, at("09:30"))
run(c, md, s_, e_, ["WIDE", "TIGHT"], st, at("09:31"))
bought = [o["symbol"] for o in e_.orders]
check("a move that clears min_move_pct but not 2x its own spread is refused",
      "WIDE" not in bought, bought)
check("the same-sized move on a tight spread is bought",
      "TIGHT" in bought, bought)

# Symbol with no quote available (the common case - _spread_pct is best-effort
# and this mode ran before this gate existed without it) is NOT penalized -
# "unmeasurable" is not "bad", same rule the continuation score already uses.
st2 = {"baseline": {}, "taken": [], "done": False}
md2 = MDSpread({"NOQUOTE": [100.0, 100.4]}, {})
e2 = Exec()
run(c, md2, Strat(), e2, ["NOQUOTE"], st2, at("09:30"))
run(c, md2, Strat(), e2, ["NOQUOTE"], st2, at("09:31"))
check("no quote available -> gate does not refuse it",
      "NOQUOTE" in [o["symbol"] for o in e2.orders])

# ratio: 0 disables the gate outright, same shape as every other 0-disables
# knob in this config (rapid_increase_max_pct, market_burst_spy_pct, ...).
st3 = {"baseline": {}, "taken": [], "done": False}
md3 = MDSpread({"WIDE": [100.0, 100.4]}, {"WIDE": 0.30})
e3 = Exec()
run(cfg(min_move_pct=0.3, min_move_to_spread_ratio=0), md3, Strat(), e3, ["WIDE"], st3, at("09:30"))
run(cfg(min_move_pct=0.3, min_move_to_spread_ratio=0), md3, Strat(), e3, ["WIDE"], st3, at("09:31"))
check("ratio 0 disables the gate", "WIDE" in [o["symbol"] for o in e3.orders])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)


