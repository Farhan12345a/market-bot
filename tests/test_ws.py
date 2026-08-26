"""Full test cycle for the WebSocket migration."""
import sys, time, threading, types
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.data.stream as stream_mod
from src.data.stream import PriceStream, BAR_STALE_AFTER_SECONDS
from src.data.market_data import MarketDataManager

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"   <- {detail}"))
    if not cond: fails.append(name)

def mkbar(sym, close, ts="T"):
    b = types.SimpleNamespace()
    b.symbol, b.open, b.high, b.low, b.close, b.volume, b.timestamp = sym, close-0.1, close+0.1, close-0.2, close, 1000, ts
    return b

import asyncio
def feed(ps, bar):
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(ps._on_bar(bar))

print("=== 1. STREAM CACHE ===")
ps = PriceStream("k", "s")
check("empty stream returns None", ps.get_bar("AAPL") is None)
feed(ps, mkbar("AAPL", 150.0))
b = ps.get_bar("AAPL")
check("bar stored and returned", b is not None and b["close"] == 150.0, b)
check("bar has full OHLCV shape", b and all(k in b for k in ("open","high","low","close","volume","timestamp")))
feed(ps, mkbar("AAPL", 151.0))
check("newer bar overwrites", ps.get_bar("AAPL")["close"] == 151.0)
check("unknown symbol still None", ps.get_bar("MSFT") is None)
check("returned dict is a copy (caller can't corrupt cache)",
      (lambda d: (d.__setitem__("close", 999), ps.get_bar("AAPL")["close"] == 151.0)[1])(ps.get_bar("AAPL")))

print("\n=== 2. STALENESS -> REST FALLBACK ===")
ps._received_at["AAPL"] = time.monotonic() - (BAR_STALE_AFTER_SECONDS + 1)
check("stale bar suppressed", ps.get_bar("AAPL") is None)
ps._received_at["AAPL"] = time.monotonic() - (BAR_STALE_AFTER_SECONDS - 5)
check("just-inside-window bar served", ps.get_bar("AAPL") is not None)

print("\n=== 3. BAD DATA CANNOT KILL THE SOCKET ===")
bad = types.SimpleNamespace(); bad.symbol = "BAD"   # missing every price field
try:
    feed(ps, bad)
    check("malformed bar swallowed, no raise", True)
except Exception as e:
    check("malformed bar swallowed, no raise", False, repr(e))
check("malformed bar not cached", ps.get_bar("BAD") is None)
feed(ps, mkbar("NVDA", 500.0))
check("stream still works after bad bar", ps.get_bar("NVDA")["close"] == 500.0)

print("\n=== 4. MarketDataManager ROUTING ===")
class RestBroker:
    def __init__(self): self.calls = 0
    def get_latest_bars(self, symbol, timeframe="1Min"):
        self.calls += 1
        return {symbol: {"open":1,"high":1,"low":1,"close":99.0,"volume":1,"timestamp":"REST"}}

# 4a: no stream at all -> pure REST (previous behavior preserved)
rb = RestBroker(); md = MarketDataManager(rb)
check("no stream -> REST used", md.get_latest_bar("AAPL")["close"] == 99.0 and rb.calls == 1)

# 4b: stream with fresh data -> stream wins, REST untouched
rb2 = RestBroker(); ps2 = PriceStream("k","s"); feed(ps2, mkbar("AAPL", 150.0))
md2 = MarketDataManager(rb2, stream=ps2)
check("fresh stream bar preferred", md2.get_latest_bar("AAPL")["close"] == 150.0)
check("REST not called when stream hits", rb2.calls == 0, rb2.calls)

# 4c: symbol the stream has never seen -> REST fallback
check("unseen symbol falls back to REST", md2.get_latest_bar("TSLA")["close"] == 99.0)
check("REST called exactly once for fallback", rb2.calls == 1, rb2.calls)

# 4d: stale stream -> REST fallback
ps2._received_at["AAPL"] = time.monotonic() - (BAR_STALE_AFTER_SECONDS + 1)
check("stale stream falls back to REST", md2.get_latest_bar("AAPL")["close"] == 99.0)

# 4e: non-1Min timeframe never uses the stream (it only carries minute bars)
rb3 = RestBroker(); ps3 = PriceStream("k","s"); feed(ps3, mkbar("AAPL", 150.0))
md3 = MarketDataManager(rb3, stream=ps3)
md3.get_latest_bar("AAPL", "5Min")
check("5Min bypasses stream", rb3.calls == 1, rb3.calls)

# 4f: REST raising must not propagate
class Boom:
    def get_latest_bars(self, *a, **k): raise RuntimeError("network down")
md4 = MarketDataManager(Boom(), stream=PriceStream("k","s"))
check("REST failure returns None, no raise", md4.get_latest_bar("AAPL") is None)

st = md2.data_source_stats()
check("stats track both sources", st["stream_hits"] == 1 and st["rest_fallbacks"] == 2, st)

print("\n=== 5. RECONNECT SUPERVISION ===")
attempts = {"n": 0}
class FakeStream:
    def __init__(self, *a, **k): attempts["n"] += 1
    def subscribe_bars(self, handler, *syms): pass
    def run(self):
        if attempts["n"] < 3: raise ConnectionError("dropped")
        time.sleep(0.3)
    def stop(self): pass
fake_mod = types.ModuleType("alpaca.data.live"); fake_mod.StockDataStream = FakeStream
sys.modules["alpaca.data.live"] = fake_mod
orig_delay = stream_mod.RECONNECT_DELAY_SECONDS
stream_mod.RECONNECT_DELAY_SECONDS = 0.05

ps5 = PriceStream("k","s"); ps5.start(["AAPL"])
for _ in range(40):
    time.sleep(0.05)
    if attempts["n"] >= 3: break
check("reconnects after drops", attempts["n"] >= 3, attempts["n"])
ps5.stop(); time.sleep(0.2)
check("stop() halts the supervisor", not ps5._thread.is_alive() or ps5._stop_requested.is_set())
before = attempts["n"]; time.sleep(0.25)
check("no reconnect attempts after stop", attempts["n"] == before, f"{before}->{attempts['n']}")

print("\n=== 6. DOUBLE START GUARDED ===")
attempts["n"] = 0
ps6 = PriceStream("k","s"); ps6.start(["AAPL"]); ps6.start(["AAPL"])
time.sleep(0.2)
check("second start() ignored (no duplicate socket)", attempts["n"] <= 3, attempts["n"])
ps6.stop()
stream_mod.RECONNECT_DELAY_SECONDS = orig_delay

print("\n=== 7. THREAD SAFETY (writer + reader concurrently) ===")
ps7 = PriceStream("k","s")
stop = threading.Event(); errors = []
def writer():
    loop = asyncio.new_event_loop()
    i = 0
    while not stop.is_set():
        try: loop.run_until_complete(ps7._on_bar(mkbar("AAPL", 100.0 + i % 10))); i += 1
        except Exception as e: errors.append(e); break
def reader():
    while not stop.is_set():
        try: ps7.get_bar("AAPL"); ps7.stats()
        except Exception as e: errors.append(e); break
ts = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
[t.start() for t in ts]; time.sleep(0.6); stop.set(); [t.join(timeout=2) for t in ts]
check("no races between stream writer and loop readers", not errors, errors[:2])
check("bars actually flowed during the race test", ps7.stats()["bars_received"] > 100, ps7.stats())

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
