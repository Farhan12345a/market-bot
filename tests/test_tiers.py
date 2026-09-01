"""Tiered take-profit: three tiers, fire-once, highest-wins, retire-below."""
import sys, copy, yaml, types
from datetime import datetime
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.strategy.strategy as S
from src.strategy.strategy import Strategy, TradeManager
from src.executor.executor import is_partial_exit, PARTIAL_EXIT_REASONS, STOP_LOSS_EXIT_REASONS
CFG=yaml.safe_load(open(CONFIG))
S._now_et=lambda: S.ET.localize(datetime(2026,8,25,9,45))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

def mk(qty=300, entry=100.0, cfg=None):
    c=copy.deepcopy(cfg or CFG); t=TradeManager("Z",entry,qty,c)
    st=Strategy(c); st.trades["Z"]=t; t.price_history=[entry]*7
    return st,t
def px(entry,pct): return entry*(1+pct/100)

print("=== A. CONFIG ===")
T=CFG["trading"]["take_profit_tiers"]
check("three tiers configured", len(T)==3, T)
check("tier 1: +1.0% -> 40%", T[0]=={"gain_pct":1.0,"sell_fraction":0.4}, T[0])
check("tier 2: +1.25% -> 30%", T[1]=={"gain_pct":1.25,"sell_fraction":0.3}, T[1])
check("tier 3: +1.5% -> all", T[2]=={"gain_pct":1.5,"sell_fraction":1.0}, T[2])
check("momentum fade over 6 samples", CFG["trading"]["momentum_fade_window_samples"]==6)

print("\n=== B. EACH TIER FIRES AT ITS OWN LEVEL ===")
st,t=mk()
check("nothing at +0.99%", t.check_take_profit(px(100,0.99))==(0,None))
q,r=t.check_take_profit(px(100,1.0))
check("tier1 at exactly +1.0%", q==120 and r=="TAKE_PROFIT_1%", (q,r))
check("40% of 300 = 120 shares", q==120)
st2,t2=mk()
q,r=t2.check_take_profit(px(100,1.25))
check("at +1.25% the HIGHEST qualifying tier fires (tier2, not tier1)", r=="TAKE_PROFIT_1.25%", r)
check("tier2 sells 30% of the original = 90", q==90, q)
st3,t3=mk()
q,r=t3.check_take_profit(px(100,1.5))
check("at +1.5% tier3 fires", r=="TAKE_PROFIT_1.5%", r)
check("tier3 sells ALL remaining", q==300, q)
st4,t4=mk()
q,r=t4.check_take_profit(px(100,5.0))
check("a gap to +5% sells everything, not 33%", q==300 and r=="TAKE_PROFIT_1.5%", (q,r))

print("\n=== C. FULL LADDER, ONE POSITION ===")
st,t=mk(300)
q,r=t.check_take_profit(px(100,1.05)); st.confirm_exit("Z",q,r,px(100,1.05))
check("tier1 fired, 120 sold", q==120 and t.qty_remaining==180, (q,t.qty_remaining))
check("tier1 marked done", 0 in t.take_profit_tiers_done)
check("tier1 will not re-fire", t.check_take_profit(px(100,1.1))==(0,None))
q,r=t.check_take_profit(px(100,1.3)); st.confirm_exit("Z",q,r,px(100,1.3))
check("tier2 fires next, 90 sold", q==90 and r=="TAKE_PROFIT_1.25%", (q,r))
check("90 shares left (300-120-90)", t.qty_remaining==90, t.qty_remaining)
check("tiers 1 and 2 both retired", {0,1} <= t.take_profit_tiers_done)
q,r=t.check_take_profit(px(100,1.6))
check("tier3 sells the remaining 90", q==90 and r=="TAKE_PROFIT_1.5%", (q,r))
st.confirm_exit("Z",q,r,px(100,1.6))
check("position fully closed", "Z" not in st.trades)

print("\n=== D. FIRING A TIER RETIRES EVERYTHING BELOW IT ===")
st,t=mk(300)
q,r=t.check_take_profit(px(100,1.3)); st.confirm_exit("Z",q,r,px(100,1.3))
check("jumped straight to tier2", r=="TAKE_PROFIT_1.25%")
check("tier1 retired without ever firing", 0 in t.take_profit_tiers_done)
check("a pullback to +1.0% does NOT sell again", t.check_take_profit(px(100,1.0))==(0,None))
check("...but tier3 still available on a push up", t.check_take_profit(px(100,1.6))[1]=="TAKE_PROFIT_1.5%")
st,t=mk(300)
q,r=t.check_take_profit(px(100,1.6)); st.confirm_exit("Z",q,r,px(100,1.6))
check("top tier retires all tiers", t.take_profit_tiers_done>={0,1,2})

print("\n=== E. COMMIT-AFTER-CONFIRMATION ===")
st,t=mk(300)
t.check_take_profit(px(100,1.05)); t.check_take_profit(px(100,1.05))
check("pure check does not retire a tier", t.take_profit_tiers_done==set())
check("pure check does not change qty", t.qty_remaining==300)
check("repeat checks still return the same tier", t.check_take_profit(px(100,1.05))[1]=="TAKE_PROFIT_1%")
st,t=mk(300); t.process_exit(100,"MOMENTUM_FADE")
check("other exit reasons never retire tiers", t.take_profit_tiers_done==set())

