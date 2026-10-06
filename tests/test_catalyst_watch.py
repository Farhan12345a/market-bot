"""
trading.catalyst_watch (2026-10-06, ON by default, OBSERVATION ONLY) - see
_fetch_catalyst_counts in src/main.py and AlpacaBroker.get_overnight_news.

From the "stocks in play" conversation: catalyst is the one thing this bot
could never see before this - every existing screener signal is a PRICE/
volume proxy for a catalyst (gap size, RVOL, extension), never the catalyst
itself. Uses Alpaca's own News API (Benzinga-sourced, free with the account
already in use) to count overnight headlines per watchlist symbol, once per
session, and journals the count. Does NOT filter, rank or size anything -
that would be a Tier 1 change needing its own evidence and go-ahead first.

Covers AlpacaBroker.get_overnight_news as a pure aggregation (mocking
Alpaca's NewsClient entirely - no real network call, no real credentials
needed) and _fetch_catalyst_counts' config gating and fail-open behavior.
"""
import copy
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import patch
import yaml
from _repo import REPO, CONFIG, repo_file
import src.main as M
from src.broker.alpaca_broker import AlpacaBroker

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def mk_broker():
    """AlpacaBroker.__new__ bypasses __init__ (which requires real Alpaca
    credentials) - only api_key/api_secret are read by get_overnight_news."""
    b = AlpacaBroker.__new__(AlpacaBroker)
    b.api_key, b.api_secret = "test-key", "test-secret"
    return b


class FakeArticle:
    def __init__(s, symbols): s.symbols = symbols


class FakeNewsSet:
    def __init__(s, articles): s.data = {"news": articles}


print("=== A. AlpacaBroker.get_overnight_news: pure aggregation ===")
b = mk_broker()
fake_articles = [
    FakeArticle(["AAPL", "MSFT"]),
    FakeArticle(["AAPL"]),
    FakeArticle(["GOOG"]),
    FakeArticle(["UNTRACKED"]),   # a symbol not in our watchlist - must not appear
]
with patch("alpaca.data.historical.news.NewsClient") as MockClient:
    MockClient.return_value.get_news.return_value = FakeNewsSet(fake_articles)
    counts = b.get_overnight_news(["AAPL", "MSFT", "GOOG", "TSLA"],
                                  datetime.now() - timedelta(hours=16), datetime.now())

check("AAPL counted across both articles mentioning it", counts.get("AAPL") == 2, counts)
check("MSFT counted once", counts.get("MSFT") == 1, counts)
check("GOOG counted once", counts.get("GOOG") == 1, counts)
check("TSLA (no news) is explicitly 0, not missing - 'checked, found nothing' "
      "must be distinguishable from 'never checked'", counts.get("TSLA") == 0, counts)
check("a symbol mentioned in an article but NOT on our watchlist is dropped, "
      "not invented as a new key", "UNTRACKED" not in counts, counts)
check("every requested symbol has an entry, even with zero articles at all",
      set(counts) == {"AAPL", "MSFT", "GOOG", "TSLA"}, counts)

print("\n=== B. EMPTY WATCHLIST -> trivially {} without a network call ===")
with patch("alpaca.data.historical.news.NewsClient") as MockClient:
    counts_empty = b.get_overnight_news([], datetime.now() - timedelta(hours=16), datetime.now())
check("no symbols -> {} immediately", counts_empty == {})
check("...and the client was never even constructed", MockClient.call_count == 0)

print("\n=== C. ANY FAILURE -> {} , never raises ===")
with patch("alpaca.data.historical.news.NewsClient") as MockClient:
    MockClient.return_value.get_news.side_effect = ConnectionError("simulated outage")
    counts_fail = b.get_overnight_news(["AAPL"], datetime.now() - timedelta(hours=16), datetime.now())
check("a broker/network failure returns {} rather than raising - a "
      "catalyst read must never be able to block the pre-market pipeline "
      "it runs ahead of", counts_fail == {}, counts_fail)

with patch("alpaca.data.historical.news.NewsClient") as MockClient:
    MockClient.return_value.get_news.return_value = types.SimpleNamespace(data={})  # malformed
    counts_malformed = b.get_overnight_news(["AAPL"], datetime.now() - timedelta(hours=16), datetime.now())
check("a response missing the expected 'news' key fails safe to zero counts "
      "(not a traceback) rather than inventing a headline that wasn't there",
      counts_malformed == {"AAPL": 0}, counts_malformed)

print("\n=== D. _fetch_catalyst_counts: config gating ===")
off_cfg = copy.deepcopy(CFG)
off_cfg["trading"]["catalyst_watch"] = {"enabled": False}
calls = []
fake_ex = types.SimpleNamespace(broker=types.SimpleNamespace(
    get_overnight_news=lambda *a, **k: (calls.append(a) or {"AAPL": 5})))
import pytz
ET = pytz.timezone("America/New_York")
result_off = M._fetch_catalyst_counts(off_cfg, fake_ex, ["AAPL"], ET)
check("disabled -> {} immediately, broker never called",
      result_off == {} and calls == [], (result_off, calls))

on_cfg = copy.deepcopy(CFG)
on_cfg["trading"]["catalyst_watch"] = {"enabled": True, "lookback_hours": 16}
calls.clear()
result_on = M._fetch_catalyst_counts(on_cfg, fake_ex, ["AAPL"], ET)
check("enabled -> the broker IS called and its result passed through",
      result_on == {"AAPL": 5}, result_on)
check("...with the watchlist symbols forwarded", calls and calls[0][0] == ["AAPL"], calls)

print("\n=== E. _fetch_catalyst_counts: fail-open on a broker exception ===")
def _raises(*a, **k): raise RuntimeError("boom")
ex_raises = types.SimpleNamespace(broker=types.SimpleNamespace(get_overnight_news=_raises))
result_exc = M._fetch_catalyst_counts(on_cfg, ex_raises, ["AAPL"], ET)
check("a broker exception never propagates out of _fetch_catalyst_counts - "
      "same fail-open contract as every other pre-market step",
      result_exc == {}, result_exc)

print("\n=== F. SHIPPED CONFIG ===")
check("catalyst_watch is enabled in the shipped config (explicit user request)",
      CFG["trading"]["catalyst_watch"]["enabled"] is True)
check("lookback_hours covers overnight + pre-market, not just a few minutes",
      CFG["trading"]["catalyst_watch"]["lookback_hours"] >= 8)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
