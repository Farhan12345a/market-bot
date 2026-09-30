"""PDT equity floor + partial-fill reconciliation."""
import sys, copy, types, yaml, time
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
def mk(qty=79, side="long"):
    c=copy.deepcopy(CFG); t=TradeManager("HOOD",100.0,qty,c,side=side)
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

# GROWING - added 2026-09-29. Previously refused unconditionally ("an exit
# may be in flight"), which silently left every slow multi-tranche fill that
# landed MORE shares than intended completely unreconciled - the real hole
# behind 2026-09-29's 27 ORPHAN_RECONCILE exits (-$567.84). The "exit may be
# in flight" risk is now the CALLER's job (see section C's
# _pending_exit_verify check) - this function itself has no reason to
# refuse a magnitude change in either direction.
st2,t2=mk(79)
changed2=st2.correct_entry_qty("HOOD", 100)
check("broker holding MORE now reconciles UP, not refused",
      changed2 is True and t2.qty_remaining==100, t2.qty_remaining)
check("original size grows proportionally too",
      t2.entry_qty==100, t2.entry_qty)

st2b,t2b=mk(79)
check("equal count is a no-op", st2b.correct_entry_qty("HOOD", 79) is False)
check("unknown symbol -> False, no raise", st2b.correct_entry_qty("NOPE", 5) is False)
check("garbage qty -> False", st2b.correct_entry_qty("HOOD","x") is False)

st3,t3=mk(79); st3.correct_entry_qty("HOOD", 1)
check("down to 1 share still leaves a valid position", t3.qty_remaining==1 and t3.entry_qty>=1)

print("\n=== B2. SHORTS GET THE SAME RECONCILIATION, BOTH DIRECTIONS ===")
# The other half of 2026-09-29's fix: the broker reports a short position's
# qty as NEGATIVE, and the caller used to skip every negative reading
# outright (held <= 0: continue), so a short's fill count was NEVER
# reconciled at all, growing or shrinking. correct_entry_qty itself works in
# MAGNITUDE, direction-agnostic - trade.direction is what says which way it
# faces, not the sign passed in here.
st4,t4=mk(18, side="short")
changed4=st4.correct_entry_qty("HOOD", -36)   # broker: short 36, tracked: short 18
check("a short's tracked qty grows too, from a SIGNED negative broker read",
      changed4 is True and t4.qty_remaining==36, t4.qty_remaining)
check("entry_qty scales the same way for a short as for a long",
      t4.entry_qty==36, t4.entry_qty)

st5,t5=mk(36, side="short")
changed5=st5.correct_entry_qty("HOOD", -20)   # broker: short 20, tracked: short 36
check("a short's tracked qty shrinks too", changed5 is True and t5.qty_remaining==20)

print("\n=== B3. A SIGN THAT DISAGREES WITH THE TRACKED SIDE IS REFUSED ===")
# Not ordinary fill drift - the broker thinks this symbol is on the OPPOSITE
# side from what the strategy believes, which is a much bigger problem than
# a magnitude correction and is left to the orphan/reconcile path instead.
st6,t6=mk(79, side="long")
check("a LONG position told the broker shows NEGATIVE (short) is refused",
      st6.correct_entry_qty("HOOD", -5) is False and t6.qty_remaining==79)
st7,t7=mk(18, side="short")
check("a SHORT position told the broker shows POSITIVE (long) is refused",
      st7.correct_entry_qty("HOOD", 5) is False and t7.qty_remaining==18)

print("\n=== B4. THE ORPHAN THIS ACTUALLY PREVENTS: A FULL EXIT NOW SELLS "
      "THE TRUE HELD AMOUNT, NOT THE ORIGINALLY-INTENDED ONE ===")
# The 2026-09-29 COIN shape, reproduced directly: entry confirmed for 18
# short, broker's real fill grows to 36 before any exit fires (a slow
# multi-tranche fill, exactly what hit COIN/MXL/IONQ/etc that day). Before
# this fix, check_exit's FINAL_EXIT still asked for the ORIGINAL 18 -
# leaving 18 real shares behind for the periodic reconcile to find later as
# an orphan. After this fix, the exit asks for the full, corrected amount.
st8, t8 = mk(18, side="short")
st8.correct_entry_qty("HOOD", -36)
check("qty_remaining reflects the true fill before any exit is evaluated",
      t8.qty_remaining == 36)
