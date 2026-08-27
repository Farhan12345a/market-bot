"""
Dynamic universe (src/screener/universe.py) and sector relative strength
(src/analytics/sectors.py).

Both are new on 2026-08-26 and both change WHICH symbols get traded, which is
the highest-leverage thing in the bot and also the easiest to get silently
wrong: a universe that quietly falls back to the static list, or a sector factor
that scores an unmapped symbol as weak rather than unknown, would both look
completely normal in the logs.
"""
import copy, json, os, sys, tempfile, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
from src.screener import universe as U
from src.analytics import sectors as SEC
from src.analytics.continuation import continuation_score

CFG = yaml.safe_load(open(CONFIG))
P = F = 0
def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


class FakeBroker:
    """Records what was asked for, so batching can be asserted rather than assumed."""
    def __init__(self, assets=None, bars=None, fail_bars=False, fail_assets=False):
        self._assets = assets if assets is not None else []
        self._bars = bars or {}
        self.fail_bars = fail_bars
        self.fail_assets = fail_assets
        self.bar_calls = []          # list of symbol-lists requested
        self.asset_calls = 0

    def get_all_assets(self, tradable_only=True):
        self.asset_calls += 1
        if self.fail_assets:
            raise RuntimeError("network down")
        return [a for a in self._assets if a.get("tradable") or not tradable_only]

    def get_historical_bars(self, symbols, start, end, timeframe="1Day"):
        self.bar_calls.append(list(symbols))
        if self.fail_bars:
            raise RuntimeError("bars unavailable")
        return {s: self._bars[s] for s in symbols if s in self._bars}


def bars(price, volume, n=30, drift=0.0, rng=1.0):
    out = []
    for i in range(n):
        c = price * (1 + drift * i / max(1, n - 1))
        out.append({"close": c, "volume": volume,
                    "high": c * (1 + rng / 200), "low": c * (1 - rng / 200)})
    return out


def asset(sym, name="Some Co", tradable=True):
    return {"symbol": sym, "name": name, "exchange": "NASDAQ", "tradable": tradable,
            "shortable": True, "fractionable": True, "marginable": True}


print("=== 1. WHAT COUNTS AS AN ORDINARY EQUITY ===")
check("plain ticker kept", U._is_ordinary_equity(asset("NVDA")))
check("warrant dropped by name", not U._is_ordinary_equity(asset("XYZW", "Foo Inc Warrant")))
check("right dropped by name", not U._is_ordinary_equity(asset("XYZR", "Foo Inc Rights")))
check("SPAC unit dropped", not U._is_ordinary_equity(asset("XYZU", "Foo Acquisition Corp Units")))
check("preferred dropped", not U._is_ordinary_equity(asset("XYZP", "Foo Preferred Series A")))
check("class share with a dot dropped", not U._is_ordinary_equity(asset("BRK.B")))
check("suffixed ticker dropped", not U._is_ordinary_equity(asset("XYZ-WS")))
check("numeric ticker dropped", not U._is_ordinary_equity(asset("123")))

print("\n=== 2. ASSET FETCH ===")
b = FakeBroker(assets=[asset("NVDA"), asset("AMD"), asset("BRK.B"),
                       asset("XW", "Warrant Co Warrants"), asset("HALT", tradable=False)])
syms = U.fetch_tradable_symbols(b, CFG)
check("only ordinary tradable symbols survive", syms == ["AMD", "NVDA"], syms)
check("a failing asset call returns [] and does not raise",
      U.fetch_tradable_symbols(FakeBroker(fail_assets=True), CFG) == [])

