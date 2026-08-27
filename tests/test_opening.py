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
    def confirm_entry(self, s, p, q): self.trades[s] = {"price": p, "qty": q}


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
check("has its OWN position budget", ob["max_positions"] == 4)
check("budget leaves room for the normal session",
      ob["max_positions"] < CFG["trading"]["max_concurrent_positions"], ob["max_positions"])
check("half size", ob["size_multiplier"] == 0.5)
check("streamed only", ob["streamed_only"] is True)
check("skips the continuation score (blind this early)", ob["skip_continuation_score"] is True)
check("ignores the signal ceiling", ob["ignore_max_pct"] is True)
check("does not arm the re-entry cooldown", ob["skip_reentry_cooldown"] is True)
check("normal entry window is UNCHANGED at 09:33",
      CFG["trading"]["entry_window_start"] == "09:33")
check("stream starts before the bell", CFG["trading"]["stream_prestart_minutes"] == 2)

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
st = {"baseline": {}, "taken": [], "done": False}
md = MD({"UP": [100.0, 101.0], "DOWN": [50.0, 49.0], "FLAT": [20.0, 20.0]})
s_, e_ = Strat(), Exec()
c = cfg()
run(c, md, s_, e_, ["UP", "DOWN", "FLAT"], st, at("09:30"))
run(c, md, s_, e_, ["UP", "DOWN", "FLAT"], st, at("09:31"))
bought = [o["symbol"] for o in e_.orders]
check("a riser is bought", "UP" in bought, bought)
check("a faller is NOT bought", "DOWN" not in bought, bought)
check("flat at min_move_pct 0.0 still qualifies (any increase)", "FLAT" in bought, bought)
check("tagged as OPENING_MOVE", all(o["method"] == M.OPENING_METHOD for o in e_.orders))

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

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
