"""Entry-price rebase + earnings beat/gap filter + Monday config."""
import sys, copy, yaml, types
from datetime import datetime
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import pytz
import src.strategy.strategy as S
from src.strategy.strategy import Strategy, TradeManager
from src.executor.executor import Executor
import src.screener.list_builder as LB
CFG=yaml.safe_load(open(CONFIG))
FIXED=S.ET.localize(datetime(2026,8,24,9,45)); S._now_et=lambda: FIXED
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

def mk(entry=100.0, qty=100, cfg=None):
    c=copy.deepcopy(cfg or CFG); t=TradeManager("Z",entry,qty,c)
    st=Strategy(c); st.trades["Z"]=t; t.price_history=[entry]*5
    return st,t

print("=== A. THE MARA BUG ===")
st,t = mk(12.135, 351)
check("entry starts at the signal price", t.entry_price==12.135)
check("BEFORE rebase: 12.29 looks like +1.28%", t.check_take_profit(12.29)[0] > 0)
st.correct_entry_price("Z", 12.2817)
check("entry rebased to the fill", abs(t.entry_price-12.2817)<1e-9, t.entry_price)
check("AFTER rebase: 12.29 is only +0.07%, no take-profit", t.check_take_profit(12.29)[0]==0)
check("take-profit still fires at a real +1.3%", t.check_take_profit(12.2817*1.013)[0] > 0)

print("\n=== B. EVERY EXIT RULE REBASES, NOT JUST TAKE-PROFIT ===")
st2,t2 = mk(100.0)
st2.correct_entry_price("Z", 100.42)          # +0.42%, the day's mean adverse slippage
# 99.40 is -1.01% from the 100.42 fill but only -0.60% from the 100.00 signal
# price: before the rebase the stop sat 0.42% too low and fired late every time.
check("final exit now measures from the fill",
      t2.check_final_exit(99.40)>0 and t2.check_final_exit(99.60)==0,
      (t2.check_final_exit(99.40), t2.check_final_exit(99.60)))
check("...that same price was NOT a stop against the signal price",
      (99.40-100.0)/100.0*100 > -1.0)
st3,t3 = mk(100.0)
st3.correct_entry_price("Z", 100.42)
check("first exit measures from the fill", t3.check_first_exit(99.85)>0, t3.check_first_exit(99.85))
check("first exit does not fire above it", t3.check_first_exit(100.10)==0)

print("\n=== C. REBASE SAFETY ===")
st4,t4 = mk(100.0)
check("unknown symbol -> False, no raise", st4.correct_entry_price("NOPE", 101.0) is False)
check("non-numeric -> False", st4.correct_entry_price("Z","abc") is False)
check("None -> False", st4.correct_entry_price("Z",None) is False)
check("zero/negative -> False", st4.correct_entry_price("Z",0) is False and st4.correct_entry_price("Z",-5) is False)
check("price unchanged after bad input", t4.entry_price==100.0)
check("identical price -> no-op False", st4.correct_entry_price("Z",100.0) is False)
st5,t5 = mk(100.0); t5.highest_since_entry=100.0; t5.lowest_since_entry=100.0; t5.highest_price=100.0
st5.correct_entry_price("Z", 101.0)
check("peak rebased up to the fill", t5.highest_since_entry==101.0)
check("trailing high rebased too", t5.highest_price==101.0)
check("trough left alone when fill is higher", t5.lowest_since_entry==100.0)
st6,t6 = mk(100.0); t6.highest_since_entry=105.0
st6.correct_entry_price("Z", 101.0)
check("an observed peak above the fill is NOT lowered", t6.highest_since_entry==105.0)

print("\n=== D. EXECUTOR -> STRATEGY WIRING ===")
ex=Executor(types.SimpleNamespace(), CFG)
check("callback defaults to None (executor usable alone)", ex.on_entry_price_corrected is None)
src=open(repo_file("src", "executor", "executor.py")).read()
check("reconciliation invokes the callback", "self.on_entry_price_corrected(symbol, actual)" in src)
check("callback failure cannot break reconciliation", "Could not rebase" in src)
msrc=open(repo_file("src", "main.py")).read()
check("main wires it to the strategy", "executor.on_entry_price_corrected = strategy.correct_entry_price" in msrc)
# compare against the CALL site, not the def, which appears earlier in the file
check("wired BEFORE positions are reconciled at startup",
      msrc.index("executor.on_entry_price_corrected = strategy.correct_entry_price")
      < msrc.index("        reconcile_existing_positions(broker"))

