"""Breakeven floor + poll-rate-invariant exit windows."""
import sys, copy, yaml
from datetime import datetime
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.strategy.strategy as S
from src.strategy.strategy import Strategy, TradeManager, _samples_for_minutes
CFG=yaml.safe_load(open(CONFIG))
S._now_et=lambda: S.ET.localize(datetime(2026,8,26,9,45))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")
def mk(cfg=None,qty=100,entry=100.0):
    c=copy.deepcopy(cfg or CFG); t=TradeManager("Z",entry,qty,c)
    st=Strategy(c); st.trades["Z"]=t
    t.price_history=[entry]*40
    return st,t
def px(p): return 100.0*(1+p/100)

print("=== A. ARMING ===")
st,t=mk()
check("not armed before +0.5%", t.check_breakeven_stop(px(-0.3))==0)
# Tiers landed 2026-08-26. Under the live config a peak of +0.49% now DOES arm,
# via the 0.30 tier - that is the point of the second tier. The mechanism "a
# peak below every trigger does not arm" is tested against an explicit
# single-tier config below, so it no longer breaks when the live tiers change.
t.highest_since_entry=px(0.49)
check("peak +0.49% arms under the live tiers (0.30 tier)", t.check_breakeven_stop(px(-0.3))>0)
_ONE=copy.deepcopy(CFG); _ONE["trading"]["breakeven_tiers"]=[{"trigger_pct":0.5,"floor_pct":0.05}]
_s1,_t1=mk(_ONE); _t1.highest_since_entry=px(0.49)
check("peak +0.49% NOT armed with a single 0.5 tier", _t1.check_breakeven_stop(px(-0.3))==0)
_s2,_t2=mk(); _t2.highest_since_entry=px(0.29)
check("peak +0.29% NOT armed - below even the 0.30 tier",
      _t2.check_breakeven_stop(px(-0.3))==0)
t.highest_since_entry=px(0.5)
check("peak +0.5% arms it", t.check_breakeven_stop(px(-0.3))==100)
check("armed but price above the floor -> no exit", t.check_breakeven_stop(px(0.5))==0)
check("exit exactly AT the floor (+0.05%)", t.check_breakeven_stop(px(0.05))==100)
check("no exit just above the floor", t.check_breakeven_stop(px(0.06))==0)
check("stays armed after price recovers", (t.check_breakeven_stop(px(2.0))==0 and t.check_breakeven_stop(px(0.0))==100))
check("sells the whole remaining position", t.check_breakeven_stop(px(-0.2))==t.qty_remaining)
t.process_exit(33,"TAKE_PROFIT_1%")
check("after a tier, sells only what's left", t.check_breakeven_stop(px(-0.2))==67)

print("\n=== B. THE 2026-08-25 CASES ===")
for sym,mfe,actual in [("CHWY",0.98,-0.41),("CRM",0.93,-0.67),("COIN",1.32,-0.23)]:
    st2,t2=mk(); t2.highest_since_entry=px(mfe)
    fired=t2.check_breakeven_stop(px(actual))
    check(f"{sym} (peak {mfe:+.2f}%, closed {actual:+.2f}%) would now stop at breakeven", fired>0)
# MARA peaked +0.44% and closed -0.98%. Under the old single 0.5 trigger it was
# NOT armed - the documented cost of that trigger. The 0.30 tier added on
# 2026-08-26 exists precisely to catch this shape: on that session the positions
# peaking between 0 and +0.5% lost $293.10 across eight, with no winners.
st3,t3=mk(); t3.highest_since_entry=px(0.44)
check("MARA (peak +0.44%) NOW armed by the 0.30 tier", t3.check_breakeven_stop(px(-0.98))>0)
_s4,_t4=mk(_ONE); _t4.highest_since_entry=px(0.44)
check("MARA would still be unarmed under a single 0.5 tier",
      _t4.check_breakeven_stop(px(-0.98))==0)

print("\n=== B2. TIER MECHANICS ===")
_TWO=copy.deepcopy(CFG); _TWO["trading"]["breakeven_tiers"]=[
    {"trigger_pct":0.5,"floor_pct":0.20},{"trigger_pct":0.30,"floor_pct":0.05}]
_s5,_t5=mk(_TWO); _t5.highest_since_entry=px(0.6)
check("both armed -> the HIGHER floor wins", _t5.check_breakeven_stop(px(0.15))>0)
_s6,_t6=mk(_TWO); _t6.highest_since_entry=px(0.35)
check("only the low tier armed -> only the low floor applies",
      _t6.check_breakeven_stop(px(0.15))==0 and _t6.check_breakeven_stop(px(0.04))>0)
_LEGACY=copy.deepcopy(CFG); _LEGACY["trading"].pop("breakeven_tiers",None)
_s7,_t7=mk(_LEGACY); _t7.highest_since_entry=px(0.55)
check("no breakeven_tiers key -> falls back to trigger/floor",
      _t7.check_breakeven_stop(px(0.02))>0)
