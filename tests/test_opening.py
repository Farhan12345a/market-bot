"""
The opening-move experiment: measure each streamed symbol from the 09:30 open,
buy the ones that are up, decide by 09:32.

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
    def pre_entry_check(self, qty, price): return True, ""
    def submit_entry_order(self, s, qty, price, entry_method=None, entry_rsi=None):
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
check("decides by 09:32", ob["decide_by"] == "09:32")
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
check("no entry after decide_by", run(c, md, s_, e_, ["AAA"], st, at("09:32")) == 0)
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
run(c, md, Strat(), Exec(), ["UP", "DOWN"], st, at("09:32"), journal=j)
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
check("...and only once the watchlist is final",
      "and pending_augmented" in msrc)
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
check("threshold is 0.5% over the window", _ob["min_move_pct"] == 0.5, _ob["min_move_pct"])
check("threshold clears the median spread (0.126% on 2026-08-26)",
      _ob["min_move_pct"] > 0.126 * 3)
check("7 of the 10 concurrent slots", _ob["max_positions"] == 7)
check("3 slots left for the normal session",
      CFG["trading"]["max_concurrent_positions"] - _ob["max_positions"] == 3)
check("the ceiling does not apply", _ob["ignore_max_pct"] is True)
check("rapid_increase_pct is irrelevant here - the mode uses its own threshold",
      "min_move_pct" in msrc and _ob["min_move_pct"] != CFG["trading"]["rapid_increase_pct"])
check("heavily-traded universe floor raised to $50M",
      CFG["trading"]["universe_min_dollar_volume"] == 50_000_000)

print("\n=== 17. PER-TRADE EXIT PROFILE ===")
from src.strategy.strategy import Strategy, TradeManager
oc = M._opening_exit_config(CFG)
check("an exits block produces an override config", oc is not None)
n_, o_ = CFG["trading"], oc["trading"]
check("first exit is tighter", o_["first_exit_loss_pct"] == -0.3 and n_["first_exit_loss_pct"] == -0.5)
check("final exit is tighter", o_["final_exit_loss_pct"] == -0.6 and n_["final_exit_loss_pct"] == -1.0)
check("trailing stop is tighter", o_["trailing_stop_pct"] == 0.40 and n_["trailing_stop_pct"] == 0.75)
check("take-profit tiers are tighter than the session's",
      [t["gain_pct"] for t in o_["take_profit_tiers"]] == [0.5, 0.75, 1.0],
      [t["gain_pct"] for t in o_["take_profit_tiers"]])
check("...and every opening tier sits below the normal top tier",
      max(t["gain_pct"] for t in o_["take_profit_tiers"])
      < max(t["gain_pct"] for t in n_["take_profit_tiers"]))
# The entry threshold and the first tier are measured from DIFFERENT anchors -
# min_move_pct from the 09:30 baseline, take-profit from the entry price - so
# them sharing the number 0.5 does not let a trade scale out on the move that
# bought it. This asserts the anchors, not the numbers.
_tm = TradeManager("A", 100.0, 100, oc)
check("a tier is measured from ENTRY, not from the session open",
      _tm.check_take_profit(100.0 * 1.005)[0] > 0 and _tm.check_take_profit(100.0 * 1.004)[0] == 0)
check("breakeven arms sooner", [t["trigger_pct"] for t in o_["breakeven_tiers"]] == [0.2])
# Everything NOT overridden must be inherited, or the profile silently drops
# rules nobody restated.
for k in ("use_resistance_exit", "momentum_fade_window_minutes", "use_breakeven_floor",
          "max_stock_price", "min_stock_price", "use_take_profit"):
    check(f"inherits {k} unchanged", o_[k] == n_[k], (o_[k], n_[k]))
check("the live config is not mutated", CFG["trading"]["first_exit_loss_pct"] == -0.5)
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
check("shows the normal side for comparison", "-0.5%" in html and "0.75%" in html)
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

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