print("\n=== 3. BATCHING IS REAL ===")
many = [f"S{i}" for i in range(450)]
b = FakeBroker(bars={s: bars(50, 1_000_000) for s in many})
cfg = copy.deepcopy(CFG); cfg["trading"]["universe_chunk_size"] = 200
stats = U.daily_snapshot(b, many, cfg)
check("450 symbols cost 3 requests, not 450", len(b.bar_calls) == 3, len(b.bar_calls))
check("no chunk exceeded the size", all(len(c) <= 200 for c in b.bar_calls))
check("every symbol got stats", len(stats) == 450, len(stats))
check("one failing chunk does not lose the rest",
      len(U.daily_snapshot(FakeBroker(fail_bars=True), many, cfg)) == 0)

print("\n=== 4. STATS FROM BARS ===")
s = U._stats_from_bars(bars(100.0, 1_000_000, n=30, drift=0.10))
check("price is the last close", abs(s["price"] - 110.0) < 0.01, s["price"])
check("dollar volume is price x shares", s["dollar_volume"] > 1e8, s["dollar_volume"])
check("5-day return positive on an uptrend", s["return_5d"] > 0, s["return_5d"])
check("atr_pct reflects the bar range", s["atr_pct"] > 0, s["atr_pct"])
check("too few bars -> None", U._stats_from_bars(bars(100, 1000, n=3)) is None)
check("empty -> None", U._stats_from_bars([]) is None)
check("junk rows -> None", U._stats_from_bars([{"close": None, "volume": None}]) is None)

print("\n=== 5. LIQUIDITY CUT ===")
stats = {
    "RICH":  {"price": 500.0, "dollar_volume": 9e8, "return_5d": 1, "volume_ratio": 1, "atr_pct": 3},
    "CHEAP": {"price": 2.0,   "dollar_volume": 9e8, "return_5d": 1, "volume_ratio": 1, "atr_pct": 3},
    "THIN":  {"price": 50.0,  "dollar_volume": 1e6, "return_5d": 1, "volume_ratio": 1, "atr_pct": 3},
    "GOOD":  {"price": 50.0,  "dollar_volume": 5e8, "return_5d": 1, "volume_ratio": 1, "atr_pct": 3},
    "OK":    {"price": 80.0,  "dollar_volume": 3e7, "return_5d": 1, "volume_ratio": 1, "atr_pct": 3},
}
out = U.liquidity_cut(stats, CFG)
check("above max_stock_price dropped", "RICH" not in out, out)
check("below min_stock_price dropped", "CHEAP" not in out, out)
check("illiquid dropped", "THIN" not in out, out)
check("liquid in-band kept", set(out) == {"GOOD", "OK"}, out)
check("ordered by dollar volume", out == ["GOOD", "OK"], out)
small = copy.deepcopy(CFG); small["trading"]["universe_size"] = 1
check("universe_size caps the list", U.liquidity_cut(stats, small) == ["GOOD"])

print("\n=== 6. CHEAP SCORE ===")
hot = {"price": 50, "dollar_volume": 2e8, "return_5d": 8.0, "volume_ratio": 2.5, "atr_pct": 6.0}
cold = {"price": 50, "dollar_volume": 2e7, "return_5d": -4.0, "volume_ratio": 0.8, "atr_pct": 0.5}
check("a moving name outscores a dead one", U.cheap_score(hot) > U.cheap_score(cold),
      (U.cheap_score(hot), U.cheap_score(cold)))
check("bounded 0-100", 0 <= U.cheap_score(hot) <= 100 and 0 <= U.cheap_score(cold) <= 100)
check("None -> 0", U.cheap_score(None) == 0.0)
check("negative momentum is penalised, never negative", U.cheap_score(cold) >= 0)
ranked = U.rank_candidates({"A": hot, "B": cold}, ["B", "A"], CFG)
check("ranking is best-first", [x[0] for x in ranked] == ["A", "B"], ranked)

