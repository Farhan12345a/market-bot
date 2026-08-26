"""Mixed stream+REST operation, and a hunt for stream failure modes."""
import sys, time, copy, types, threading, logging, yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.data.stream as ST
from src.data.stream import PriceStream, ALPACA_WS_LOGGER
from src.data.market_data import MarketDataManager
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

class Broker:
    def __init__(s): s.calls=[]
    def get_latest_bars(s,sym,tf="1Min"):
        # get_latest_bar's REST path calls THIS, not get_historical_bars.
        s.calls.append(sym)
        from datetime import datetime
        return {sym: {"open":10.0,"high":10.2,"low":9.9,"close":10.1,
                      "volume":1000,"timestamp":datetime(2026,8,24,13,40)}}
    def get_latest_quote(s,sym): return {"bid":9.99,"ask":10.01,"spread":0.02}
def mkstream(syms, trades=True):
    ST._ACTIVE_STREAM=None
    ps=PriceStream("k","s",feed="iex",subscribe_trades=trades,max_subscriptions=28)
    ps._run_forever=lambda: None
    ps.start(syms, priority=syms[:14])
    return ps
def push_bar(ps,sym,close,age=0.0):
    with ps._lock:
        ps._bars[sym]={"open":close,"high":close,"low":close,"close":close,"volume":1,"timestamp":None}
        ps._received_at[sym]=time.monotonic()-age
        ps._bars_received+=1
def push_tick(ps,sym,px,age=0.0):
    with ps._lock:
        ps._last_trade[sym]=px; ps._trade_received_at[sym]=time.monotonic()-age
        ps._trades_received+=1

SYMS=[f"S{i}" for i in range(30)]

print("=== A. MIXED MODE: 14 STREAMED, REST FOR THE REST ===")
ps=mkstream(SYMS); b=Broker(); md=MarketDataManager(b, stream=ps)
check("only the budget is subscribed", len(ps._symbols)==14, len(ps._symbols))
check("the rest are recorded as dropped", len(ps._dropped_symbols)==16, len(ps._dropped_symbols))
for s_ in ps._symbols: push_bar(ps,s_,10.0)
b.calls.clear()
got=[md.get_latest_bar(s_) for s_ in ps._symbols]
check("streamed symbols served WITHOUT any REST call", b.calls==[], b.calls[:4])
check("streamed symbols return a bar", all(g for g in got))
b.calls.clear()
got2=[md.get_latest_bar(s_) for s_ in ps._dropped_symbols]
check("dropped symbols fall through to REST", len(b.calls)==16, len(b.calls))
check("dropped symbols still return a bar (tradeable)", all(g for g in got2))
st=md.data_source_stats()
check("stats count both paths", st["stream_hits"]==14 and st["rest_fallbacks"]==16, st)
check("stream_pct reflects the split", 45 < st["stream_pct"] < 50, st["stream_pct"])

print("\n=== B. STALENESS FALLS BACK PER-SYMBOL ===")
push_bar(ps,"S0",11.0,age=ST.BAR_STALE_AFTER_SECONDS+5)
b.calls.clear(); md.get_latest_bar("S0")
check("stale streamed bar -> REST for THAT symbol", b.calls==["S0"], b.calls)
b.calls.clear(); md.get_latest_bar("S1")
check("...and fresh symbols are unaffected", b.calls==[], b.calls)
push_bar(ps,"S0",11.0,age=0)
b.calls.clear(); md.get_latest_bar("S0")
check("symbol recovers when a fresh bar lands", b.calls==[], b.calls)

print("\n=== C. ENTRY PRICING + SOURCE LABEL ===")
push_tick(ps,"S1",12.34)
check("tick used for entry when fresh", md.get_entry_price("S1",{"close":99}) == 12.34)
check("labelled 'tick'", md.entry_price_source("S1")=="tick")
push_tick(ps,"S2",50.0,age=ST.TRADE_STALE_AFTER_SECONDS+5)
push_bar(ps,"S2",51.0)
check("stale tick ignored, bar close used", md.get_entry_price("S2",{"close":51.0})==51.0)
check("labelled 'stream bar' (live, just not a tick)", md.entry_price_source("S2")=="stream bar")
d0=ps._dropped_symbols[0]
check("unstreamed symbol prices from the bar", md.get_entry_price(d0,{"close":7.5})==7.5)
check("labelled 'REST' - the delayed path", md.entry_price_source(d0)=="REST", md.entry_price_source(d0))
check("unknown symbol -> 'unknown', no raise", md.entry_price_source("NEVER_SEEN")=="unknown")

