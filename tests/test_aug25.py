"""Wash-trade cancel, known-beat filter, watchlist price filter."""
import sys, copy, types, yaml, logging
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import pytz
import src.main as M
import src.screener.list_builder as LB
from src.executor.executor import Executor
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

print("=== A. CANCEL BEFORE EXIT (the 2026-08-24 wash-trade bug) ===")
class Order:
    def __init__(s,i,side="buy",filled=40,qty=79): s.id=i; s.side=side; s.filled_qty=filled; s.qty=qty
class Broker:
    # holdings models what the broker ACTUALLY holds, independent of
    # open_orders/sold - every scenario in this file exits a real, already-
    # filled HOOD position (79 shares), so the phantom-entry guard in
    # submit_exit_order (added 2026-09-02) must see it as genuinely held, not
    # as a phantom. A bare `get_positions(): return {}` used to be fine here
    # because nothing read it; now it decides whether the sell fires at all.
    def __init__(s, open_orders=None, cancel_boom=False, list_boom=False, sell_boom=False,
                 holdings=None):
        s.open_orders=open_orders or []; s.cancelled=[]; s.sold=[]
        s.cancel_boom=cancel_boom; s.list_boom=list_boom; s.sell_boom=sell_boom
        s.order=0
        s.holdings = dict(holdings) if holdings is not None else {"HOOD": 79}
    def cancel_open_orders(s,sym):
        if s.list_boom: raise RuntimeError("orders endpoint down")
        n=0
        for o in list(s.open_orders):
            if s.cancel_boom: raise RuntimeError("cancel failed")
            s.cancelled.append(o.id); s.open_orders.remove(o); n+=1
        return n
    def submit_market_order(s,sym,qty,side="buy"):
        if side=="sell" and s.sell_boom and s.open_orders:
            raise RuntimeError('potential wash trade detected')
        s.sold.append((sym,qty,side)); s.order+=1
        if side=="sell":
            s.holdings[sym] = max(0, s.holdings.get(sym, 0) - qty)
        return types.SimpleNamespace(id=f"o{s.order}")
    def get_account(s): return types.SimpleNamespace(cash="90000",equity="90000",buying_power="90000")
    def get_positions(s):
        return {sym: types.SimpleNamespace(symbol=sym, qty=str(q))
                for sym, q in s.holdings.items() if q}

def mkex(broker):
    ex=Executor(broker, copy.deepcopy(CFG))
    ex.open_entries["HOOD"]=100.0
    ex.entry_meta["HOOD"]={"method":"X","rsi":None,"entry_time":None,"price_source":"tick"}
    return ex

b=Broker(open_orders=[Order("1e711544")], sell_boom=True)
ex=mkex(b)
r=ex.submit_exit_order("HOOD", 79, "FIRST_EXIT_-0.5%", price=99.5, qty_before=79)
check("exit succeeds where it previously hit the wash-trade rejection", r is not None)
check("the working entry order was cancelled first", b.cancelled==["1e711544"], b.cancelled)
check("the sell then went through", ("HOOD",79,"sell") in b.sold, b.sold)

b2=Broker(open_orders=[])
ex2=mkex(b2); ex2.submit_exit_order("HOOD",79,"TRAILING_STOP",price=99.0,qty_before=79)
check("no open orders -> nothing cancelled, exit still submitted",
      b2.cancelled==[] and len(b2.sold)==1)
b3=Broker(open_orders=[Order("a"),Order("b"),Order("c")], sell_boom=True)
ex3=mkex(b3); ex3.submit_exit_order("HOOD",79,"RESISTANCE",price=99.0,qty_before=79)
check("cancels EVERY working order, not just one", len(b3.cancelled)==3, b3.cancelled)

b4=Broker(open_orders=[Order("x")], list_boom=True)
ex4=mkex(b4)
r4=ex4.submit_exit_order("HOOD",79,"FINAL_EXIT_-1.0%",price=99.0,qty_before=79)
check("cancel failure does NOT block the exit attempt", len(b4.sold)==1, b4.sold)
check("...and does not raise into the trading loop", r4 is not None)
b5=Broker(open_orders=[Order("y")], cancel_boom=True)
ex5=mkex(b5)
check("cancel raising mid-loop still lets the exit through",
      ex5.submit_exit_order("HOOD",79,"RESISTANCE",price=99.0,qty_before=79) is not None)
src=open(repo_file("src", "executor", "executor.py")).read()
check("cancel happens BEFORE the sell is submitted",
      src.index("cancel_open_orders(symbol)") < src.index('submit_market_order(symbol, qty, side="sell")'))
bsrc=open(repo_file("src", "broker", "alpaca_broker.py")).read()
check("broker exposes cancel_open_orders", "def cancel_open_orders" in bsrc)
check("broker filters to the ONE symbol", "symbols=[symbol]" in bsrc)
check("broker only cancels OPEN orders", "QueryOrderStatus.OPEN" in bsrc)

print("\n=== B. KNOWN-BEAT FILTER ===")
class FS:
    et=pytz.timezone("America/New_York")
    def __init__(s,gaps): s.g=gaps
    def _get_recent_gap(s,x): return s.g.get(x,0.0)
