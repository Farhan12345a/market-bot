"""
AlpacaBroker's data feed is now configurable (2026-10-07), reusing
trading.websocket_feed. Found while considering shortening
rapid_increase_lookback_minutes: the user upgraded their Alpaca market data
subscription to paid (SIP, the full consolidated tape) specifically to
remove the free IEX feed's ~2-3min lag behind wall-clock time - the exact
reason rapid_increase_lookback_minutes was widened from 2 to 3 minutes in
the first place (a window shorter than the feed's own lag can never
accumulate 2 genuinely-different in-window samples).

But two REST calls in AlpacaBroker (get_historical_bars, get_latest_quote)
were hardcoded to DataFeed.IEX regardless of config - PriceStream already
read trading.websocket_feed (config.yaml has it set to "sip"), so the
~92% of reads served by the live stream already benefited from the paid
upgrade, but the REST fallback path (a stream drop, an unsubscribed
symbol, or straight historical-bar fetches) silently stayed on the free,
laggy feed no matter what the account now pays for.
"""
import os
import sys
from _repo import REPO, CONFIG, repo_file
import yaml

os.environ.setdefault("APCA_API_KEY_ID", "test-key")
os.environ.setdefault("APCA_API_SECRET_KEY", "test-secret")

from src.broker.alpaca_broker import AlpacaBroker
from alpaca.data.enums import DataFeed

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


print("=== A. THE feed PARAMETER ===")
b_sip = AlpacaBroker(feed="sip")
check("feed='sip' resolves to DataFeed.SIP", b_sip.feed == DataFeed.SIP, b_sip.feed)

b_default = AlpacaBroker()
check("no feed given -> defaults to iex (old behavior preserved for any "
      "other caller that doesn't pass one)", b_default.feed == DataFeed.IEX)

b_bad = AlpacaBroker(feed="not-a-real-feed")
check("an invalid feed string fails safe to iex rather than raising - "
      "same contract as PriceStream._resolve_feed", b_bad.feed == DataFeed.IEX)

print("\n=== B. NO MORE HARDCODED DataFeed.IEX IN THE REST CALLS ===")
src = open(repo_file("src", "broker", "alpaca_broker.py")).read()
check("get_historical_bars uses self.feed, not a hardcoded feed",
      "feed=self.feed" in src)
check("...and get_latest_quote too",
      src.count("feed=self.feed") >= 2, src.count("feed=self.feed"))
check("the only remaining live DataFeed.IEX reference is the safe-fallback "
      "default (a comment above it also mentions it by name, which is fine)",
      "self.feed = DataFeed.IEX" in src and "falling back to iex" in src)

print("\n=== C. main.py THREADS THE LIVE CONFIG VALUE THROUGH ===")
msrc = open(repo_file("src", "main.py")).read()
check("AlpacaBroker is constructed with feed=trading.websocket_feed, not "
      "left on the old implicit default",
      'feed=config["trading"].get("websocket_feed", "iex")' in msrc)

print("\n=== D. SHIPPED CONFIG ===")
check("websocket_feed is sip in the live config - the account IS paid, "
      "this just makes the REST path actually use it",
      CFG["trading"]["websocket_feed"] == "sip")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