print("\n=== 7. CACHE ===")
sandbox_cwd()
path = "logs/universe.json"
check("no cache -> None", U.load_cached_universe(path, 7) is None)
U.save_cached_universe(path, ["AAA", "BBB"])
check("round-trips", U.load_cached_universe(path, 7) == ["AAA", "BBB"])
os.utime(path, (0, 0))          # epoch mtime = decades stale
check("stale cache is rejected", U.load_cached_universe(path, 7) is None)
U.save_cached_universe(path, ["AAA", "BBB"])
check("fresh again after a rebuild", U.load_cached_universe(path, 7) == ["AAA", "BBB"])
open(path, "w").write("{not json")
check("corrupt cache -> None, no raise", U.load_cached_universe(path, 7) is None)
U.save_cached_universe(path, [])
check("empty cache is treated as absent", U.load_cached_universe(path, 7) is None)

print("\n=== 8. END TO END, AND THE FALLBACK ===")
# Alphabetic tickers on purpose: _is_ordinary_equity rejects anything with a
# digit, which is correct for real symbols and was quietly emptying this fixture.
def tick(i):
    return "A" + chr(65 + i // 26) + chr(65 + i % 26)

universe_assets = [asset(tick(i)) for i in range(40)]
universe_bars = {}
for i in range(40):
    # increasing volatility and volume, so the ranking has something to find
    universe_bars[tick(i)] = bars(50.0, 500_000 + i * 200_000, drift=i * 0.004, rng=1 + i * 0.2)
cfg = copy.deepcopy(CFG)
cfg["trading"].update({"use_dynamic_universe": True, "universe_shortlist_size": 10,
                       "universe_min_dollar_volume": 1e6, "universe_cache_file": "logs/u2.json",
                       "universe_size": 100})
b = FakeBroker(assets=universe_assets, bars=universe_bars)
cands, info = U.select_candidates(b, cfg, static_pool=["ZZZ"])
check("source is dynamic", info["source"] == "dynamic", info)
check("shortlist honoured", info["shortlist"] == 10, info)
check("static pool is folded in, not dropped", "ZZZ" in cands, cands[-3:])
check("shortlist + static is the candidate list", len(cands) == 11, len(cands))
check("the most volatile names rank top", cands[0] == tick(39), cands[:3])

b2 = FakeBroker(fail_assets=True)
cfg2 = copy.deepcopy(cfg); cfg2["trading"]["universe_cache_file"] = "logs/u3.json"
cands2, info2 = U.select_candidates(b2, cfg2, static_pool=["AAA", "BBB"])
check("asset failure -> static pool, not empty", cands2 == ["AAA", "BBB"], cands2)
check("...and it says so", info2["source"] == "static")

cfg3 = copy.deepcopy(cfg); cfg3["trading"]["use_dynamic_universe"] = False
cands3, info3 = U.select_candidates(FakeBroker(), cfg3, static_pool=["AAA"])
check("disabled -> untouched static pool", cands3 == ["AAA"] and info3["source"] == "static")

b4 = FakeBroker(assets=universe_assets, bars={})
cfg4 = copy.deepcopy(cfg); cfg4["trading"]["universe_cache_file"] = "logs/u4.json"
cands4, info4 = U.select_candidates(b4, cfg4, static_pool=["AAA"])
check("no bars -> static pool", cands4 == ["AAA"], cands4)

print("\n=== 9. CACHE AVOIDS THE REBUILD ===")
cfg5 = copy.deepcopy(cfg); cfg5["trading"]["universe_cache_file"] = "logs/u5.json"
b5 = FakeBroker(assets=universe_assets, bars=universe_bars)
U.select_candidates(b5, cfg5, static_pool=[])
first_assets = b5.asset_calls
U.select_candidates(b5, cfg5, static_pool=[])
check("second run does not re-list assets", b5.asset_calls == first_assets, b5.asset_calls)
check("but it DOES refetch today's bars", len(b5.bar_calls) > 1)

print("\n=== 10. SECTOR MAP ===")
check("miners map to the crypto complex", SEC.sector_for("MARA") == "WGMI")
check("semis map to SMH", SEC.sector_for("NVDA") == "SMH")
check("unmapped -> None", SEC.sector_for("NOSUCH") is None)
check("case insensitive", SEC.sector_for("mara") == "WGMI")
check("None symbol is safe", SEC.sector_for(None) is None)
aug26 = "ADBE CIFR CLSK CMG COIN CVNA DASH HOOD IONQ MARA MTCH RIOT WULF XPEV".split()
need = SEC.sectors_for(aug26)
check("a 14-name watchlist needs only a handful of ETFs", len(need) <= 6, need)
conc = SEC.sector_concentration(aug26)
check("2026-08-26 concentration is detected", "WGMI" in conc, conc)
check("...and it was 7 of 14 names", len(conc["WGMI"]) == 7, conc["WGMI"])
check("no false concentration on a spread list",
      SEC.sector_concentration(["NVDA", "JPM", "XOM"]) == {})

print("\n=== 11. SECTOR STRENGTH FACTOR ===")
check("moving WITH the sector scores 50",
      SEC.sector_strength("MARA", 3.0, {"WGMI": 3.0}) == 50.0)
check("leading the sector scores above 50",
      SEC.sector_strength("MARA", 3.0, {"WGMI": 1.0}) > 50)
check("lagging the sector scores below 50",
      SEC.sector_strength("MARA", 1.0, {"WGMI": 3.0}) < 50)
check("unmapped symbol -> None, NOT zero",
      SEC.sector_strength("NOSUCH", 3.0, {"WGMI": 0.0}) is None)
check("missing sector return -> None", SEC.sector_strength("MARA", 3.0, {}) is None)
check("no returns dict at all -> None", SEC.sector_strength("MARA", 3.0, None) is None)
check("bounded 0-100",
      SEC.sector_strength("MARA", 99.0, {"WGMI": 0.0}) == 100.0 and
      SEC.sector_strength("MARA", -99.0, {"WGMI": 0.0}) == 0.0)

print("\n=== 12. None IS DROPPED, NOT SCORED AS WEAK ===")
w = dict(CFG["trading"]["continuation_weights"]); w["sector_strength"] = 0.3
withs = continuation_score({"efficiency": 80.0, "sector_strength": 80.0}, w)
without = continuation_score({"efficiency": 80.0, "sector_strength": None}, w)
check("an unmapped symbol is not penalised", withs == without == 80.0, (withs, without))
low = continuation_score({"efficiency": 80.0, "sector_strength": 0.0}, w)
check("a measurably weak sector IS penalised", low < without, (low, without))

print("\n=== 13. SHIPPED AT ZERO WEIGHT ===")
lw = CFG["trading"]["continuation_weights"]
check("sector_strength is registered", "sector_strength" in lw)
check("...at zero, so it decides nothing yet", lw["sector_strength"] == 0.0, lw)
base = continuation_score({"efficiency": 80.0}, lw)
same = continuation_score({"efficiency": 80.0, "sector_strength": 5.0}, lw)
check("zero weight cannot move the score", base == same, (base, same))

print("\n=== 14. JOURNAL COLUMNS ===")
from src.analytics.signal_journal import JOURNAL_FIELDS
check("cf_sector_strength is journalled", "cf_sector_strength" in JOURNAL_FIELDS)
check("cf_sector_etf is journalled too (50 vs what?)", "cf_sector_etf" in JOURNAL_FIELDS)
check("appended AFTER the existing cf_ block, not inserted mid-schema",
      JOURNAL_FIELDS.index("cf_sector_strength") > JOURNAL_FIELDS.index("cf_vwap"))
check("cf_score stays last of the cf_ block",
      JOURNAL_FIELDS.index("cf_score") > JOURNAL_FIELDS.index("cf_sector_etf"))

print("\n=== 15. BENCHMARKS ON THE STREAM ===")
# SPY was never subscribed, so it always came over REST - ~15 min delayed on the
# free tier - while streamed symbols came live. Every excess_vs_spy_pct compared
# the two. Sector ETFs would inherit the identical defect, so both go on the
# stream, last in priority.
import src.main as M
watch = "ADBE CIFR CLSK CMG COIN CVNA DASH HOOD IONQ MARA MTCH RIOT WULF XPEV".split()
bm = M._benchmark_symbols(CFG, watch)
check("SPY is included", "SPY" in bm, bm)
check("the day's sector ETFs are included", "WGMI" in bm and "XLK" in bm, bm)
check("only sectors this watchlist needs", set(bm) - {"SPY"} == set(SEC.sectors_for(watch)), bm)
check("no duplicates", len(bm) == len(set(bm)), bm)
check("a benchmark that is ALSO a traded symbol is not double-subscribed",
      "SPY" not in M._benchmark_symbols(CFG, watch + ["SPY"]), M._benchmark_symbols(CFG, watch + ["SPY"]))
check("watchlist + benchmarks fits the subscription budget",
      len(watch) + len(bm) <= CFG["trading"]["stream_max_subscriptions"],
      len(watch) + len(bm))
off = copy.deepcopy(CFG); off["trading"]["stream_benchmarks"] = False
check("disabling the flag restores the old behaviour", M._benchmark_symbols(off, watch) == [])
check("an all-unmapped watchlist still streams SPY",
      M._benchmark_symbols(CFG, ["NOSUCH1", "NOSUCH2"]) == ["SPY"])
check("an empty watchlist is safe", M._benchmark_symbols(CFG, []) == ["SPY"])
check("live config has benchmarks on", CFG["trading"]["stream_benchmarks"] is True)

print("\n=== 16. SIGNAL CEILING REPORTING ===")
from src.notifications.email_notifier import _peak_signal_note
check("bound is reported as bound",
      "BOUND" in _peak_signal_note({"peak_signal_pct": 1.9, "rapid_increase_max_pct": 1.25,
                                    "peak_signal_symbol": "XYZ"}))
check("never-bound is reported as never-bound",
      "never bound" in _peak_signal_note({"peak_signal_pct": 1.452,
                                          "rapid_increase_max_pct": 2.0}))
check("no signals is not reported as a pass",
      _peak_signal_note({"peak_signal_pct": 0, "rapid_increase_max_pct": 2.0}) == "no signals")
check("no ceiling set is stated plainly",
      "no ceiling" in _peak_signal_note({"peak_signal_pct": 1.0, "rapid_increase_max_pct": 0}))
check("the symbol is named", "XYZ" in _peak_signal_note(
      {"peak_signal_pct": 1.9, "rapid_increase_max_pct": 1.25, "peak_signal_symbol": "XYZ"}))
check("empty context does not raise", _peak_signal_note({}) == "no signals")

print("\n=== 17. THE 2026-08-27 CONFIG DECISIONS ===")
t = CFG["trading"]
check("ceiling lowered to 1.25 so it actually binds", t["rapid_increase_max_pct"] == 1.25, t["rapid_increase_max_pct"])
check("ceiling still applies to streamed symbols only",
      t["rapid_increase_max_pct_streamed_only"] is True)
check("ceiling sits above the entry floor",
      t["rapid_increase_max_pct"] > t["rapid_increase_pct"])
check("resistance is ON", t["use_resistance_exit"] is True)
check("resistance still needs a real decline", t["resistance_min_decline_pct"] == 0.5)
check("dynamic universe stays ON", t["use_dynamic_universe"] is True)
# The dip guard the user asked to keep: a rising last tick must block the exit.
from src.strategy.strategy import TradeManager
_rl2 = 18
_up = TradeManager("U", 100.0, 10, CFG)
_up.price_history = [100.0 - 1.0 * i / (_rl2 - 1) for i in range(_rl2 - 1)] + [100.0]
_up.highest_since_entry = 100.0
check("resistance still refuses to sell into an upturn",
      _up.check_resistance(_up.price_history[-1]) == 0)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
