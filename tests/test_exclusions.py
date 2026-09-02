"""
Untradeable symbols: leveraged/inverse ETFs and index baskets.

PENDING_WORK.md item 0d bullet 3, open as a theory since 2026-08-21 and
EVIDENCED on 2026-09-01 - SOXL and TQQQ were both watched, and SOXL (a 3x
leveraged semiconductor ETF) was the FIRST opening-burst entry of the day,
inside a 4-trade 0W/4L block. max_stock_price only blocked QQQ at ~$711 by
accident; a leveraged fund at $106 sailed straight through.

Three enforcement layers are tested here, because a filter that exists at
only one of them is the shape of bug this repo keeps re-finding: the
screener (before ranking), the watchlist, and entry time.
"""
import copy
import types
import pytz
import yaml
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.screener.exclusions import is_excluded, filter_symbols, LEVERAGED_ETFS, BASKET_ETFS
from src.screener.stock_screener import StockScreener

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


ON = {"exclude_leveraged_etfs": True, "exclude_basket_etfs": True, "exclude_symbols": []}

print("=== 1. THE 2026-09-01 CASE ===")
ex, why = is_excluded("SOXL", ON)
check("SOXL is refused - the symbol that actually got bought", ex, why)
check("...for the leverage reason, not the basket one", "leveraged" in (why or ""), why)
check("TQQQ is refused too - also on that watchlist", is_excluded("TQQQ", ON)[0])
check("IGV (an unleveraged sector basket) is refused", is_excluded("IGV", ON)[0])
check("a real single name is NOT refused", is_excluded("NVDA", ON)[0] is False)
check("nor is a small-cap single name", is_excluded("RGTI", ON)[0] is False)

print("\n=== 2. INVERSE AND VOLATILITY PRODUCTS ===")
for sym in ("SQQQ", "SPXS", "SOXS", "UVXY", "TZA", "LABD"):
    check(f"{sym} is refused", is_excluded(sym, ON)[0], sym)

print("\n=== 3. THE TWO SWITCHES ARE INDEPENDENT ===")
lev_only = {"exclude_leveraged_etfs": True, "exclude_basket_etfs": False, "exclude_symbols": []}
check("leverage on, baskets off: SOXL still refused", is_excluded("SOXL", lev_only)[0])
check("...but a plain sector ETF is allowed", is_excluded("XLK", lev_only)[0] is False)
basket_only = {"exclude_leveraged_etfs": False, "exclude_basket_etfs": True, "exclude_symbols": []}
check("baskets on, leverage off: XLK refused", is_excluded("XLK", basket_only)[0])
check("...and SOXL allowed (it is only on the leveraged list)",
      is_excluded("SOXL", basket_only)[0] is False)
off = {"exclude_leveraged_etfs": False, "exclude_basket_etfs": False, "exclude_symbols": []}
check("both off -> nothing is excluded", is_excluded("SOXL", off)[0] is False)

print("\n=== 4. exclude_symbols IS ALWAYS HONOURED ===")
explicit = {"exclude_leveraged_etfs": False, "exclude_basket_etfs": False,
            "exclude_symbols": ["PLUG", "gme"]}
check("an explicitly listed name is refused", is_excluded("PLUG", explicit)[0])
check("...case-insensitively", is_excluded("GME", explicit)[0])
check("...and says which rule caught it", "exclude_symbols" in (is_excluded("PLUG", explicit)[1] or ""))
check("an unlisted name still passes", is_excluded("HOOD", explicit)[0] is False)

print("\n=== 5. UNKNOWN SYMBOLS ARE KEPT ===")
check("a symbol on no list is allowed - absence of evidence is not evidence",
      is_excluded("ZZZZ", ON)[0] is False)
check("empty/None is not a crash", is_excluded("", ON)[0] is False and is_excluded(None, ON)[0] is False)

print("\n=== 6. filter_symbols NEVER EMPTIES THE LIST ===")
kept, dropped = filter_symbols(["NVDA", "SOXL", "HOOD"], ON)
check("mixed list drops only the ETF", kept == ["NVDA", "HOOD"], kept)
check("...and reports what it dropped and why", len(dropped) == 1 and dropped[0][0] == "SOXL", dropped)
kept2, dropped2 = filter_symbols(["SOXL", "TQQQ", "SQQQ"], ON)
check("an all-excluded list comes back INTACT rather than empty - watching "
      "nothing guarantees a blank day",
      kept2 == ["SOXL", "TQQQ", "SQQQ"], kept2)
check("...and reports nothing dropped, since nothing was", dropped2 == [], dropped2)

print("\n=== 7. LAYER 1 - THE SCREENER EXCLUDES BEFORE RANKING ===")