# A price far enough through final_exit_loss_pct to force a full close,
# regardless of the live config's exact threshold.
bad_price = t8.entry_price * 1.05   # +5% against a short - well past any final stop
exit_info = st8.check_exit("HOOD", {"close": bad_price})
check("a full exit now asks for ALL 36 shares - nothing is left behind "
      "for reconcile to find as a phantom orphan",
      exit_info is not None and exit_info["qty"] == 36, exit_info)

print("\n=== C. EXECUTOR -> STRATEGY WIRING ===")
e=Executor(types.SimpleNamespace(), CFG)
check("callback defaults to None (executor usable alone)", e.on_entry_qty_corrected is None)
esrc=open(repo_file("src", "executor", "executor.py")).read()
check("reconciliation invokes the callback", "self.on_entry_qty_corrected(symbol, held)" in esrc)
check("skipped inside the entry grace window (broker list lags a fill)",
      "ENTRY_CONFIRM_GRACE_SECONDS" in esrc.split("on_entry_qty_corrected(symbol, held)")[0][-1200:])
check("only a truly FLAT broker reading is skipped now - held <= 0 used to "
      "exempt every short position from this reconciliation entirely",
      "if held == 0:" in esrc.split("on_entry_qty_corrected(symbol, held)")[0][-900:]
      and "if held <= 0:" not in esrc.split("def refresh_account_snapshot")[1].split(
          "def note_post_exit_prices")[0])
check("a symbol with an exit still in flight is deferred, not reconciled - "
      "this is where the 'an exit may be in flight' risk correct_entry_qty "
      "used to guard against on its own now actually lives",
      "if symbol in self._pending_exit_verify:" in
      esrc.split("def refresh_account_snapshot")[1].split("def note_post_exit_prices")[0])
check("a callback failure cannot break the snapshot refresh",
      "Could not reconcile share count" in esrc)
msrc=open(repo_file("src", "main.py")).read()
check("main wires it to the strategy",
      "executor.on_entry_qty_corrected = strategy.correct_entry_qty" in msrc)
check("wired alongside the price correction",
      abs(msrc.index("on_entry_qty_corrected") - msrc.index("on_entry_price_corrected")) < 400)

print("\n=== C2. THE DEFERRAL ACTUALLY WORKS END TO END ===")
class FakeBroker:
    def __init__(self, positions, equity=100000.0, bp=100000.0):
        self._positions = positions
        self.equity, self.bp = equity, bp
    def get_account(self):
        return types.SimpleNamespace(equity=self.equity, buying_power=self.bp, last_equity=self.equity)
    def get_positions(self):
        return self._positions
def Pos(qty, avg=100.0):
    return types.SimpleNamespace(qty=str(qty), avg_entry_price=str(avg),
                                  market_value=str(qty*avg), unrealized_pl=0)

calls = []
e2 = Executor(FakeBroker({"COIN": Pos(-54)}), copy.deepcopy(CFG))
e2._open_symbols.add("COIN")
e2._entry_recorded_at["COIN"] = time.monotonic() - 999   # well outside ENTRY_CONFIRM_GRACE_SECONDS
# (NOT 0.0 - time.monotonic()'s epoch is process/system start, not some
# fixed point far in the past. On a freshly-started container it can
# itself be under ENTRY_CONFIRM_GRACE_SECONDS (120s), which made this
# test genuinely flaky: it passed once this process had been up 120s+,
# and failed - silently, since refresh_account_snapshot's own grace-
# window skip looks identical to the deferral this test means to prove -
# on a fresh one. Found 2026-09-30 by re-running the full suite fresh
# rather than trusting a prior green run.)
e2.open_entries["COIN"] = 100.0
e2.on_entry_qty_corrected = lambda sym, held: calls.append((sym, held))
e2._pending_exit_verify["COIN"] = {"ts": 0.0, "qty": 5, "side": "buy"}
e2.refresh_account_snapshot()
check("qty reconciliation is skipped while an exit is still pending for "
      "this symbol - the broker's count is ambiguous in that exact window",
      calls == [], calls)

e2._pending_exit_verify.pop("COIN", None)
e2.refresh_account_snapshot()
check("...and runs normally once the exit is no longer pending",
      calls == [("COIN", -54)], calls)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