print("\n=== F. NEVER OVERSELLS ===")
st,t=mk(300)
t.process_exit(280,"FIRST_EXIT_-0.5%")           # only 20 left
q,r=t.check_take_profit(px(100,1.05))
check("tier1 capped at qty_remaining", q==20, q)
st,t=mk(2)
q,r=t.check_take_profit(px(100,1.05))
check("40% of 2 -> 0 shares (truncates), no order emitted", q==0 and r is None, (q,r))
q,r=t.check_take_profit(px(100,1.6))
check("...but tier3 still closes the 2 shares", q==2, q)
st,t=mk(1)
check("1 share: tiers 1-2 emit nothing", t.check_take_profit(px(100,1.3))[0] in (0,), t.check_take_profit(px(100,1.3)))
check("1 share: tier3 sells it", t.check_take_profit(px(100,1.6))[0]==1)

print("\n=== G. PRIORITY AGAINST OTHER RULES ===")
st,t=mk(300)
r=st.check_exit("Z",{"close":99.0})
check("a -1% loss still outranks take-profit", r["reason"]=="FINAL_EXIT_-1.0%")
st,t=mk(300)
r=st.check_exit("Z",{"close":99.5})
check("first exit outranks take-profit on a loser", r["reason"]=="FIRST_EXIT_-0.5%")
st,t=mk(300); t.price_history=[100,100.4,100.8,101.2,101.4,101.6]
r=st.check_exit("Z",{"close":px(100,1.3)})
check("take-profit beats fade/resistance/trailing", r["reason"]=="TAKE_PROFIT_1.25%", r)
check("check_exit carries the tier name through", "1.25" in r["reason"])

print("\n=== H. PARTIAL vs FULL CLASSIFICATION ===")
check("tier1 partial (99 of 300)", is_partial_exit("TAKE_PROFIT_1%",99,300) is True)
check("tier3 is a FULL exit (81 of 81)", is_partial_exit("TAKE_PROFIT_1.5%",81,81) is False)
check("first exit partial", is_partial_exit("FIRST_EXIT_-0.5%",99,300) is True)
check("final exit full", is_partial_exit("FINAL_EXIT_-1.0%",300,300) is False)
check("no quantities -> falls back to the reason", is_partial_exit("TAKE_PROFIT_1%",None,None) is True)
check("no quantities, stop -> full", is_partial_exit("TRAILING_STOP",None,None) is False)
check("garbage quantities -> reason fallback, no raise", is_partial_exit("TAKE_PROFIT_1%","x","y") is True)
check("take-profit is NOT a stop-loss", not any(x.startswith("TAKE_PROFIT") for x in STOP_LOSS_EXIT_REASONS))

print("\n=== I. TOGGLES AND BAD CONFIG ===")
off=copy.deepcopy(CFG); off["trading"]["use_take_profit"]=False
st,t=mk(300,cfg=off)
check("disabled -> never fires", t.check_take_profit(px(100,5.0))==(0,None))
check("disabled -> no exit at +5%", st.check_exit("Z",{"close":px(100,5.0)}) is None)
legacy=copy.deepcopy(CFG); legacy["trading"].pop("take_profit_tiers")
st,t=mk(300,cfg=legacy)
q,r=t.check_take_profit(px(100,1.3))
check("no tiers -> legacy single-tier still works", q==150 and r=="TAKE_PROFIT_1.25%", (q,r))
bad=copy.deepcopy(CFG)
bad["trading"]["take_profit_tiers"]=[{"gain_pct":1.0,"sell_fraction":0.33},
                                     {"gain_pct":"oops"},{"gain_pct":-1,"sell_fraction":0.5},
                                     {"gain_pct":1.5,"sell_fraction":1.0}]
st,t=mk(300,cfg=bad)
check("malformed tiers skipped, valid ones survive", len(t.take_profit_tiers())==2, t.take_profit_tiers())
check("bad config still fires the good tiers", t.check_take_profit(px(100,1.6))[0]==300)
unsorted_=copy.deepcopy(CFG)
unsorted_["trading"]["take_profit_tiers"]=[{"gain_pct":1.5,"sell_fraction":1.0},
                                           {"gain_pct":1.0,"sell_fraction":0.33}]
st,t=mk(300,cfg=unsorted_)
check("tiers sorted regardless of config order", [x["gain_pct"] for x in t.take_profit_tiers()]==[1.0,1.5])
check("...and the highest still wins", t.check_take_profit(px(100,1.6))[1]=="TAKE_PROFIT_1.5%")
empty=copy.deepcopy(CFG); empty["trading"]["take_profit_tiers"]=[]
st,t=mk(300,cfg=empty)
check("empty tier list -> legacy fallback, no crash", t.check_take_profit(px(100,1.3))[0]>0)

print("\n=== J. ENTRY-PRICE REBASE STILL APPLIES ===")
st,t=mk(351,12.135)
st.correct_entry_price("Z",12.2817)
check("no tier fires at the old +1.28% (真 +0.07%)", t.check_take_profit(12.29)==(0,None))
check("tier1 fires at a real +1.0%", t.check_take_profit(12.2817*1.011)[1]=="TAKE_PROFIT_1%")
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
