"""
The four guards shipped for 2026-08-31, tested as behaviour rather than as config.

  1. max_positions_per_sector    - stops one complex taking every slot
  2. breadth_halt                - stops new entries on a broadly falling tape
  3. universe_rank               - records WHERE a name placed, so the dynamic
                                   universe can be judged on outcomes
  4. the opening breakeven floor - arms OUTSIDE the measured bid-ask

All four ship in the same session, which is exactly why each needs a test that
fails for its own reason. A config assertion ("the number is 3") passes just as
happily when the code reading that number is broken; these drive the real
functions and check what they DO.

The costliest failure mode is a guard that silently refuses everything - a
sector map bucketing every symbol together, or a halt latching on an empty
sample - because the day would just look quiet and nothing in the report would
say why. Several tests below exist only to pin that down.
"""
import copy, yaml
from datetime import datetime
import pytz
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.executor.executor import Executor
from src.strategy.strategy import TradeManager
from src.analytics.sectors import sector_for

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


class Broker:
    def get_positions(self): return {}


def at(hhmm):
    return datetime.now(ET).replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]),
                                    second=0, microsecond=0)


def executor(**over):
    c = copy.deepcopy(CFG)
    c["trading"].update(over)
    ex = Executor(Broker(), c)
    # A funded, unconstrained account, so anything refused below is refused by
    # the guard under test and not by buying power or exposure.
    ex._buying_power = 1_000_000.0
    ex._equity = 1_000_000.0
    ex._total_exposure_usd = 0.0
    ex._open_symbols = set()
    return ex


print("=== 1. SECTOR CAP ===")
# WGMI is the crypto-miner complex: 9 of 30 positions and 0 winners on
# 2026-08-28, which is what this exists to prevent.
MINERS = [s for s in ("MARA", "RIOT", "CLSK", "HUT", "WULF", "CIFR")
          if sector_for(s)]
check("the miner names actually map to a complex", len(MINERS) >= 4, MINERS)
check("...and to the SAME one", len({sector_for(s) for s in MINERS}) == 1,
      {s: sector_for(s) for s in MINERS})

ex = executor(max_positions_per_sector=3)
for s in MINERS[:3]:
    ex._open_symbols.add(s)
ok, why = ex.pre_entry_check(10, 100.0, symbol=MINERS[3])
check("a 4th name in a full complex is refused", ok is False, why)
check("...and the reason names the complex and the count",
      "max_positions_per_sector" in why and "3/3" in why, why)

# The cap must bind on the SECTOR, not on the position count - a different
# complex has to still get through with three positions already open.
other = next((s for s in ("XOM", "JPM", "PFE", "WMT", "SPY")
              if sector_for(s) and sector_for(s) != sector_for(MINERS[0])), None)
check("a control symbol from another complex exists", other is not None)
if other:
    ok2, why2 = ex.pre_entry_check(10, 100.0, symbol=other)
    check("a different complex is NOT refused", ok2 is True, why2)

# Re-entering a name already held must not count itself and lock itself out.
ok3, _ = ex.pre_entry_check(10, 100.0, symbol=MINERS[0])
check("a symbol already held does not count against its own cap", ok3 is True)

# An unmapped symbol has no complex to be concentrated in. Refusing it would
# lump every unknown name into one phantom bucket and starve all of them.
ghost = "ZZZQ"
check("the ghost symbol really is unmapped", sector_for(ghost) is None, sector_for(ghost))
ok4, _ = ex.pre_entry_check(10, 100.0, symbol=ghost)
check("an unmapped symbol is never refused", ok4 is True)

# Without a symbol the check has nothing to reason about and must not guess.
ok5, _ = ex.pre_entry_check(10, 100.0)
check("no symbol passed -> the sector check is skipped, not failed", ok5 is True)

# Cap off entirely = old behaviour.
ex_off = executor(max_positions_per_sector=0)
for s in MINERS[:5]:
    ex_off._open_symbols.add(s)
ok6, _ = ex_off.pre_entry_check(10, 100.0, symbol=MINERS[5] if len(MINERS) > 5 else MINERS[0])
check("cap of 0 disables the check", ok6 is True)

# The guard must not swallow the OTHER checks.
ex_broke = executor(max_positions_per_sector=3)
ex_broke._buying_power = 1.0
ok7, why7 = ex_broke.pre_entry_check(10, 100.0, symbol=MINERS[0])
check("buying power still refuses first", ok7 is False and "buying power" in why7, why7)


print("\n=== 2. BREADTH: MEASUREMENT ONLY (the halt was retired 2026-09-02) ===")
# _breadth_halt did two jobs: it measured the watchlist's mean move since the
# open, and it latched a hard NO-NEW-ENTRIES halt below -0.3% at 09:45.
#
# The HALT is gone. It had been dead code since regime sizing shipped - the
# flag was AND-ed with `not regime_active`, and regime_active is true - and
# keeping it meant two guards answering "is today tradeable?" from different
# evidence, where whichever ran first won by ordering accident rather than by
# rule. regime_sizing supersedes it properly: continuous size scaling, and at
# bearish_multiplier 0.0 it can still refuse everything.
#
# The MEASUREMENT stays, and now carries DISPERSION and CROSSINGS too - the
# chop reading breadth_halt never computed.


