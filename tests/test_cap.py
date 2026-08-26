"""Stream subscription cap + priority ordering."""
import sys, yaml, types, time
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.data.stream as ST
from src.data.stream import PriceStream

CFG = yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

def mk(trades=False, cap=30):
    ps = PriceStream("k","s",feed="iex",subscribe_trades=trades,max_subscriptions=cap)
    ps._run_forever = lambda: None          # never actually connect
    ps._watch_for_silence = lambda: None
    return ps

print("=== A. BUDGET ===")
check("bars only, cap 30 -> 30 symbols", mk(False,30).symbol_budget()==30)
check("bars+trades, cap 30 -> 15 symbols", mk(True,30).symbol_budget()==15)
check("cap 1 -> at least 1 (never 0)", mk(True,1).symbol_budget()==1)
check("cap 0 -> still 1, never subscribes nothing", mk(True,0).symbol_budget()==1)
check("larger cap scales", mk(False,200).symbol_budget()==200)

print("\n=== B. THE 2026-08-21 CASE ===")
today = ["HOOD","MRVL","CMG","CADL","COIN","AMZN","QQQ","ADBE"] + \
        [f"D{i}" for i in range(48)] + ["BJ","BEKE","BKE"]
check("reproduces the 59-symbol watchlist", len(today)==59, len(today))
ps = mk(True,30)
ps.start(today, priority=["HOOD","MRVL","CMG","CADL","COIN","AMZN","QQQ","ADBE","BJ","BEKE","BKE"])
check("subscribes within budget (was 59)", len(ps._symbols)==15, len(ps._symbols))
check("all screener picks streamed", {"HOOD","MRVL","CMG","COIN","AMZN","ADBE"} <= set(ps._symbols))
check("all earnings adds streamed", {"BJ","BEKE","BKE"} <= set(ps._symbols), ps._symbols)
check("priority names come first", ps._symbols[:11]==["HOOD","MRVL","CMG","CADL","COIN","AMZN","QQQ","ADBE","BJ","BEKE","BKE"], ps._symbols[:11])
check("static universe fills the remainder", all(s.startswith("D") for s in ps._symbols[11:]), ps._symbols[11:])
check("dropped list recorded", len(ps._dropped_symbols)==44, len(ps._dropped_symbols))
check("nothing lost - kept + dropped == requested",
      set(ps._symbols)|set(ps._dropped_symbols)==set(today))

print("\n=== C. NO CAP NEEDED ===")
ps2 = mk(True,30); ps2.start(["A","B","C"], priority=["C"])
check("under budget -> everything subscribed", ps2._symbols==["A","B","C"], ps2._symbols)
check("under budget -> original order preserved (no needless reorder)", ps2._symbols[0]=="A")
check("under budget -> nothing dropped", ps2._dropped_symbols==[])
ps3 = mk(False,30); ps3.start([f"S{i}" for i in range(30)])
check("exactly at budget -> all kept", len(ps3._symbols)==30)
ps4 = mk(False,30); ps4.start([f"S{i}" for i in range(31)])
check("one over budget -> exactly one dropped", len(ps4._dropped_symbols)==1)

print("\n=== D. EDGE CASES ===")
ps5 = mk(True,30); ps5.start([])
check("empty watchlist -> no crash", ps5._symbols==[])
ps6 = mk(True,30); ps6.start(["A","A","B","B","C"])
check("duplicates collapsed before counting", ps6._symbols==["A","B","C"], ps6._symbols)
ps7 = mk(True,30); ps7.start([f"S{i}" for i in range(40)], priority=["NOTLISTED","S39"])
check("priority names not in the watchlist are ignored", "NOTLISTED" not in ps7._symbols)
check("priority name that IS present is promoted", ps7._symbols[0]=="S39", ps7._symbols[:3])
ps8 = mk(True,30); ps8.start([f"S{i}" for i in range(40)])
check("no priority given -> falls back to list order", ps8._symbols[0]=="S0")
ps9 = mk(True,30); ps9.start([f"S{i}" for i in range(40)], priority=[f"S{i}" for i in range(40)])
check("priority longer than budget -> truncated, no crash", len(ps9._symbols)==15)

print("\n=== E. CONFIG ===")
t=CFG["trading"]
check("stream_max_subscriptions present", "stream_max_subscriptions" in t)
# 28, not 30: deliberately one symbol under the free-tier limit. Being exactly
# AT a limit costs the whole session's stream if the vendor's bound turns out to
# be exclusive, and costs one symbol if it does not.
check("cap sits at or under the free-tier limit", 0 < t["stream_max_subscriptions"] <= 30, t.get("stream_max_subscriptions"))
check("cap leaves headroom under the limit", t["stream_max_subscriptions"] < 30, t.get("stream_max_subscriptions"))
check("trade ticks still on", t["use_trade_ticks_for_entry"] is True)
budget = t["stream_max_subscriptions"] // (2 if t["use_trade_ticks_for_entry"] else 1)
check(f"live config yields {budget} streamed symbols", budget >= 1, budget)
check("default constant matches the free tier", ST.DEFAULT_MAX_SUBSCRIPTIONS==30)
ps10 = PriceStream("k","s")
check("default construction uses the cap", ps10._max_subscriptions==30)

print("\n=== F. MAIN WIRING ===")
src=open(repo_file("src", "main.py")).read()
check("max_subscriptions passed from config", "stream_max_subscriptions" in src)
check("priority passed to start()", "priority=stream_priority" in src)
check("stream priority is ordered by SCORE, not list position", "sorted(symbols, key=lambda s_: ranked.get(s_, 0), reverse=True)" in src)
check("earnings adds appended to the priority", "stream_priority[\"symbols\"] + added" in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