class SC(StockScreener):
    def __init__(s, prices, scores, config=None):
        s.broker = types.SimpleNamespace()
        s.et = pytz.timezone("America/New_York")
        s.config = config if config is not None else dict(ON)
        s.candidates = list(prices)
        s._p, s._s = prices, scores
        s.last_scores = {}
        s.last_details = {}

    def score_stock(s, sym):
        return s._s[sym], {"symbol": sym, "price": s._p[sym], "gap_pct": 0, "5day_return_pct": 0,
                           "volume_ratio": 1.0, "volatility_percentile": 50, "atr_pct": 1.5,
                           "volatility_score": 10.0, "opening_hit_rate": 0,
                           "opening_avg_gain": 0, "opening_sessions": 0}


sc = SC({"SOXL": 106.0, "NVDA": 170.0, "HOOD": 106.0, "TQQQ": 90.0, "DASH": 221.0},
        {"SOXL": 99, "NVDA": 60, "HOOD": 58, "TQQQ": 97, "DASH": 55})
picked = sc.screen(top_n=10, min_score=0)
check("SOXL is excluded even with the TOP score", "SOXL" not in picked, picked)
check("TQQQ too", "TQQQ" not in picked, picked)
check("the single names survive", {"NVDA", "HOOD", "DASH"} <= set(picked), picked)
check("an excluded name cannot occupy a stream slot either", "SOXL" not in sc.last_scores)

print("\n=== 8. LAYER 2 - THE WATCHLIST FILTER ===")
watch = ["NVDA", "SOXL", "MDT", "TQQQ", "RGTI"]
out = M._filter_watchlist_by_exclusions(CFG, watch)
check("drops the two ETFs from the watchlist", out == ["NVDA", "MDT", "RGTI"], out)
check("an all-excluded watchlist is kept intact rather than emptied",
      M._filter_watchlist_by_exclusions(CFG, ["SOXL", "TQQQ"]) == ["SOXL", "TQQQ"])
check("runs AFTER the price filter in the selection path",
      open(repo_file("src", "main.py")).read().index("_filter_watchlist_by_price(config, symbols)")
      < open(repo_file("src", "main.py")).read().index("_filter_watchlist_by_exclusions(config, symbols)"))

print("\n=== 9. LAYER 3 - THE ENTRY-TIME BACKSTOP ===")
msrc = open(repo_file("src", "main.py")).read()
check("_attempt_entry checks exclusions before buying",
      "_is_excluded_symbol(config, symbol)" in msrc)
check("...and refuses rather than logging and continuing",
      "entry skipped - {_reason}" in msrc)
check("the check can never raise into the entry path",
      "exclusion check skipped for" in msrc)
check("it sits before the price gate, so the cheaper check runs first",
      msrc.index("_is_excluded_symbol(config, symbol)")
      < msrc.index('min_price = config["trading"].get("min_stock_price")'))

print("\n=== 10. LIVE CONFIG ===")
t = CFG["trading"]
check("leveraged ETFs excluded in the shipped config", t.get("exclude_leveraged_etfs") is True)
check("basket ETFs excluded too", t.get("exclude_basket_etfs") is True)
check("exclude_symbols exists and is a list", isinstance(t.get("exclude_symbols"), list))
check("the static stock_universe contains no excluded names",
      not [s for s in t["stock_universe"] if is_excluded(s, t)[0]],
      [s for s in t["stock_universe"] if is_excluded(s, t)[0]])
# The benchmarks are deliberately ON the basket list - they are measured, never
# traded, and _benchmark_symbols subscribes them separately from the watchlist.
check("SPY is on the basket list (measured, never traded)", "SPY" in BASKET_ETFS)
check("...and the sector benchmarks too", {"XLK", "SMH", "ARKK"} <= BASKET_ETFS)

print("\n=== 11. STREAM COVERAGE: UNIQUE-SYMBOL CAP, TICKS FREE ===")
# 2026-09-01 served 14/26 - all 14 SUBSCRIBED symbols delivered a baseline
# within ~70s, so the gap to 26 was the counting model, not IEX sparsity.
# 29, not 30: one under the free-tier limit on purpose. Sitting exactly AT a
# vendor bound costs the whole session if the bound is exclusive; 29 of 30 is
# still more than double the 14 the old counting model allowed.
check("the cap is 14 - conservative until the boundary is tested live",
      CFG["trading"]["stream_max_subscriptions"] == 14,
      CFG["trading"]["stream_max_subscriptions"])
check("ticks stay ON - free under the corrected model, not a coverage trade",
      CFG["trading"]["use_trade_ticks_for_entry"] is True)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