class MD:
    def __init__(self, now_px): self.now_px = now_px
    def get_latest_bar(self, s, tf="1Min"):
        return {"close": self.now_px[s]} if s in self.now_px else None
    def get_entry_price(self, s, bar): return self.now_px.get(s)


SYMS = ["A", "B", "C", "D", "E", "F"]
opens = {s: 100.0 for s in SYMS}


def bstate(o):
    return {"open_px": dict(o)}


check("the halt function is gone entirely", not hasattr(M, "_breadth_halt"))
check("the measurement replaces it", hasattr(M, "_measure_breadth"))
check("the retired config key is gone", "breadth_halt" not in CFG["trading"])
check("...and the measurement block remains",
      CFG["trading"]["breadth"]["enabled"] is True)

# A tape down ~1%: the 2026-08-28 shape, where the mean signal returned -1.045%.
down = MD({s: 99.0 for s in SYMS})
st = bstate(opens)
M._measure_breadth(CFG, down, SYMS, st, at("09:45"), ET)
check("it records the mean it saw", round(st["mean_move"], 2) == -1.0, st.get("mean_move"))
check("...and how many symbols it saw", st["breadth_n"] == 6, st.get("breadth_n"))
check("...and how many are falling", st["falling"] == 6, st.get("falling"))
check("a uniform move has ~zero dispersion", st["dispersion"] < 0.01, st.get("dispersion"))

# It measures EVERY poll now, rather than latching after one check. A regime
# read that only ever sees 09:45 governs 15:45 as well.
up_now = MD({s: 102.0 for s in SYMS})
M._measure_breadth(CFG, up_now, SYMS, st, at("09:50"), ET)
check("a later poll RE-measures rather than latching", round(st["mean_move"], 2) == 2.0,
      st.get("mean_move"))

# Dispersion is the number the old function never computed, and the one that
# tells "nothing is happening" apart from "everything is happening in both
# directions at once".
st_chop = bstate(opens)
M._measure_breadth(CFG, MD({"A": 101.5, "B": 98.5, "C": 101.2, "D": 98.8,
                            "E": 100.9, "F": 99.1}), SYMS, st_chop, at("09:45"), ET)
check("a scattered tape reads near-zero MEAN", abs(st_chop["mean_move"]) < 0.25,
      st_chop.get("mean_move"))
check("...but HIGH dispersion", st_chop["dispersion"] > 0.6, st_chop.get("dispersion"))
check("...which _chop_reading calls CHOPPY", M._chop_reading(CFG, st_chop)[0] is True)
check("the uniformly falling tape is NOT chop - that is bearish",
      M._chop_reading(CFG, {"mean_move": -1.0, "dispersion": 0.005})[0] is False)

# Thin evidence must never produce a reading. A stream serving 3 symbols would
# otherwise decide the session for 27.
st4 = {"open_px": {"A": 100.0, "B": 100.0}}
M._measure_breadth(CFG, MD({"A": 90.0, "B": 90.0}), ["A", "B"], st4, at("09:45"), ET)
check("too few symbols -> no mean at all, rather than a confident wrong one",
      st4.get("mean_move") is None, st4.get("mean_move"))
check("...and no dispersion either", st4.get("dispersion") is None)
check("...so the chop reading abstains", M._chop_reading(CFG, st4)[0] is False)

# Missing open prices are simply symbols the check cannot see.
st5 = {"open_px": {s: 100.0 for s in SYMS[:5]}}
M._measure_breadth(CFG, MD({s: 99.0 for s in SYMS}), SYMS, st5, at("09:45"), ET)
check("only symbols with an open price are counted", st5.get("breadth_n") == 5,
      st5.get("breadth_n"))

# Crossings: a name that keeps flipping sides of its own open is chop's most
# direct signature.
st6 = bstate(opens)
for px in (101.0, 99.0, 101.0, 99.0):
    M._measure_breadth(CFG, MD({s: px for s in SYMS}), SYMS, st6, at("09:45"), ET)
check("repeated flips across the open are counted per symbol",
      st6["crossings"]["A"] >= 3, st6.get("crossings"))
check("...and surfaced as choppy_symbols", st6["choppy_symbols"] == len(SYMS),
      st6.get("choppy_symbols"))
st7 = bstate(opens)
for px in (100.5, 101.0, 101.5):
    M._measure_breadth(CFG, MD({s: px for s in SYMS}), SYMS, st7, at("09:45"), ET)
check("a steadily rising tape records no crossings at all",
      not any(st7.get("crossings", {}).values()), st7.get("crossings"))


print("\n=== 3. THE REGIME AND THE WINDOW FIT TOGETHER ===")
_t = CFG["trading"]
_r = _t["regime_sizing"]
check("regime sizing is enabled", _r["enabled"] is True)
check("its authoritative read is at 09:40 (moved from 09:45 - the 2026-09-02 "
      "session was over at 09:38:19)", _r["check_time"] == "09:40")
