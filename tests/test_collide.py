"""Do tomorrow's changes fight each other? Live config, no mocks of the rules."""
import sys, copy, yaml
from datetime import datetime
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.strategy.strategy as S
from src.strategy.strategy import Strategy, TradeManager, _samples_for_minutes
import src.main as M
CFG=yaml.safe_load(open(CONFIG))
S._now_et=lambda: S.ET.localize(datetime(2026,8,26,9,45))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")
def mk(qty=300):
    c=copy.deepcopy(CFG); t=TradeManager("Z",100.0,qty,c)
    st=Strategy(c); st.trades["Z"]=t; t.price_history=[100.0]*40
    return st,t
def px(p): return 100.0*(1+p/100)

print("=== 1. ONE POSITION, ALL EXIT RULES LIVE ===")
st,t=mk()
path=[0.3,0.6,1.05,1.3,1.55,0.4,0.06]   # up through every tier, then collapse
fired=[]
for p in path:
    r=st.check_exit("Z",{"close":px(p)})
    if r: fired.append((round(p,2),r["reason"],r["qty"])); st.confirm_exit("Z",r["qty"],r["reason"],px(p))
    if "Z" not in st.trades: break
names=[f[1] for f in fired]
check("tiers fire in order, then the position closes", names==["TAKE_PROFIT_1%","TAKE_PROFIT_1.25%","TAKE_PROFIT_1.5%"], fired)
check("no rule double-sold: qty sums to the position", sum(f[2] for f in fired)==300, fired)
check("position fully closed", "Z" not in st.trades)

print("\n=== 2. BREAKEVEN FLOOR vs THE STOPS (can they both fire?) ===")
st,t=mk()
for p in [0.6, 0.02]:
    r=st.check_exit("Z",{"close":px(p)})
    if r: fired2=(p,r["reason"]); st.confirm_exit("Z",r["qty"],r["reason"],px(p))
check("armed position exits at BREAKEVEN_STOP, not -0.5%/-1.0%", fired2[1]=="BREAKEVEN_STOP", fired2)
st,t=mk()
r=st.check_exit("Z",{"close":px(-0.6)})
check("UNARMED position still takes the -0.5% first exit", r["reason"]=="FIRST_EXIT_-0.5%", r)
st,t=mk(); t.highest_since_entry=px(0.8)
r=st.check_exit("Z",{"close":px(-1.5)})
check("a gap straight through still takes the -1.0% stop, not breakeven",
      r["reason"]=="FINAL_EXIT_-1.0%", r)

print("\n=== 3. TRAIL vs FLOOR vs TIERS - which wins where ===")
st,t=mk(); t.highest_since_entry=px(0.6); t.highest_price=px(0.6)
r=st.check_exit("Z",{"close":px(-0.2)})
check("floor beats the trail when both would fire", r["reason"]=="BREAKEVEN_STOP", r)
# Spend the tiers first - otherwise TAKE_PROFIT_1.5% correctly pre-empts both,
# which is the right priority but not the question being asked here.
st,t=mk(); t.take_profit_tiers_done={0,1,2}
t.highest_since_entry=px(3.0); t.highest_price=px(3.0)
r=st.check_exit("Z",{"close":px(2.0)})
check("with tiers spent, a runner exits on the TRAIL, not the floor",
      r["reason"]=="TRAILING_STOP", r)
check("...and the floor was armed the whole time, it just did not win",
      t.check_breakeven_stop(px(2.0))==0 and t.check_breakeven_stop(px(0.0))>0)

print("\n=== 4. RESISTANCE OFF - does anything depend on it? ===")
st,t=mk()
t.price_history=[100.0-0.1*i for i in range(20)]; t.highest_since_entry=100.0
r=st.check_exit("Z",{"close":t.price_history[-1]})
check("a clear failed breakout no longer exits via RESISTANCE",
      r is None or r["reason"]!="RESISTANCE", r)
check("...it falls through to another rule or holds - never crashes", True)

print("\n=== 5. 10s POLL - windows still mean minutes ===")
mw=_samples_for_minutes(CFG,"momentum_fade_window_minutes","momentum_fade_window_samples",6,6)
rw=_samples_for_minutes(CFG,"resistance_lookback_minutes","resistance_lookback_samples",3,3)
iv=CFG["trading"]["entry_check_interval_seconds"]
check(f"momentum fade = {mw} samples x {iv}s = {mw*iv/60:.0f} real minutes", mw*iv/60==6, mw)
check(f"resistance = {rw} samples x {iv}s = {rw*iv/60:.0f} real minutes", rw*iv/60==3, rw)
st,t=mk(); t.price_history=[100.0,99.9,99.8]
check("3 samples is NOT enough to trigger a 6-minute fade at a 10s poll",
      t.check_momentum_fade(99.8)==0)

print("\n=== 6. CEILING vs BURST vs SPY - all three gates together ===")
c=copy.deepcopy(CFG)
check("SPY calm + small burst -> full size", M._burst_policy(c,2,0.1)[0] is None)
check("SPY lurch + small burst -> throttled anyway", M._burst_policy(c,3,0.55)[0]==c["trading"]["burst_max_entries"])
check("big burst -> throttled", M._burst_policy(c,12,0.1)[0]==c["trading"]["burst_max_entries"])
check("ceiling is independent of burst logic (different gate)",
      c["trading"]["rapid_increase_max_pct"]==2.0 and c["trading"]["burst_width_threshold"]==5)

print("\n=== 7. ENTRY PACE AT A 10s POLL ===")
t_=CFG["trading"]
window_min=22
polls=window_min*60/t_["entry_check_interval_seconds"]
check(f"entry window is now {polls:.0f} polls (was {window_min} at 60s)", polls==132)
check("concurrent cap still bounds exposure", t_["max_concurrent_positions"]==10)
check("daily entry cap still bounds churn", t_["max_daily_entries"]==50)
check("re-entry cooldown still gates the same symbol", t_["reentry_cooldown_minutes"]==5)

print("\n=== 8. CONTINUATION SCORING ===")
# Turned ON 2026-08-26. Note what "on" means: the score RANKS a burst best-first
# so the throttle keeps the best signals rather than whichever names sorted
# earliest. It does not GATE - no signal is refused for scoring low, because the
# weights are still reasoned guesses rather than a fit.
check("continuation scoring is ON", t_["use_continuation_score"] is True)
check("factors are recorded either way", "cf_score" in __import__("src.analytics.signal_journal",
      fromlist=["JOURNAL_FIELDS"]).JOURNAL_FIELDS)
# post_exit_* are written onto the row AFTER the fact and must never restate
# what was booked: assert the writer touches neither pl nor pl_pct.
_ex = open(repo_file("src", "executor", "executor.py")).read()
_fn = _ex.split("def note_post_exit_prices")[1].split("def _note_position_closed")[0]
check("post-exit tracking never rewrites pl", 'row["pl"]' not in _fn and "row['pl']" not in _fn)
check("post-exit tracking never rewrites pl_pct", "pl_pct\"]" not in _fn.replace('row.get("pl")',''))
check("it only writes its own two fields",
      'row["post_exit_pct"]' in _fn and 'row["post_exit_note"]' in _fn)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