_s8,_t8=mk(_LEGACY); _t8.highest_since_entry=px(0.44)
check("legacy fallback keeps the old 0.5 behaviour", _t8.check_breakeven_stop(px(-0.98))==0)
_BAD=copy.deepcopy(CFG); _BAD["trading"]["breakeven_tiers"]=[
    {"trigger_pct":0.30,"floor_pct":0.05},{"nonsense":1}]
_s9,_t9=mk(_BAD); _t9.highest_since_entry=px(0.35)
check("a malformed tier is skipped, not fatal", _t9.check_breakeven_stop(px(-0.5))>0)
_LIVE_T=CFG["trading"]["breakeven_tiers"]
check("live config carries both tiers", len(_LIVE_T)==2)
check("live low tier is 0.30", any(abs(t["trigger_pct"]-0.30)<1e-9 for t in _LIVE_T))
check("every live floor sits ABOVE entry (a true zero fills into the spread)",
      all(t["floor_pct"]>0 for t in _LIVE_T))

print("\n=== C. PRIORITY ===")
st4,t4=mk(); t4.highest_since_entry=px(0.8)
r=st4.check_exit("Z",{"close":px(0.02)})
check("BREAKEVEN_STOP fires and is named", r and r["reason"]=="BREAKEVEN_STOP", r)
st5,t5=mk(); t5.highest_since_entry=px(0.8)
r5=st5.check_exit("Z",{"close":px(-1.2)})
check("a real -1% loss still outranks it", r5["reason"]=="FINAL_EXIT_-1.0%", r5)
st6,t6=mk(); t6.highest_since_entry=px(2.0)
r6=st6.check_exit("Z",{"close":px(1.6)})
check("take-profit tiers still outrank it", r6["reason"].startswith("TAKE_PROFIT"), r6)
off=copy.deepcopy(CFG); off["trading"]["use_breakeven_floor"]=False
st7,t7=mk(off); t7.highest_since_entry=px(0.8)
check("toggle off -> never fires", t7.check_breakeven_stop(px(0.0))==0)
check("toggle off -> check_exit unaffected", st7.check_exit("Z",{"close":px(0.02)}) is None)

print("\n=== D. POLL-RATE-INVARIANT WINDOWS ===")
def win(interval, key_m, key_s, dm, ds):
    c=copy.deepcopy(CFG); c["trading"]["entry_check_interval_seconds"]=interval
    return _samples_for_minutes(c, key_m, key_s, dm, ds)
check("6 min @ 60s poll -> 6 samples", win(60,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)==6)
check("6 min @ 10s poll -> 36 samples", win(10,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)==36)
check("6 min @ 5s poll -> 72 samples", win(5,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)==72)
check("3 min @ 10s poll -> 18 samples", win(10,"resistance_lookback_minutes","resistance_lookback_samples",3,3)==18)
check("never below 2 samples", win(3600,"resistance_lookback_minutes","resistance_lookback_samples",3,3)==2)
nom=copy.deepcopy(CFG); nom["trading"].pop("momentum_fade_window_minutes")
check("minutes key absent -> falls back to the sample count",
      _samples_for_minutes(nom,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)==6)
z=copy.deepcopy(CFG); z["trading"]["entry_check_interval_seconds"]=0
check("zero interval -> treated as 60s, no divide-by-zero",
      _samples_for_minutes(z,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)==6)

print("\n=== E. THE HAIR-TRIGGER REGRESSION THIS PREVENTS ===")
fast=copy.deepcopy(CFG); fast["trading"]["entry_check_interval_seconds"]=10
st8,t8=mk(fast)
# 18 samples at 10s = 3 real minutes of decline required for RESISTANCE
t8.price_history=[100.0, 99.99, 99.98]
t8.highest_since_entry=100.0
check("3 samples at a 10s poll is NOT enough for resistance (was, before)",
      t8.check_resistance(99.98)==0)
lookback=_samples_for_minutes(fast,"resistance_lookback_minutes","resistance_lookback_samples",3,3)
check(f"resistance now needs {lookback} samples = 3 real minutes", lookback==18)
mwin=_samples_for_minutes(fast,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)
check(f"momentum fade needs {mwin} samples = 6 real minutes", mwin==36)

print("\n=== F. CONFIG ===")
t_=CFG["trading"]
check("poll interval 10s", t_["entry_check_interval_seconds"]==10)
check("trail back to 0.75", t_["trailing_stop_pct"]==0.75)
check("floor is above zero (covers the spread)", t_["breakeven_floor_pct"]>0)
check("trigger below the first take-profit tier",
      t_["breakeven_trigger_pct"] < t_["take_profit_tiers"][0]["gain_pct"])
check("trigger above the trail width", t_["breakeven_trigger_pct"] >= t_["trailing_stop_pct"]*0.6)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
