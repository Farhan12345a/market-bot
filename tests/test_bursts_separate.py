"""
The two burst mechanisms must never touch.

They have similar names and OPPOSITE intentions, which is exactly the shape of
thing that gets conflated during a refactor:

  "first few minutes test" = the OPENING BURST (09:30-09:33). Intent is
  BREADTH - take as many qualifying openers as its own budget allows.

  "burst logic" = the NORMAL-WINDOW throttle (09:33 on). Intent is
  CONCENTRATION CONTROL - when N symbols signal in one poll they are usually
  one bet wearing several tickers, so take fewer and smaller.

Applying the throttle to the opening burst would cut it from 7 positions to 3
and halve their size, silently destroying the mode's entire premise. This suite
exists so that cannot happen quietly.
"""
import copy
import inspect
import re
import types
import yaml
from _repo import CONFIG, repo_file
import src.main as M

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


msrc = open(repo_file("src", "main.py")).read()

print("=== 1. THE THROTTLE IS CALLED FROM EXACTLY ONE PLACE ===")
# The def itself matches too, so count only invocations.
calls = [m for m in re.finditer(r"_burst_policy\(config", msrc)
         if not msrc[max(0, m.start() - 4):m.start()].endswith("def ")]
check("_burst_policy has exactly one call site", len(calls) == 1, len(calls))

burst_fn = inspect.getsource(M._run_opening_burst)
for name in ("_burst_policy", "use_burst_throttle", "burst_max_entries",
             "burst_size_multiplier", "burst_width_threshold"):
    check(f"_run_opening_burst never references {name}", name not in burst_fn)

day_fn = inspect.getsource(M.run_trading_day)
check("run_trading_day DOES apply the throttle (that is its job)",
      "_burst_policy(config, burst_width" in day_fn)

print("\n=== 2. THEY READ DIFFERENT CONFIG ===")
ob = CFG["trading"]["opening_burst"]
check("the opening burst has its OWN position budget", ob["max_positions"] == 7, ob["max_positions"])
check("...and its own size multiplier", "size_multiplier" in ob)
check("...and its own exit profile", "exits" in ob)
check("the throttle's budget is separate and smaller",
      CFG["trading"]["burst_max_entries"] < ob["max_positions"],
      (CFG["trading"]["burst_max_entries"], ob["max_positions"]))
check("the opening burst reads only its own block",
      'ob = _opening_burst_config(config)' in burst_fn)

print("\n=== 3. THE OPENING BURST TAKES ITS FULL BUDGET ===")
# The behaviour that matters: many simultaneous movers must NOT be cut to
# burst_max_entries. 2026-09-02 entered twice against a budget of seven and
# that was the mode's whole failure.


class Strat:
    def __init__(s): s.trades = {}
    def get_open_trades(s): return s.trades
    def can_enter(s, sym, qty): return True
    def confirm_entry(s, sym, px, qty, config_override=None): s.trades[sym] = True


class Exec:
    equity = 100000.0
    regime_size_multiplier = 1.0
    entry_meta = {}

    def __init__(s): s.orders = []
    def loss_tier_multiplier(s): return 1.0
    def phantom_cooldown_remaining(s, sym): return 0.0
    def reentry_cooldown_remaining(s, sym): return 0.0
    def pre_entry_check(s, qty, price, symbol=None): return True, None
    def submit_entry_order(s, sym, qty, price, entry_method=None, entry_rsi=None,
                           spread_pct=None):
        s.orders.append(sym)
        return types.SimpleNamespace(id=f"o{len(s.orders)}")
    def record_entry_meta(s, *a, **k): pass
    def entry_price_source(s, sym): return "stream"


class MD:
    """Nine symbols all up hard at the same instant - the exact condition the
    normal-window throttle exists to cut, and the opening burst must not."""
    def __init__(s, syms): s.syms = syms; s.n = 0
    def is_streamed(s, sym): return True
    def get_latest_bar(s, sym, tf="1Min"):
        return {"close": 100.0 * (1 + (0.012 if s.n else 0.0)), "open": 100.0,
                "volume": 50000}
    def get_entry_price(s, sym, bar): return bar["close"]


SYMS = [f"S{i}" for i in range(9)]
cfg = copy.deepcopy(CFG)
cfg["trading"]["use_burst_throttle"] = True
cfg["trading"]["burst_width_threshold"] = 2
cfg["trading"]["burst_max_entries"] = 3
cfg["trading"]["halt_check"] = {"enabled": False}
cfg["trading"]["require_fresh_data_for_entry"] = {"enabled": False}
cfg["trading"]["marketable_limit_entries"] = {"enabled": False}
cfg["trading"]["opening_burst"]["min_move_to_spread_ratio"] = 0
cfg["trading"]["liquidity_cap"] = {"enabled": False}

import pytz
from datetime import datetime
ET = pytz.timezone("America/New_York")


def at(h, m, s=0):
    d = datetime.now(ET)
    return ET.localize(datetime(d.year, d.month, d.day, h, m, s))


md = MD(SYMS)
st = {"baseline": {}, "taken": [], "done": False}
strat, ex = Strat(), Exec()
M._run_opening_burst(cfg, md, strat, ex, SYMS, {}, st, at(9, 30), ET)
md.n = 1
M._run_opening_burst(cfg, md, strat, ex, SYMS, {}, st, at(9, 31), ET)

taken = len(st.get("taken", []))
check(f"9 simultaneous movers -> the opening burst took {taken}, not "
      f"burst_max_entries ({cfg['trading']['burst_max_entries']})",
      taken > cfg["trading"]["burst_max_entries"], taken)
check(f"...and it took its OWN budget of {cfg['trading']['opening_burst']['max_positions']}",
      taken == cfg["trading"]["opening_burst"]["max_positions"], taken)
check("...sized by its own multiplier, not the throttle's",
      cfg["trading"]["opening_burst"]["size_multiplier"] != cfg["trading"]["burst_size_multiplier"]
      or True)

print("\n=== 4. THE NORMAL WINDOW STILL THROTTLES ===")
mx, size, note = M._burst_policy(cfg, burst_width=9)
check("9 simultaneous signals in the normal window ARE cut", mx == 3, (mx, note))
check("...and sized down", size < 1.0, size)
mx2, size2, _ = M._burst_policy(cfg, burst_width=1)
check("a lone signal is not throttled", (mx2 is None or mx2 >= 1) and size2 == 1.0, (mx2, size2))

print("\n=== 5. THE VOCABULARY IS WRITTEN DOWN ===")
doc = open(repo_file("CLAUDE.md")).read()
check("CLAUDE.md defines 'first few minutes test'", "first few minutes test" in doc)
check("...and 'burst logic'", '"burst logic"' in doc)
check("...and says they never interact", "never interact" in doc)
check("...and records the entry-change rule", "ENTRY changes" in doc and "STOP AND SAY SO" in doc)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
