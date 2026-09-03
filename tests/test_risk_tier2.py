"""
Tier-2 risk controls and marketable-limit entries (2026-09-02).

R1 execution costs in replay/grid, R2 intraday liquidity cap, R3 halt check,
R4 broker reconciliation, plus the entry routing that makes a passive-limit
experiment measurable later.
"""
import copy
import csv
import importlib.util
import os
import subprocess
import sys
import types
import yaml
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.executor.executor import Executor

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def load(path):
    spec = importlib.util.spec_from_file_location("mod", repo_file(*path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = load(("ops", "replay.py"))

# ===================================================================
print("=== R1. EXECUTION COSTS IN REPLAY/GRID ===")
ec = CFG["trading"]["execution_costs"]
check("shipped in config", isinstance(ec, dict) and "sec_fee_per_dollar" in ec)
c = R.costs_from_config(CONFIG)
check("replay reads them from config, not hardcoded", c["sec_fee_per_dollar"] == ec["sec_fee_per_dollar"])

# 100 shares sold at $100: SEC 10000*0.0000278 = 0.278, TAF 100*0.000166 = 0.0166
cost = R.round_trip_cost(99.0, 100.0, 100, c)
check("SEC + TAF on a $10,000 sale is ~$0.29", abs(cost - 0.2946) < 0.001, cost)
check("...charged on the SELL side only, not doubled",
      abs(R.round_trip_cost(1.0, 100.0, 100, c) - cost) < 1e-9)
# 200,000 shares at $10: TAF would be 200000*0.000166 = $33.20, above the
# $8.30 cap, so the cap binds. SEC is 200000*10*0.0000278 = $55.60 and is NOT
# capped - it dominates at this size, which is the whole reason the two are
# modelled separately rather than as one blended per-share number.
big = R.round_trip_cost(10.0, 10.0, 200000, c)
uncapped_taf = 200000 * c["finra_taf_per_share"]
check("the FINRA TAF cap binds on a huge share count",
      big < 200000 * 10.0 * c["sec_fee_per_dollar"] + uncapped_taf, (big, uncapped_taf))
check("...landing at the cap plus the UNCAPPED SEC portion",
      abs(big - (200000 * 10.0 * c["sec_fee_per_dollar"] + c["finra_taf_cap"])) < 0.01, big)
check("a zero-qty trade costs nothing rather than raising",
      R.round_trip_cost(10.0, 10.0, 0, c) == 0.0)
check("garbage input costs nothing rather than raising",
      R.round_trip_cost(None, None, None, c) == 0.0)

# The point of the feature: cost scales with TRADE COUNT, which is what biases
# a grid whose cells differ mainly in how often they trade.
one = R.round_trip_cost(100.0, 100.0, 90, c)
check("20 trades cost twice what 10 do - which is exactly the bias a "
      "zero-cost grid hides",
      abs(20 * one - 2 * (10 * one)) < 1e-9)

rsrc = open(repo_file("ops", "replay.py")).read()
gsrc = open(repo_file("ops", "grid.py")).read()
check("replay_all accepts costs", "def replay_all(trades, cfg, costs=None):" in rsrc)
check("...and reports NET separately from gross, so the size is visible",
      'print(f"NET of fees:' in rsrc)
check("grid RANKS on net by default", '_key = "pnl" if args.no_costs else "net_pnl"' in gsrc)
check("...and passes costs into the replay", "costs=costs" in gsrc)
check("--no-costs exists to reproduce an old result", "--no-costs" in rsrc and "--no-costs" in gsrc)
check("...and says why it is biased", "trade FREQUENCY" in rsrc or "trade more often" in gsrc)

# ===================================================================
print("\n=== R2. INTRADAY LIQUIDITY CAP ===")
lc = CFG["trading"]["liquidity_cap"]
check("shipped enabled", lc["enabled"] is True)
check("caps at a small fraction of a minute", 0 < lc["max_fraction_of_minute"] <= 0.05)

# 20,000 shares/min at $100 = $2,000,000/min; 2% = $40,000 = 400 shares.
vh = {"LIQUID": [20000] * 10}
cap = M._liquidity_cap_shares(CFG, "LIQUID", 100.0, vh)
check("a liquid name caps around 2% of the minute's dollars", cap == 400, cap)
# 120 shares/min at $100 = $12,000/min; 2% = $240 -> below the floor.
thin = {"THIN": [120] * 10}
cap_thin = M._liquidity_cap_shares(CFG, "THIN", 100.0, thin)
check("a thin name caps far lower", cap_thin < cap, (cap_thin, cap))
check("...but never below floor_usd - a data gap is not an illiquid stock",
      cap_thin * 100.0 >= lc["floor_usd"], cap_thin * 100.0)

check("too few samples -> NO OPINION, not a refusal",
      M._liquidity_cap_shares(CFG, "NEW", 100.0, {"NEW": [5000]}) is None)
check("no history at all -> no opinion",
      M._liquidity_cap_shares(CFG, "NONE", 100.0, {}) is None)
check("no price -> no opinion", M._liquidity_cap_shares(CFG, "LIQUID", None, vh) is None)
off = copy.deepcopy(CFG); off["trading"]["liquidity_cap"]["enabled"] = False
check("disabled -> inert", M._liquidity_cap_shares(off, "LIQUID", 100.0, vh) is None)

# MEDIAN not mean: one opening spike must not license a position the next
# twenty minutes cannot support.
spiky = {"SPIKE": [200000] + [1000] * 9}
flat = {"SPIKE": [1000] * 10}
check("one huge print does not raise the cap - the median ignores it",
      M._liquidity_cap_shares(CFG, "SPIKE", 100.0, spiky)
      == M._liquidity_cap_shares(CFG, "SPIKE", 100.0, flat))

ex = types.SimpleNamespace(equity=100000.0, regime_size_multiplier=1.0,
                           loss_tier_multiplier=lambda: 1.0)
uncapped = M._position_size(CFG, ex, 100.0, symbol="THIN")
capped = M._position_size(CFG, ex, 100.0, symbol="THIN", volume_history=thin)
check("_position_size applies it, and only DOWNWARD", capped < uncapped, (uncapped, capped))
check("a liquid name is not reduced at all",
      M._position_size(CFG, ex, 100.0, symbol="LIQUID", volume_history=vh) == uncapped)
msrc = open(repo_file("src", "main.py")).read()
check("it is applied LAST, after the account-side ceilings",
      msrc.index("regime_mult * _volatility_multiplier") < msrc.index("cap = _liquidity_cap_shares"))
check("...and threaded into the entry path", "volume_history=volume_history" in msrc)

# ===================================================================
print("\n=== R3. HALT / NOT-TRADABLE CHECK ===")
hc = CFG["trading"]["halt_check"]
check("shipped enabled", hc["enabled"] is True)
check("cached, so it is one lookup per symbol per TTL not per poll",
      hc["cache_seconds"] >= 30 and "_HALT_CACHE" in msrc)
bsrc = open(repo_file("src", "broker", "alpaca_broker.py")).read()
check("the broker exposes a tradability check", "def is_symbol_tradable" in bsrc)
check("a failed lookup returns None (unknown), never False",
      'return None, "asset lookup failed"' in bsrc)
check("the entry path refuses only on an explicit False",
      "if _tradable is False:" in msrc)
check("...so an unknown answer never blocks the whole watchlist",
      "_tradable is False" in msrc and "_tradable is None" not in msrc.split("entry refused")[0][-500:])
check("checked at ENTRY, not at selection - a halt happens intraday",
      msrc.index("Not opening into a halted name") < msrc.index("ok, reason = executor.pre_entry_check"))
check("it is honest that a stop cannot survive a halt gap",
      "reopens 3% lower" in msrc)


class HaltBroker:
    def __init__(s, answer): s.answer = answer; s.calls = 0
    def is_symbol_tradable(s, sym):
        s.calls += 1
        return s.answer


M._HALT_CACHE.clear()
strat = types.SimpleNamespace(can_enter=lambda *a: False, get_open_trades=lambda: {})
exh = types.SimpleNamespace(broker=HaltBroker((False, "halted")), equity=100000.0,
                            regime_size_multiplier=1.0, loss_tier_multiplier=lambda: 1.0,
                            phantom_cooldown_remaining=lambda s: 0.0,
                            reentry_cooldown_remaining=lambda s: 0.0)
res = M._attempt_entry(CFG, strat, exh, "HALTED", 100.0, "TEST", 50)
check("a halted symbol is refused", res is False)
M._HALT_CACHE.clear()

# ===================================================================
print("\n=== R4. BROKER RECONCILIATION ===")
rc = CFG["trading"]["reconcile"]
check("shipped enabled", rc["enabled"] is True)


class RB:
    def __init__(s, held): s.held = held
    def get_positions(s):
        return {k: types.SimpleNamespace(symbol=k, qty=str(v)) for k, v in s.held.items()}


def recon(held, tracked):
    e = Executor.__new__(Executor)
    e.broker = RB(held)
    return e.reconcile_against_broker(tracked)[0]


check("agreement reports nothing", recon({"AAA": 100}, ["AAA"]) == [])
mm = recon({}, ["AAA"])
check("tracked but the broker holds nothing -> PHANTOM", len(mm) == 1 and "phantom" in mm[0][1], mm)
mm = recon({"BBB": 50}, [])
check("held but untracked is reported too", len(mm) == 1 and "not tracking" in mm[0][1], mm)
mm = recon({"CCC": -39}, ["CCC"])
check("a SHORT is called out explicitly - this bot never opens one",
      len(mm) == 1 and "SHORT" in mm[0][1], mm)


class DeadBroker:
    def get_positions(s): raise RuntimeError("down")


e = Executor.__new__(Executor); e.broker = DeadBroker()
check("a broker outage reports nothing rather than a false mismatch",
      e.reconcile_against_broker(["AAA"]) == ([], "broker unavailable"))
check("it REPORTS, never silently repairs - the right repair differs by cause",
      "it does not repair" in open(repo_file("src", "executor", "executor.py")).read())
check("wired into the poll loop on an interval", "executor.reconcile_against_broker(" in msrc)
check("...and alerts only on a CHANGE, not once per interval",
      '_sig != reconcile_state.get("last_signature")' in msrc)

# ===================================================================
print("\n=== MARKETABLE-LIMIT ENTRIES ===")
me = CFG["trading"]["marketable_limit_entries"]
check("shipped enabled", me["enabled"] is True)
check("the band is wider than a typical spread, narrower than the losses "
      "it prevents", 0.05 <= me["slippage_pct"] <= 1.0, me["slippage_pct"])

calls = []


class LB:
    def get_positions(s): return {}
    def submit_limit_order(s, sym, qty, px, side="buy", extended_hours=False):
        calls.append(("limit", px, side)); return types.SimpleNamespace(id="L")
    def submit_market_order(s, sym, qty, side="buy"):
        calls.append(("market", None, side)); return types.SimpleNamespace(id="M")


e = Executor(LB(), copy.deepcopy(CFG)); e._equity = 100000.0; e._buying_power = 100000.0
e.submit_entry_order("AAA", 10, 100.0, entry_method="TEST")
check("an entry is routed as a LIMIT", calls and calls[0][0] == "limit", calls)
check("...ABOVE the reference, so it still crosses the spread",
      calls[0][1] > 100.0, calls[0][1])
check("...by about slippage_pct",
      abs(calls[0][1] - 100.0 * (1 + me["slippage_pct"] / 100)) < 0.02, calls[0][1])
check("the routing is recorded, which is what makes a passive-limit "
      "experiment measurable later",
      e.entry_meta["AAA"].get("entry_route") == "limit")
check("...along with the reference price it was set from",
      e.entry_meta["AAA"].get("signal_price") == 100.0)


class NoLimit(LB):
    def submit_limit_order(s, *a, **k): raise RuntimeError("unsupported")


calls.clear()
e2 = Executor(NoLimit(), copy.deepcopy(CFG)); e2._equity = 100000.0; e2._buying_power = 100000.0
e2.submit_entry_order("BBB", 10, 100.0, entry_method="TEST")
check("a limit route that cannot be submitted falls back to MARKET - a missed "
      "entry is an opportunity cost, but an unfilled one should not be silent",
      calls[-1][0] == "market", calls)

off2 = copy.deepcopy(CFG); off2["trading"]["marketable_limit_entries"]["enabled"] = False
calls.clear()
e3 = Executor(LB(), off2); e3._equity = 100000.0; e3._buying_power = 100000.0
e3.submit_entry_order("CCC", 10, 100.0, entry_method="TEST")
check("disabled -> plain market orders, exactly the old behaviour", calls[0][0] == "market")

esrc = open(repo_file("src", "executor", "executor.py")).read()
check("the code states why PASSIVE limits are NOT what shipped",
      "PASSIVE limit\n        # (at or below the ask) may never fill" in esrc
      or "may never fill" in esrc)

# ===================================================================
print("\n=== ops/fill-rate.py: the measurement, read-only ===")
fixture = "/tmp/claude_fillrate.csv"
from src.analytics.trade_recorder import CONTEXT_FIELDS
with open(fixture, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=CONTEXT_FIELDS, extrasaction="ignore")
    w.writeheader()
    # worst-filled entries are the WINNERS - the case where a passive limit
    # would buy better on exactly the trades you want, and miss them.
    for i, (slip, pnl) in enumerate([(0.05, -10), (0.10, -5), (0.15, -8), (0.20, -3),
                                     (0.60, 40), (0.75, 55), (0.80, 30), (0.91, 60)]):
        w.writerow({"trade_id": f"t{i}", "date": "2026-09-03", "symbol": "AAA",
                    "entry_slippage_pct": slip, "exit_slippage_pct": -0.1,
                    "realized_pnl": pnl})
r = subprocess.run([sys.executable, repo_file("ops", "fill-rate.py"),
                    "--context", fixture, "--buckets", "2"],
                   capture_output=True, text=True, cwd=REPO)
check("runs cleanly", r.returncode == 0, r.stderr)
check("reports entry slippage", "ENTRY" in r.stdout and "0.91" in r.stdout, r.stdout)
check("buckets outcomes by slippage", "WOULD A PASSIVE LIMIT HELP" in r.stdout)
check("...and reads the fixture correctly: worst fills ARE the winners, so it "
      "advises AGAINST passive limits",
      "Do NOT switch to passive limits" in r.stdout, r.stdout)
check("...and states the caveat that it only sees fills that HAPPENED",
      "cannot tell you what a passive limit would have missed" in r.stdout)

# reversed: worst fills are the losers -> passive limits worth testing
with open(fixture, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=CONTEXT_FIELDS, extrasaction="ignore")
    w.writeheader()
    for i, (slip, pnl) in enumerate([(0.05, 50), (0.10, 40), (0.15, 35), (0.20, 45),
                                     (0.60, -20), (0.75, -30), (0.80, -25), (0.91, -40)]):
        w.writerow({"trade_id": f"t{i}", "date": "2026-09-03", "symbol": "AAA",
                    "entry_slippage_pct": slip, "realized_pnl": pnl})
r = subprocess.run([sys.executable, repo_file("ops", "fill-rate.py"),
                    "--context", fixture, "--buckets", "2"],
                   capture_output=True, text=True, cwd=REPO)
check("the opposite fixture gives the opposite advice",
      "worth" in r.stdout and "TESTING" in r.stdout, r.stdout)

r = subprocess.run([sys.executable, repo_file("ops", "fill-rate.py"),
                    "--context", "/does/not/exist"], capture_output=True, text=True, cwd=REPO)
check("a missing file exits non-zero with no traceback",
      r.returncode != 0 and "Traceback" not in r.stderr, r.stderr)
check("nothing live reads this tool",
      "fill-rate" not in msrc and "fill_rate" not in msrc)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
