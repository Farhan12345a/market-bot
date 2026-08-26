"""Reproduce the real 2026-08-19 breach: a buy approved at 10/10 because the
broker's position list lagged the previous poll's fills."""
import sys, time
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.executor.executor as ex_mod
from src.executor.executor import Executor

CFG = {"trading": {"max_concurrent_positions": 10, "max_total_exposure_fraction": 0.9,
                   "max_daily_loss_usd": 100000}}

class Pos:
    def __init__(self, sym, qty, px):
        self.symbol=sym; self.qty=str(qty); self.market_value=str(qty*px)
        self.avg_entry_price=str(px); self.current_price=str(px)
class Acct:
    equity="100000"; buying_power="1000000"; cash="100000"
class Order:
    id="o1"

class LaggyBroker:
    """Models Alpaca: fills are real immediately, but get_positions() only
    reveals them after `lag` further calls."""
    def __init__(self, lag=1):
        self.real={}; self.visible={}; self.lag=lag; self._queue=[]
    def submit_market_order(self, symbol, qty, side="buy"):
        if side=="buy": self.real[symbol]=qty
        else: self.real.pop(symbol, None)
        self._queue.append(dict(self.real))
        return Order()
    def get_account(self): return Acct()
    def get_positions(self):
        # reveal a snapshot from `lag` steps ago
        if len(self._queue) > self.lag: self.visible = self._queue.pop(0)
        return {s: Pos(s,q,100.0) for s,q in self.visible.items()}

fails=[]
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ")+name+("" if cond else f"   <- {detail}"))
    if not cond: fails.append(name)

# ---- T1: THE REAL BUG. Buys spread across polls with a lagging broker. ----
b=LaggyBroker(lag=2); e=Executor(b,CFG)
opened=[]
for poll in range(6):
    e.refresh_account_snapshot()
    for i in range(4):                      # 4 candidates qualify each poll
        sym=f"S{poll}_{i}"
        ok,reason=e.pre_entry_check(10,100.0)
        if ok:
            e.submit_entry_order(sym,10,100.0,entry_method="T")
            opened.append(sym)
check("T1 never exceeds cap of 10 despite broker lag", len(opened)<=10, f"opened {len(opened)}")
check("T1 actually fills up to the cap", len(opened)==10, f"opened {len(opened)}")
check("T1 broker truly holds <=10", len(b.real)<=10, f"broker has {len(b.real)}")

# ---- T2: same-poll burst (the earlier fix must not regress) ----
b2=LaggyBroker(lag=0); e2=Executor(b2,CFG)
e2.refresh_account_snapshot()
n=0
for i in range(30):
    ok,_=e2.pre_entry_check(10,100.0)
    if ok: e2.submit_entry_order(f"B{i}",10,100.0,entry_method="T"); n+=1
check("T2 same-poll burst capped at 10", n==10, f"opened {n}")

# ---- T3: exit frees a slot immediately, within the same poll ----
ok,_=e2.pre_entry_check(10,100.0)
check("T3 blocked while full", ok is False)
e2.submit_exit_order("B0",10,"FINAL_EXIT_-1.0%",100.0)
ok,_=e2.pre_entry_check(10,100.0)
check("T3 slot freed after full exit", ok is True)

# ---- T4: PARTIAL exit must NOT free a slot ----
b4=LaggyBroker(lag=0); e4=Executor(b4,CFG)
e4.refresh_account_snapshot()
for i in range(10): e4.submit_entry_order(f"P{i}",10,100.0,entry_method="T")
e4.submit_exit_order("P0",3,"FIRST_EXIT_-0.5%",100.0)
ok,_=e4.pre_entry_check(10,100.0)
check("T4 partial exit does not free a slot", ok is False)
check("T4 count still 10", e4._open_position_count==10, e4._open_position_count)

# ---- T5: position closed OUTSIDE the bot ages out of the count ----
b5=LaggyBroker(lag=0); e5=Executor(b5,CFG)
e5.refresh_account_snapshot()
e5.submit_entry_order("GHOST",10,100.0,entry_method="T")
b5.real.pop("GHOST"); b5._queue=[dict(b5.real)]   # vanished without our exit path
e5.refresh_account_snapshot()
check("T5 within grace: still counted", e5._open_position_count==1, e5._open_position_count)
orig=ex_mod.ENTRY_CONFIRM_GRACE_SECONDS
ex_mod.ENTRY_CONFIRM_GRACE_SECONDS=-1            # simulate grace elapsing
b5._queue=[dict(b5.real)]
e5.refresh_account_snapshot()
check("T5 after grace: aged out (no permanent phantom)", e5._open_position_count==0, e5._open_position_count)
ex_mod.ENTRY_CONFIRM_GRACE_SECONDS=orig

# ---- T6: externally-opened position IS counted (broker is authoritative) ----
b6=LaggyBroker(lag=0); e6=Executor(b6,CFG)
b6.real={"EXT1":5,"EXT2":5}; b6._queue=[dict(b6.real)]
e6.refresh_account_snapshot()
check("T6 external positions counted", e6._open_position_count==2, e6._open_position_count)

# ---- T7: exposure includes broker-invisible pending entries ----
b7=LaggyBroker(lag=5); e7=Executor(b7,CFG)
e7.refresh_account_snapshot()
e7.submit_entry_order("X",10,100.0,entry_method="T")   # $1000, broker can't see it yet
before=e7._total_exposure_usd
e7.refresh_account_snapshot()
check("T7 exposure survives refresh while broker lags",
      abs(e7._total_exposure_usd-1000.0)<1e-6, f"{before} -> {e7._total_exposure_usd}")

# ---- T8: exposure cap engages independently of the count cap ----
CFG8={"trading":{"max_concurrent_positions":100,"max_total_exposure_fraction":0.05,
                 "max_daily_loss_usd":100000}}
b8=LaggyBroker(lag=3); e8=Executor(b8,CFG8); e8.refresh_account_snapshot()
n8=0
for poll in range(4):
    e8.refresh_account_snapshot()
    for i in range(5):
        ok,_=e8.pre_entry_check(10,100.0)
        if ok: e8.submit_entry_order(f"E{poll}_{i}",10,100.0,entry_method="T"); n8+=1
check("T8 exposure cap holds under lag (<=5% of 100k = 5 positions)", n8<=5, f"opened {n8}")

# ---- T9: no double-count when the broker finally reveals a pending entry ----
b9=LaggyBroker(lag=2); e9=Executor(b9,CFG); e9.refresh_account_snapshot()
e9.submit_entry_order("D",10,100.0,entry_method="T")
for _ in range(5): e9.refresh_account_snapshot()
check("T9 revealed entry counted exactly once", e9._open_position_count==1, e9._open_position_count)
check("T9 exposure not double-counted", abs(e9._total_exposure_usd-1000.0)<1e-6, e9._total_exposure_usd)

print("\n"+("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
