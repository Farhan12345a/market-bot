"""
The volatility term becomes a TRUE percentile (PENDING_WORK.md item 0d, open
since 2026-08-21, fixed 2026-09-02).

_get_volatility_percentile returned one of five hardcoded values
(10/30/50/75/95) by ATR band, while being the joint-heaviest term in
score_stock at 20 points. On 2026-08-21 three earnings candidates all landed
on an IDENTICAL 26.2 for it, so the gap term silently decided the entire
ranking. The real ATR% was already being computed one line earlier and thrown
away into a bucket.

screen() now ranks those raw ATR% values against each other after every
candidate is scored, and adjusts each score by the difference between the
band value and the percentile value.
"""
import types
import pytz
from _repo import REPO, CONFIG, repo_file
from src.screener.stock_screener import StockScreener, MIN_CANDIDATES_FOR_TRUE_PERCENTILE

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


class SC(StockScreener):
    """Drives screen() with controlled ATR% values, bypassing the network.

    score_stock is stubbed to contribute ONLY the volatility term, so any
    difference in the final score is attributable to the ranking and nothing
    else.
    """
    def __init__(self, atrs, prices=None):
        self.broker = types.SimpleNamespace()
        self.et = pytz.timezone("America/New_York")
        self.config = {}
        self.candidates = list(atrs)
        self._atrs = atrs
        self._prices = prices or {s: 100.0 for s in atrs}
        self.last_scores = {}
        self.last_details = {}

    def _get_volatility_percentile(self, symbol):
        atr_pct = self._atrs[symbol]
        if not hasattr(self, "_atr_pct"):
            self._atr_pct = {}
        self._atr_pct[symbol] = float(atr_pct)
        # the same five bands the real implementation falls back to
        if atr_pct < 0.5: return 10
        elif atr_pct < 1.0: return 30
        elif atr_pct < 1.5: return 50
        elif atr_pct < 2.5: return 75
        else: return 95

    def score_stock(self, symbol):
        vol = self._get_volatility_percentile(symbol)
        # screen() logs every selected row, so the stub has to carry the
        # fields that log line formats - a stub that is narrower than the
        # real return value fails inside the logger, not in the assertion.
        details = {"symbol": symbol, "price": self._prices[symbol],
                   "volatility_percentile": vol, "volatility_score": vol * 0.2,
                   "atr_pct": self._atr_pct.get(symbol),
                   "gap_pct": 0.0, "5day_return_pct": 0.0, "volume_ratio": 1.0,
                   "opening_hit_rate": 0, "opening_avg_gain": 0.0,
                   "opening_sessions": 0}
        return vol * 0.2, details


print("=== 1. THE 2026-08-21 CASE: THREE NAMES IN ONE BAND NOW SEPARATE ===")
# All three sit in the 1.5-2.5% band -> identical 75 -> identical 15.0 before.
sc = SC({"A": 1.6, "B": 2.0, "C": 2.4, "D": 0.4, "E": 3.0})
sc.screen(top_n=10, min_score=0)
vols = {s: sc.last_details[s]["volatility_percentile"] for s in "ABC"}
check("the three formerly-identical names now hold three different values",
      len(set(vols.values())) == 3, vols)
check("...and they order by actual ATR%", vols["A"] < vols["B"] < vols["C"], vols)
check("every ranked row is marked as such",
      all(sc.last_details[s].get("volatility_ranked") for s in "ABCDE"))

print("\n=== 2. THE PERCENTILES ARE REAL PERCENTILES ===")
# 5 candidates, ATRs 0.4 < 1.6 < 2.0 < 2.4 < 3.0. bisect_right/n * 100:
#   D(0.4)=20, A(1.6)=40, B(2.0)=60, C(2.4)=80, E(3.0)=100
expect = {"D": 20.0, "A": 40.0, "B": 60.0, "C": 80.0, "E": 100.0}
got = {s: sc.last_details[s]["volatility_percentile"] for s in expect}
check("percentile = share of the candidate set at or below this ATR", got == expect, got)
check("the lowest-ATR name is no longer floored at the 10-band",
      got["D"] == 20.0 and sc.last_details["D"]["volatility_score"] == 4.0)
check("the highest tops out at 100, not 95", got["E"] == 100.0)

print("\n=== 3. THE SCORE IS ADJUSTED BY THE DIFFERENCE, NOT DOUBLE-COUNTED ===")
# Each stub score is purely the volatility term, so score == percentile * 0.2.
for sym, pctile in expect.items():
    check(f"{sym}: score reflects the ranked value ({pctile} * 0.2)",
          abs(sc.last_scores[sym] - pctile * 0.2) < 1e-9,
          (sc.last_scores[sym], pctile * 0.2))

print("\n=== 4. TIES SHARE A RANK ===")
sc2 = SC({"T1": 2.0, "T2": 2.0, "T3": 0.5, "T4": 0.9, "T5": 3.0})
sc2.screen(top_n=10, min_score=0)
check("two identical ATRs get identical percentiles",
      sc2.last_details["T1"]["volatility_percentile"]
      == sc2.last_details["T2"]["volatility_percentile"],
      (sc2.last_details["T1"], sc2.last_details["T2"]))

print("\n=== 5. TOO FEW CANDIDATES -> KEEP THE BANDS, DON'T FAKE A DISTRIBUTION ===")
sc3 = SC({"X": 1.6, "Y": 2.0})
sc3.screen(top_n=10, min_score=0)
check(f"under {MIN_CANDIDATES_FOR_TRUE_PERCENTILE} candidates the band value survives",
      sc3.last_details["X"]["volatility_percentile"] == 75
      and sc3.last_details["Y"]["volatility_percentile"] == 75,
      {s: sc3.last_details[s]["volatility_percentile"] for s in ("X", "Y")})
check("...and they are NOT marked as ranked",
      not sc3.last_details["X"].get("volatility_ranked"))

print("\n=== 6. AN UNMEASURABLE ATR IS NOT TREATED AS 'NOT VOLATILE' ===")
sc4 = SC({"A": 1.6, "B": 2.0, "C": 2.4, "D": 0.4, "E": 3.0})
sc4.screen(top_n=10, min_score=0)
before = sc4.last_details["C"]["volatility_percentile"]
# Now blank one out the way a failed ATR call would, and re-rank.
scores = dict(sc4.last_scores)
details = dict(sc4.last_details)
details["C"] = dict(details["C"]); details["C"]["atr_pct"] = None
details["C"]["volatility_percentile"] = 75; details["C"]["volatility_score"] = 15.0
sc4._rank_volatility_percentiles(scores, details)
check("a symbol with no ATR keeps its band value rather than ranking last",
      details["C"]["volatility_percentile"] == 75, details["C"])
check("...and the others still rank among themselves",
      details["E"]["volatility_percentile"] == 100.0, details["E"])

print("\n=== 7. THE RAW ATR% IS ACTUALLY KEPT NOW ===")
src = open(repo_file("src", "screener", "stock_screener.py")).read()
check("the real implementation records atr_pct instead of discarding it",
      "self._atr_pct[symbol] = float(atr_pct)" in src)
check("score_stock passes it through to details", 'details["atr_pct"]' in src)
check("screen() ranks before it sorts",
      src.index("_rank_volatility_percentiles(scores, details_dict)")
      < src.index("sorted_stocks = sorted(scores.items()"))
check("the five bands survive as the documented fallback",
      "Fallback bands" in src and "atr_pct < 0.5" in src)

print(f"\n{P} passed, {F} failed")
import sys
sys.exit(1 if F else 0)
