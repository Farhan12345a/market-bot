"""PDT equity floor + partial-fill reconciliation."""
import sys, copy, types, yaml
from datetime import datetime
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.strategy.strategy as S
from src.strategy.strategy import Strategy, TradeManager
from src.executor.executor import Executor
CFG=yaml.safe_load(open(CONFIG))
S._now_et=lambda: S.ET.localize(datetime(2026,8,26,9,45))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

print("=== A. PDT EQUITY FLOOR ===")
def ex_with(equity, exposure=0.0, bp=1e6, n_open=0):
    e=Executor(types.SimpleNamespace(), copy.deepcopy(CFG))
    e._equity=equity; e._buying_power=bp; e._total_exposure_usd=exposure
    e._open_symbols=set(f"S{i}" for i in range(n_open))
    return e
ok,why=ex_with(24999.0).pre_entry_check(10, 100.0)
check("blocks entries below $25,000", ok is False)
check("reason names the PDT rule", "pattern-day-trader" in why, why)
check("reason states open positions are unaffected", "still managed" in why)
ok2,_=ex_with(25000.0).pre_entry_check(10, 100.0)
check("allows entries exactly AT the threshold", ok2 is True)
ok3,_=ex_with(94000.0).pre_entry_check(10, 100.0)
check("allows entries well above it", ok3 is True)
zero=copy.deepcopy(CFG); zero["trading"]["min_account_equity_usd"]=0
e0=Executor(types.SimpleNamespace(), zero); e0._equity=100.0; e0._buying_power=1e6; e0._total_exposure_usd=0
check("0 disables the floor", e0.pre_entry_check(1,10.0)[0] is True)
e_unknown=ex_with(0.0)
check("unknown equity does not block (fails open, other gates still apply)",
      e_unknown.pre_entry_check(10,100.0)[0] is True)
check("live config floor is 25000", CFG["trading"]["min_account_equity_usd"]==25000)

print("\n=== B. PARTIAL FILL RECONCILIATION ===")
def mk(qty=79):
    c=copy.deepcopy(CFG); t=TradeManager("HOOD",100.0,qty,c)
    st=Strategy(c); st.trades["HOOD"]=t; t.price_history=[100.0]*40
    return st,t
st,t=mk(79)
check("starts believing it holds what it asked for", t.qty_remaining==79)
changed=st.correct_entry_qty("HOOD", 40)
check("reconciles down to what the broker holds", t.qty_remaining==40, t.qty_remaining)
check("reports that it changed something", changed is True)
check("original size shrinks proportionally so tiers size off reality",
      t.entry_qty==40, t.entry_qty)
tier=t.check_take_profit(101.05)   # +1.05% = tier 1, not the top tier
check("a 40% tier now sizes off 40, not 79", tier[0]==int(40*0.4), tier)
check("tier can never exceed what is held", tier[0] <= t.qty_remaining)

st2,t2=mk(79)
check("broker holding MORE is ignored (an exit may be in flight)",
      st2.correct_entry_qty("HOOD", 100) is False and t2.qty_remaining==79)
check("equal count is a no-op", st2.correct_entry_qty("HOOD", 79) is False)
check("unknown symbol -> False, no raise", st2.correct_entry_qty("NOPE", 5) is False)
check("garbage qty -> False", st2.correct_entry_qty("HOOD","x") is False)
check("negative qty -> False", st2.correct_entry_qty("HOOD",-5) is False)
st3,t3=mk(79); st3.correct_entry_qty("HOOD", 1)
check("down to 1 share still leaves a valid position", t3.qty_remaining==1 and t3.entry_qty>=1)

print("\n=== C. EXECUTOR -> STRATEGY WIRING ===")
e=Executor(types.SimpleNamespace(), CFG)
check("callback defaults to None (executor usable alone)", e.on_entry_qty_corrected is None)
esrc=open(repo_file("src", "executor", "executor.py")).read()
check("reconciliation invokes the callback", "self.on_entry_qty_corrected(symbol, held)" in esrc)
check("skipped inside the entry grace window (broker list lags a fill)",
      "ENTRY_CONFIRM_GRACE_SECONDS" in esrc.split("on_entry_qty_corrected(symbol, held)")[0][-700:])
check("a callback failure cannot break the snapshot refresh",
      "Could not reconcile share count" in esrc)
msrc=open(repo_file("src", "main.py")).read()
check("main wires it to the strategy",
      "executor.on_entry_qty_corrected = strategy.correct_entry_qty" in msrc)
check("wired alongside the price correction", 
      abs(msrc.index("on_entry_qty_corrected") - msrc.index("on_entry_price_corrected")) < 400)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
