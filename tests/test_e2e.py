"""End-to-end: full run_trading_day driven by the WebSocket cache, verifying
the stream integration AND that every earlier fix still holds."""
import sys, types, asyncio
from datetime import datetime, timedelta
import pytz
from _repo import REPO, CONFIG, repo_file, sandbox_cwd

import src.main as m
import src.strategy.strategy as S

# Pin the strategy clock. Since the ET/UTC fix, TradeManager reads real wall
# time, so momentum_fade_hour made these tests pass before 10:15 ET and fail
# after it. Freeze at 09:40 ET so behavior is time-of-day independent.
_PINNED = datetime.now(pytz.timezone("America/New_York")).replace(hour=9, minute=40, second=0, microsecond=0)
S._now_et = lambda: _PINNED
from src.strategy.strategy import Strategy
from src.executor.executor import Executor
from src.data.market_data import MarketDataManager
from src.data.stream import PriceStream

ET = pytz.timezone("America/New_York")
fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"   <- {detail}"))
    if not cond: fails.append(name)

def mkbar(sym, close, ts=None):
    b = types.SimpleNamespace()
    b.symbol, b.open, b.high, b.low, b.close, b.volume = sym, close*0.999, close*1.001, close*0.998, close, 1e5
    b.timestamp = ts if ts is not None else datetime.now(ET)
    return b

LOOP = asyncio.new_event_loop()
def feed(ps, sym, px, ts=None): LOOP.run_until_complete(ps._on_bar(mkbar(sym, px, ts)))

class Acct:
    equity="100000"; buying_power="200000"; cash="100000"; last_equity="100000"
class Pos:
    def __init__(s,sym,q,px): s.symbol,s.qty,s.market_value,s.avg_entry_price,s.current_price,s.unrealized_pl=sym,str(q),str(q*px),str(px),str(px),"0"
class Broker:
    def __init__(s): s.real={}; s.orders=[]
    def submit_market_order(s,sym,qty,side="buy"):
        s.orders.append((sym,qty,side))
        if side=="buy": s.real[sym]=qty
        else: s.real.pop(sym,None)
        return types.SimpleNamespace(id=f"o{len(s.orders)}")
    def get_account(s): return Acct()
    def get_positions(s): return {k:Pos(k,v,50.0) for k,v in s.real.items()}
    def get_latest_bars(s,sym,tf="1Min"):
        raise AssertionError(f"REST called for {sym} - stream should have served it")

CFG = {"trading":{
    "entry_window_start":"09:30","entry_window_end":"09:55","entry_check_interval_seconds":0,
    "rapid_increase_pct":0.5,"rapid_increase_lookback_minutes":5,
    "use_rsi_filter":False,"rsi_period":14,"rsi_max_for_entry":50,
    "use_pullback_entry":False,"use_three_bar_momentum":True,"three_bar_require_acceleration":True,
    "use_opening_reversal_entry":False,"opening_reversal_window_minutes":5,
    "opening_reversal_drop_bars":5,"opening_reversal_confirm_bars":5,
    "final_exit_loss_pct":-1.0,"first_exit_loss_pct":-0.5,"first_exit_pct":0.33,
    "trailing_stop_pct":0.75,"time_stop_hour":16,
    "momentum_fade_hour":10,"momentum_fade_minute":15,"momentum_fade_window_samples":5,
    "momentum_fade_slope_threshold":-0.05,
    "resistance_lookback_samples":3,"resistance_min_decline_pct":0.3,
    "max_position_per_stock_usd":10000,"max_daily_loss_usd":100000,
    "max_concurrent_positions":10,"max_total_exposure_fraction":0.9,
    "max_risk_per_trade_fraction":0.005,"max_daily_entries":25,
    "reentry_cooldown_minutes":20,"reentry_cooldown_after_loss_only":True,
}}

class FakeDT:
    """Drives run_trading_day's clock forward through the session."""
    cur = None
    @classmethod
    def now(cls, tz=None): return cls.cur

def run_session(symbol_prices, cfg=CFG, minutes=14):
    """symbol_prices: {sym: [price per minute]}. Returns (broker, strategy, executor, md)."""
    syms = list(symbol_prices)
    broker = Broker(); strat = Strategy(cfg); ex = Executor(broker, cfg)
    ps = PriceStream("k","s"); md = MarketDataManager(broker, stream=ps)

    start = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    step = {"i": 0}
    real_bar = md.get_latest_bar
    def timed_bar(sym, tf="1Min"):
        i = min(step["i"], len(symbol_prices[sym]) - 1)
        feed(ps, sym, symbol_prices[sym][i], ts=start + timedelta(minutes=step["i"]))
        return real_bar(sym, tf)
    md.get_latest_bar = timed_bar

    orig_dt, orig_sleep = m.datetime, m.time.sleep
    m.datetime = FakeDT
    def fake_sleep(_):
        step["i"] += 1
        FakeDT.cur = start + timedelta(minutes=step["i"])
        if step["i"] > minutes: raise KeyboardInterrupt
    m.time.sleep = fake_sleep
    FakeDT.cur = start
    try:
        m.run_trading_day(cfg, md, strat, ex, syms, {}, types.SimpleNamespace(send_daily_summary=lambda *a,**k: None), ET)
    except KeyboardInterrupt:
        pass
    finally:
        m.datetime, m.time.sleep = orig_dt, orig_sleep
    return broker, strat, ex, md