gaps={"BEAT":1.0,"MISS":1.0,"UNKNOWN":1.0}
sur={"BEAT":8.0,"MISS":-4.0,"UNKNOWN":None}
why={k:"today BMO" for k in gaps}
# Pin the flag ON explicitly rather than inheriting it from the live config.
# These cases test the MECHANISM - what the filter does when the flag is on -
# and that question is unchanged by whether the flag happens to be on today.
# Reading it from CFG made these break the moment the live setting flipped,
# which is a test of the config file, not of the code.
on=copy.deepcopy(CFG); on["trading"]["earnings_require_known_beat"]=True
out=LB._filter_earnings_candidates(FS(gaps), list(gaps), why, sur, on)
check("known beat kept", "BEAT" in out, out)
check("miss dropped", "MISS" not in out, out)
check("UNKNOWN now dropped (this was PDD, -$86.56)", "UNKNOWN" not in out, out)
off=copy.deepcopy(CFG); off["trading"]["earnings_require_known_beat"]=False
out2=LB._filter_earnings_candidates(FS(gaps), list(gaps), why, sur, off)
check("flag off -> unknown kept again (old behaviour)", "UNKNOWN" in out2, out2)
check("flag off still drops a miss", "MISS" not in out2)
allunknown={"A":None,"B":None}
out3=LB._filter_earnings_candidates(FS({"A":1.0,"B":1.0}), ["A","B"], {"A":"x","B":"x"}, allunknown, on)
check("all-unknown morning -> empty list, no crash (the documented cost)", out3==[], out3)
# The live setting went back OFF on 2026-08-26. Turning it on had rested on a
# single data point (PDD, -$86.56); the next session it dropped 15 of 15
# candidates, making it a total veto rather than a filter and the whole earnings
# feature permanently inert. The gap filter is the part with evidence behind it.
check("live config has the flag OFF again",
      CFG["trading"]["earnings_require_known_beat"] is False)
check("live config still requires a BEAT", CFG["trading"]["earnings_require_beat"] is True)
check("live config still caps the pre-market gap", CFG["trading"]["earnings_max_gap_pct"] == 3.0)

print("\n=== C. WATCHLIST PRICE FILTER ===")
M.screener_details.clear()
M.screener_details.update({"AMC":{"price":2.70},"TLRY":{"price":4.64},"PTON":{"price":5.39},
                           "HOOD":{"price":106.0},"QQQ":{"price":710.0},"DASH":{"price":221.0},
                           "NOPRICE":{}})
syms=["AMC","TLRY","PTON","HOOD","QQQ","DASH","NOPRICE","UNSEEN"]
out=M._filter_watchlist_by_price(CFG, syms)
check("sub-$10 names dropped from the watchlist", not ({"AMC","TLRY","PTON"} & set(out)), out)
check("over-$300 dropped (QQQ at $710)", "QQQ" not in out, out)
check("in-band names kept", {"HOOD","DASH"} <= set(out), out)
check("symbol with no price is KEPT (absent != out of band)", "NOPRICE" in out)
check("symbol the screener never saw is KEPT", "UNSEEN" in out)
check("order preserved", out==["HOOD","DASH","NOPRICE","UNSEEN"], out)
M.screener_details.clear()
check("no screener details -> nothing dropped", M._filter_watchlist_by_price(CFG, syms)==syms)
M.screener_details.update({s:{"price":1.0} for s in syms})
check("all priced out -> watchlist kept intact rather than emptied",
      M._filter_watchlist_by_price(CFG, syms)==syms)
nofilter=copy.deepcopy(CFG); nofilter["trading"]["min_stock_price"]=0; nofilter["trading"]["max_stock_price"]=0
check("both bounds zero -> filter disabled", M._filter_watchlist_by_price(nofilter, syms)==syms)
msrc=open(repo_file("src", "main.py")).read()
check("filter runs before the stream-budget warning",
      msrc.index("_filter_watchlist_by_price(config, symbols)") <
      msrc.index("_warn_if_watchlist_outruns_the_stream(config, symbols)"))
check("entry-time price gate still present as a backstop",
      "below min_stock_price" in msrc and "above max_stock_price" in msrc)

print("\n=== D. CONFIG ===")
t=CFG["trading"]
# reverted to 0.75 on 2026-08-25 - the wider leash gave back every positive
# MFE it touched. Assert the SHAPE, not the value, so retuning is not a failure.
check("trailing_stop_pct is a sane positive %", 0 < t["trailing_stop_pct"] <= 3, t["trailing_stop_pct"])
check("still above the first-exit stop", t["trailing_stop_pct"] > abs(t["first_exit_loss_pct"]))
check("take-profit tiers unchanged", [x["gain_pct"] for x in t["take_profit_tiers"]]==[1.0,1.25,1.5])

print("\n=== E. SCREENER EXCLUDES OUT-OF-BAND PRICES BEFORE RANKING ===")
import types as _t
from src.screener.stock_screener import StockScreener
class SC(StockScreener):
    def __init__(s, prices, scores):
        s.broker=_t.SimpleNamespace(); s.et=pytz.timezone("America/New_York")
        s.config=CFG["trading"]; s.candidates=list(prices); s._p=prices; s._s=scores
        s.last_scores={}; s.last_details={}
    def score_stock(s, sym):
        return s._s[sym], {"symbol":sym,"price":s._p[sym],"gap_pct":0,"5day_return_pct":0,
                           "volume_ratio":1.0,"volatility_percentile":50,"opening_hit_rate":0,
                           "opening_avg_gain":0,"opening_sessions":0}
sc=SC({"AMC":2.70,"TLRY":4.64,"HOOD":106.0,"QQQ":710.0,"DASH":221.0,"NOPX":0},
      {"AMC":95,"TLRY":90,"HOOD":60,"QQQ":85,"DASH":55,"NOPX":70})
picked=sc.screen(top_n=10, min_score=35)
check("sub-$10 excluded even with the TOP score", "AMC" not in picked, picked)
check("TLRY excluded too", "TLRY" not in picked, picked)
check("over-$300 excluded (QQQ $710)", "QQQ" not in picked, picked)
check("in-band names still selected", {"HOOD","DASH"} <= set(picked), picked)
check("unknown price still allowed through", "NOPX" in picked, picked)
check("they cannot occupy a stream slot either", "AMC" not in sc.last_scores)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