print("\n=== E. EARNINGS: SURPRISE PARSING ===")
f=LB._earnings_surprise
check("plain surprise", f({"surprise":"12.5"})==12.5)
check("negative surprise", f({"surprise":"-8.2"})==-8.2)
check("parenthesised negative", f({"surprise":"(8.2)"})==-8.2)
check("percent sign stripped", f({"surprise":"5%"})==5.0)
check("N/A -> None", f({"surprise":"N/A"}) is None)
check("empty -> None", f({"surprise":""}) is None)
check("missing -> None", f({}) is None)
check("falls back to eps vs forecast", round(f({"eps":"$1.10","epsForecast":"$1.00"}),1)==10.0)
check("fallback handles a miss", round(f({"eps":"$0.90","epsForecast":"$1.00"}),1)==-10.0)
check("zero forecast -> None, no divide-by-zero", f({"eps":"1.0","epsForecast":"0"}) is None)

print("\n=== F. EARNINGS FILTER ===")
class FS:
    et=pytz.timezone("America/New_York")
    def __init__(s,gaps): s.g=gaps
    def _get_recent_gap(s,x): return s.g.get(x,0.0)
gaps={"BEAT_SMALL":1.2,"BEAT_BIG":6.68,"MISS":0.5,"UNKNOWN":0.8,"BEAT_EDGE":3.0}
sur={"BEAT_SMALL":10.0,"BEAT_BIG":15.0,"MISS":-5.0,"UNKNOWN":None,"BEAT_EDGE":2.0}
why={k:"today BMO" for k in gaps}
out=LB._filter_earnings_candidates(FS(gaps), list(gaps), why, sur, CFG)
check("a beat with a small gap is kept", "BEAT_SMALL" in out, out)
check("a MISS is dropped (long-only)", "MISS" not in out, out)
check("an already-gapped beat is dropped", "BEAT_BIG" not in out, out)
# earnings_require_known_beat went ON 2026-08-24 (PDD had lost $86.56 on a
# $119.38 day) and back OFF on 2026-08-26, when it dropped 15 of 15 candidates -
# not a filter but a total veto. This asserts the LIVE behaviour; the flag is
# covered both ways, independently of the live config, in test_aug25.
check("unknown surprise KEPT under the live config", "UNKNOWN" in out, out)
_off = copy.deepcopy(CFG); _off["trading"]["earnings_require_known_beat"] = False
check("...and kept again when that flag is off",
      "UNKNOWN" in LB._filter_earnings_candidates(FS(gaps), list(gaps), why, sur, _off))
check("gap exactly at the cap is kept", "BEAT_EDGE" in out, out)
nb=copy.deepcopy(CFG); nb["trading"]["earnings_require_beat"]=False
check("require_beat off -> misses come back",
      "MISS" in LB._filter_earnings_candidates(FS(gaps),list(gaps),why,sur,nb))
ng=copy.deepcopy(CFG); ng["trading"]["earnings_max_gap_pct"]=0
check("max_gap 0 disables the gap filter",
      "BEAT_BIG" in LB._filter_earnings_candidates(FS(gaps),list(gaps),why,sur,ng))
class Broken(FS):
    def _get_recent_gap(s,x): raise RuntimeError("down")
check("gap lookup failure -> symbol kept, no raise",
      len(LB._filter_earnings_candidates(Broken({}),["A"],{"A":"x"},{"A":5.0},CFG))==1)
check("empty input -> empty, no raise", LB._filter_earnings_candidates(FS({}),[],{},{},CFG)==[])
check("the 2026-08-21 losers would now be blocked",
      "BEAT_BIG" not in out and gaps["BEAT_BIG"]==6.68)

print("\n=== G. MONDAY CONFIG ===")
t_=CFG["trading"]
for k,v in [("entry_window_start","09:33"),("take_profit_pct",1.25),
            ("resistance_min_decline_pct",0.5),("earnings_list_top_n",3),
            ("earnings_require_beat",True),("earnings_max_gap_pct",3.0),
            ("reentry_cooldown_minutes",5),("stream_max_subscriptions",28)]:
    check(f"{k} = {v}", t_[k]==v, t_[k])
check("burst_max_entries still 3 (raise AFTER ranking is fixed)", t_["burst_max_entries"]==3, t_["burst_max_entries"])
check("entry window still opens after the bell", t_["entry_window_start"] > "09:30")
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