print("=== 8. STREAM SERVES A FULL TRADING DAY (REST would assert) ===")
rise = [50.0, 50.1, 50.4, 50.9, 51.5, 52.2, 53.0, 53.9, 54.9, 56.0, 57.2, 58.5, 59.9, 61.4, 63.0]
b, s, e, md = run_session({"AAA": rise})
buys = [o for o in b.orders if o[2] == "buy"]
check("entry taken using streamed bars only", len(buys) >= 1, b.orders[:3])
st = md.data_source_stats()
check("all reads served by stream, zero REST", st["rest_fallbacks"] == 0 and st["stream_hits"] > 0, st)

print("\n=== 9. EARLIER FIXES STILL HOLD THROUGH THE STREAM ===")
# concurrent cap: 30 symbols all ripping at once
many = {f"S{i}": rise for i in range(30)}
b2, s2, e2, md2 = run_session(many, minutes=6)
opened = len([o for o in b2.orders if o[2] == "buy"])
check("max_concurrent_positions still capped at 10", opened <= 10, opened)

# daily entry cap
cfg3 = {"trading": dict(CFG["trading"], max_concurrent_positions=100,
                        max_total_exposure_fraction=50.0, max_daily_entries=4)}
b3, s3, e3, _ = run_session({f"T{i}": rise for i in range(20)}, cfg=cfg3, minutes=6)
check("max_daily_entries capped at 4", len([o for o in b3.orders if o[2]=="buy"]) <= 4,
      len([o for o in b3.orders if o[2]=="buy"]))

# resistance floor: tiny wobble off the peak must NOT exit
wobble = [50.0, 50.1, 50.4, 50.9, 51.5, 52.2, 53.0, 53.9, 54.9, 56.0,
          56.0, 55.99, 55.98, 55.97, 55.96]   # ~0.07% drift down from peak
b4, s4, e4, _ = run_session({"WOB": wobble})
res_exits = [o for o in b4.orders if o[2] == "sell"]
check("0.07% drift does NOT trigger resistance exit", len(res_exits) == 0, res_exits)

# resistance floor: a real decline SHOULD exit
realdrop = [50.0, 50.1, 50.4, 50.9, 51.5, 52.2, 53.0, 53.9, 54.9, 56.0,
            56.0, 55.6, 55.2, 54.8, 54.4]     # ~2.9% off the peak
b5, s5, e5, _ = run_session({"DRP": realdrop})
check("2.9% decline DOES exit", any(o[2] == "sell" for o in b5.orders), b5.orders)

print("\n=== 10. STREAM OUTAGE MID-SESSION -> REST TAKES OVER ===")
class RestOK(Broker):
    def get_latest_bars(s, sym, tf="1Min"):
        return {sym: {"open":50,"high":50,"low":50,"close":50.0,"volume":1,"timestamp":"REST"}}
broker6 = RestOK(); strat6 = Strategy(CFG); ex6 = Executor(broker6, CFG)
ps6 = PriceStream("k","s"); md6 = MarketDataManager(broker6, stream=ps6)
feed(ps6, "AAA", 50.0, ts=datetime.now(ET))
check("stream serves while fresh", md6.get_latest_bar("AAA")["timestamp"] != "REST")
import time as _t
ps6._received_at["AAA"] = _t.monotonic() - 9999          # simulate the socket going silent
check("goes to REST once stream stalls", md6.get_latest_bar("AAA")["timestamp"] == "REST")
feed(ps6, "AAA", 51.0, ts=datetime.now(ET))                                    # socket recovers
check("returns to stream after recovery", md6.get_latest_bar("AAA")["timestamp"] != "REST")

print("\n=== 11. HEALTH SIGNAL DISTINGUISHES CONNECTED FROM WORKING ===")
ps7 = PriceStream("k","s")
ps7._connected = True                                     # run() entered, but 403-looping inside
check("connected-but-silent reports unhealthy", ps7.stats()["connected"] and not ps7.stats()["healthy"])
feed(ps7, "AAA", 10.0, ts=datetime.now(ET))
check("healthy once a bar lands", ps7.is_healthy())
ps7._received_at["AAA"] = _t.monotonic() - 9999
check("unhealthy again once bars stop", not ps7.is_healthy())

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