print("\n=== D. STREAM DEATH MID-SESSION ===")
ps2=mkstream(SYMS); md2=MarketDataManager(Broker(), stream=ps2)
for s_ in ps2._symbols: push_bar(ps2,s_,10.0)
check("healthy while bars flow", ps2.is_healthy() is True)
ps2._gave_up=True; ps2.stop()
check("after give-up, is_healthy False", ps2.is_healthy() is False)
bb=Broker(); md3=MarketDataManager(bb, stream=ps2)
bb.calls.clear(); r=md3.get_latest_bar(ps2._symbols[0])
check("dead stream still returns a price", r is not None)
# A cached bar under 180s old is FRESHER than REST's ~15-min delay, so it is
# still served; the symbol falls back on its own once it ages out.
check("recent cached bar still preferred over delayed REST", bb.calls==[], bb.calls)
push_bar(ps2, ps2._symbols[0], 10.0, age=ST.BAR_STALE_AFTER_SECONDS+5)
bb.calls.clear(); md3.get_latest_bar(ps2._symbols[0])
check("once the cache ages out, everything goes to REST", bb.calls==[ps2._symbols[0]], bb.calls)
ps2b=mkstream(["S0"]); push_bar(ps2b,"S0",99.0); ps2b.stop()
ps2b._run_forever=lambda: None; ps2b.start(["S0"])
check("a new session does not inherit the previous one's bars", ps2b.get_bar("S0") is None)
check("...nor its ticks", ps2b.get_last_trade_price("S0") is None)
ps2b.stop()
check("entry pricing survives a dead stream", md3.get_entry_price(ps2._symbols[0],{"close":5.0})==5.0)

print("\n=== E. NO-STREAM MODE IS UNCHANGED ===")
b4=Broker(); md4=MarketDataManager(b4, stream=None)
check("no stream -> bar returned", md4.get_latest_bar("X") is not None)
check("no stream -> REST used", b4.calls==["X"])
check("no stream -> entry from bar", md4.get_entry_price("X",{"close":3.0})==3.0)
check("no stream -> labelled REST", md4.entry_price_source("X")=="REST")

print("\n=== F. HOSTILE / MALFORMED DATA ===")
ps3=mkstream(["A","B"]); md5=MarketDataManager(Broker(), stream=ps3)
import asyncio
bad=[types.SimpleNamespace(symbol="A",open="x",high=1,low=1,close=1,volume=1,timestamp=None),
     types.SimpleNamespace(symbol="A",open=None,high=None,low=None,close=None,volume=None,timestamp=None),
     types.SimpleNamespace()]
for bo in bad:
    asyncio.get_event_loop().run_until_complete(ps3._on_bar(bo))
check("malformed bars never raise into the socket", True)
check("malformed bars are not stored", ps3.get_bar("A") is None, ps3.get_bar("A"))
for t_ in [types.SimpleNamespace(symbol="A",price="abc"),
           types.SimpleNamespace(symbol="A",price=0),
           types.SimpleNamespace(symbol="A",price=-5),
           types.SimpleNamespace()]:
    asyncio.get_event_loop().run_until_complete(ps3._on_trade(t_))
check("bad/zero/negative ticks rejected", ps3.get_last_trade_price("A") is None)
asyncio.get_event_loop().run_until_complete(ps3._on_trade(types.SimpleNamespace(symbol="A",price=9.5)))
check("a good tick after bad ones still lands", ps3.get_last_trade_price("A")==9.5)

print("\n=== G. THREAD SAFETY ===")
ps4=mkstream([f"T{i}" for i in range(14)]); md6=MarketDataManager(Broker(), stream=ps4)
stop=threading.Event(); errors=[]
def writer():
    i=0
    while not stop.is_set():
        try: push_bar(ps4,f"T{i%14}",10+i%5); push_tick(ps4,f"T{i%14}",10+i%5)
        except Exception as e: errors.append(e)
        i+=1
def reader():
    while not stop.is_set():
        try:
            md6.get_latest_bar("T3"); md6.get_entry_price("T3",{"close":1}); ps4.stats()
        except Exception as e: errors.append(e)
th=[threading.Thread(target=writer),threading.Thread(target=reader),threading.Thread(target=reader)]
[t.start() for t in th]; time.sleep(1.5); stop.set(); [t.join(3) for t in th]
check("no races between the socket thread and the trading loop", errors==[], errors[:2])
check("bars actually flowed during the race", ps4._bars_received>50, ps4._bars_received)

print("\n=== H. FAILURE MODES NOT YET SEEN LIVE ===")
ps5=mkstream(SYMS)
lg=logging.getLogger(ALPACA_WS_LOGGER)
lg.error("error: auth failed")
for _ in range(40):
    if ps5._gave_up: break
    time.sleep(0.1)
check("auth failure -> named, immediate give-up", ps5._gave_up is True)
ps5.stop()
ps6=mkstream(SYMS)
lg.error("error: insufficient subscription")
for _ in range(40):
    if ps6._gave_up: break
    time.sleep(0.1)
check("wrong data plan -> named give-up", ps6._gave_up is True)
ps6.stop()
ps7=PriceStream("k","s",feed="sip_typo",subscribe_trades=False,max_subscriptions=28)
check("unknown feed name falls back to iex rather than crashing",
      ps7._resolve_feed().value=="iex", ps7._resolve_feed())
ps8=mkstream([]) ; check("empty symbol list -> no crash", ps8._symbols==[]); ps8.stop()
ps9=mkstream(["A"]); ps9.stop(); ps9.stop()
check("double stop is idempotent", True)
ps10=mkstream(["A"]); ps10.stop()
check("get_bar after stop -> None, no raise", ps10.get_bar("A") is None)
check("get_last_trade_price after stop -> None", ps10.get_last_trade_price("A") is None)
check("stats after stop -> dict, no raise", isinstance(ps10.stats(), dict))
ps11=mkstream(SYMS)
check("subscribed count never exceeds the budget", len(ps11._symbols)<=14)
check("kept + dropped == everything requested",
      set(ps11._symbols)|set(ps11._dropped_symbols)==set(SYMS))
ps11.stop()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