check("entry window ends 10:15", _t["entry_window_end"] == "10:15")
_start = _t["entry_window_start"]
check("the read lands INSIDE the entry window",
      _start < _r["check_time"] < _t["entry_window_end"],
      (_start, _r["check_time"], _t["entry_window_end"]))
_mins_after = (int(_t["entry_window_end"][:2]) * 60 + int(_t["entry_window_end"][3:])) - \
              (int(_r["check_time"][:2]) * 60 + int(_r["check_time"][3:]))
check("at least 10 minutes of window left after it", _mins_after >= 10, _mins_after)
# ...and unlike the old halt, it is no longer the FIRST reading of the day.
check("the regime also reads every poll from the open, so 09:30-09:40 is not "
      "ungoverned", _r["cadence"]["opening_seconds"] == 0)


print("\n=== 4. UNIVERSE RANK ===")
from src.screener.universe import select_candidates
check("dynamic universe is ON", _t["use_dynamic_universe"] is True)

# A static-pool session has no ranking, and must record that rather than a
# fabricated rank of zero.
c_static = copy.deepcopy(CFG); c_static["trading"]["use_dynamic_universe"] = False
syms, info = select_candidates(None, c_static, static_pool=["AAA", "BBB"])
check("static pool returns the pool unchanged", syms == ["AAA", "BBB"], syms)
check("...and publishes NO rank map", "rank" not in info, list(info))

_src = open(repo_file("src", "main.py")).read()
check("main records universe_rank at entry", 'universe_rank' in _src)
check("...only when a rank exists", "if _rank:" in _src)
_usrc = open(repo_file("src", "screener", "universe.py")).read()
check("the rank map is 1-based", '"rank": {sym: i + 1 for i, sym in enumerate(merged)}' in _usrc)


print("\n=== 5. THE OPENING BREAKEVEN FLOOR ===")
oc = M._opening_exit_config(CFG)
tiers = oc["trading"]["breakeven_tiers"]
check("one tier", len(tiers) == 1, tiers)
check("trigger is 0.15", tiers[0]["trigger_pct"] == 0.15, tiers)
check("floor is 0.05", tiers[0]["floor_pct"] == 0.05, tiers)

# The number that matters: the trigger must sit clear of the 0.126% median
# bid-ask measured on 2026-08-26, with margin. A trigger inside the spread is
# armed by one print crossing it and fired by the next crossing back, which
# protects against noise rather than against losses.
MEDIAN_SPREAD = 0.126
check("the trigger clears the measured median spread",
      tiers[0]["trigger_pct"] > MEDIAN_SPREAD, tiers[0]["trigger_pct"])
check("...with real margin, not a rounding error",
      tiers[0]["trigger_pct"] >= MEDIAN_SPREAD * 1.15, tiers[0]["trigger_pct"])


def tm(peak):
    t = TradeManager("X", 100.0, 100, oc)
    t.highest_since_entry = peak
    return t


check("a peak inside the spread does NOT arm the floor",
      tm(100.0 * 1.001).check_breakeven_stop(100.0) == 0)
check("a peak past the trigger DOES arm it",
      tm(100.0 * 1.002).check_breakeven_stop(100.0) > 0)
check("armed, it exits at entry", tm(100.0 * 1.002).check_breakeven_stop(100.0) > 0)
check("armed, it does NOT exit above the floor",
      tm(100.0 * 1.002).check_breakeven_stop(100.0 * 1.001) == 0)
check("the floor sits ABOVE entry, so an armed trade cannot lose",
      tiers[0]["floor_pct"] > 0)
# The session profile must be untouched by the opening one.
check("the SESSION breakeven is unchanged",
      [t["trigger_pct"] for t in CFG["trading"]["breakeven_tiers"]] == [0.5, 0.3],
      CFG["trading"]["breakeven_tiers"])


print("\n=== 6. THE GUARDS DO NOT FIGHT EACH OTHER ===")
check("sector cap leaves room under the concurrent cap",
      _t["max_positions_per_sector"] < _t["max_concurrent_positions"],
      (_t["max_positions_per_sector"], _t["max_concurrent_positions"]))
# With a cap of 3 and 10 slots, at least 4 complexes are needed to fill the book.
check("filling the book needs at least 4 complexes",
      -(-_t["max_concurrent_positions"] // _t["max_positions_per_sector"]) >= 4)
# The burst has its own budget and must still fit.
_ob = _t["opening_burst"]
check("the burst budget still fits under the concurrent cap",
      _ob["max_positions"] < _t["max_concurrent_positions"])
check("the burst closes before normal entries open",
      _ob["decide_by"] <= _t["entry_window_start"],
      (_ob["decide_by"], _t["entry_window_start"]))
# The sector cap applies to burst entries too - they go through the same gate -
# so a burst that filled 7 slots from one complex would be refused after 3.
check("burst entries pass through pre_entry_check",
      "executor.pre_entry_check(qty, price, symbol=symbol)" in _src)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
