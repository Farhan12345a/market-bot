"""Earnings list + QQQ list + open-burst ranking."""
import sys, os, copy, types, yaml
from datetime import datetime, date, timedelta
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import pytz, pandas as pd
import src.screener.list_builder as LB

ET = pytz.timezone("America/New_York")
CFG = yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

# ---- fake screener ----
class FakeScreener:
    et = ET
    def __init__(s, **kw):
        s.gap=kw.get("gap",{}); s.vol=kw.get("vol",{}); s.rvol=kw.get("rvol",{})
        s.r5=kw.get("r5",{}); s.px=kw.get("px",{}); s.bars=kw.get("bars",{})
        s.calls=[]
        s.broker=types.SimpleNamespace(get_historical_bars=s._bars)
    def _bars(s,sym,a,b,tf): return {sym: s.bars[sym]} if sym in s.bars else {}
    def _get_recent_gap(s,x): s.calls.append(x); return s.gap.get(x,0.0)
    def _get_volatility_percentile(s,x): return s.vol.get(x,50.0)
    def _get_volume_ratio(s,x): return s.rvol.get(x,1.0)
    def _get_5day_return(s,x): return s.r5.get(x,0.0)
    def _get_current_price(s,x): return s.px.get(x,50.0)
    def _get_price(s,x): return s.px.get(x,50.0)

def qqq_bars(closes):
    return pd.DataFrame({"timestamp":[datetime(2026,8,10)+timedelta(days=i) for i in range(len(closes))],
                         "close":closes})

print("=== A. CONSTITUENT FILE ===")
c = LB.load_qqq_constituents()
check("constituent file loads", len(c) > 50, len(c))
check("no comments/blanks leak in", all(x and not x.startswith("#") for x in c))
check("deduped", len(c)==len(set(c)))
check("all look like tickers", all(x.isalpha() and 1<=len(x)<=5 for x in c), [x for x in c if not x.isalpha()][:5])
check("contains bellwethers", {"AAPL","MSFT","NVDA","QCOM"} <= set(c))
check("missing file -> empty, no raise", LB.load_qqq_constituents("/nonexistent/x.txt")==[])

print("\n=== B. EARNINGS TIMING FILTER ===")
check("pre-market -> bmo", LB._report_timing({"time":"time-pre-market"})=="bmo")
check("after-hours -> amc", LB._report_timing({"time":"time-after-hours"})=="amc")
check("not-supplied -> unknown", LB._report_timing({"time":"time-not-supplied"})=="unknown")
check("missing key -> unknown", LB._report_timing({})=="unknown")
check("None -> unknown", LB._report_timing({"time":None})=="unknown")

print("\n=== C. EARNINGS FETCH ===")
CAL = {}
def fake_fetch(datestr, timeout):
    return CAL.get(datestr, [])
LB._fetch_nasdaq_earnings = fake_fetch
now = ET.localize(datetime(2026,8,21,9,20))          # Friday
CAL = {"2026-08-21":[{"symbol":"AAA","time":"time-pre-market"},
                     {"symbol":"BBB","time":"time-after-hours"},   # reports TONIGHT - no catalyst
                     {"symbol":"CCC","time":"time-not-supplied"}],
       "2026-08-20":[{"symbol":"DDD","time":"time-after-hours"},
                     {"symbol":"EEE","time":"time-pre-market"}]}   # yesterday morning - stale
syms, why, _sur = LB.fetch_earnings_symbols(now, CFG)
check("today's BMO included", "AAA" in syms)
check("today's AMC excluded (news doesn't exist yet)", "BBB" not in syms, syms)
check("unknown timing excluded", "CCC" not in syms)
check("yesterday's AMC included", "DDD" in syms)
check("yesterday's BMO excluded (stale)", "EEE" not in syms, syms)
check("reason labels attached", why.get("AAA")=="today BMO" and why.get("DDD")=="prev AMC", why)
check("exactly the 2 catalysts", set(syms)=={"AAA","DDD"}, syms)

mon = ET.localize(datetime(2026,8,24,9,20))          # Monday -> prev trading day is Friday
CAL = {"2026-08-24":[], "2026-08-21":[{"symbol":"FRI","time":"time-after-hours"}]}
syms_m,_,_ = LB.fetch_earnings_symbols(mon, CFG)
check("Monday looks back to Friday, not Sunday", syms_m==["FRI"], syms_m)

CAL = {}
check("empty calendar -> empty list, no raise", LB.fetch_earnings_symbols(now,CFG)[0]==[])

def boom(d,t): raise RuntimeError("network down")
_save = LB._fetch_nasdaq_earnings
LB._fetch_nasdaq_earnings = boom
try:
    LB.fetch_earnings_symbols(now, CFG); ok=False
except RuntimeError: ok=True
check("raw fetcher errors propagate to augment_symbols' guard (by design)", ok)
LB._fetch_nasdaq_earnings = _save

CAL = {"2026-08-21":[{"symbol":"","time":"time-pre-market"},
                     {"symbol":"XYZ.WS","time":"time-pre-market"},
                     {"symbol":"GOOD","time":"time-pre-market"},
                     {"symbol":"GOOD","time":"time-pre-market"}]}
s2,_,_ = LB.fetch_earnings_symbols(now, CFG)
check("blank/warrant symbols dropped, dupes collapsed", s2==["GOOD"], s2)

print("\n=== D. QQQ TREND (majority of 3) ===")
def trend_case(gap, r5, px, closes):
    fs = FakeScreener(gap={"QQQ":gap}, r5={"QQQ":r5}, px={"QQQ":px}, bars={"QQQ":qqq_bars(closes)})
    return LB.qqq_trend(fs, CFG)
up,_ = trend_case(0.5, 2.0, 105.0, [100,101,102,103,104])
check("3/3 up -> trending up", up)
up,_ = trend_case(0.5, 2.0, 95.0, [100,101,102,103,104])
check("2/3 up -> trending up", up)
up,d = trend_case(-0.5, 2.0, 95.0, [100,101,102,103,104])
check("1/3 up -> not trending up", not up, d)
up,_ = trend_case(-0.5, -2.0, 95.0, [100,101,102,103,104])
check("0/3 -> not trending up", not up)
up,_ = trend_case(0.0, 0.0, 100.0, [100]*5)
check("dead flat (no strict >0) -> not up", not up)
class Broken(FakeScreener):
    def _get_recent_gap(s,x): raise RuntimeError("data down")
up,d = LB.qqq_trend(Broken(), CFG)
check("data failure -> not up, no raise (conservative)", up is False)

print("\n=== E. OPEN-BURST SCORE ===")
fs = FakeScreener(vol={"A":90.0,"B":10.0}, rvol={"A":3.0,"B":1.0},
                  gap={"A":2.0,"B":0.0}, r5={"A":8.0,"B":-10.0}, px={"A":50.0,"B":50.0})
sa,da = LB.open_burst_score(fs,"A",CFG); sb,db = LB.open_burst_score(fs,"B",CFG)
check("strong candidate outscores weak", sa > sb, (sa,sb))
check("score bounded 0-100", 0 <= sa <= 100 and 0 <= sb <= 100, (sa,sb))
check("max profile ~100", abs(sa - (90*0.35 + 30 + 20 + 15)) < 0.01, sa)
check("details carry components", {"movability_score","rvol_score","gap_score","trend_score"} <= set(da))

g = lambda v: LB.open_burst_score(FakeScreener(gap={"X":v}),"X",CFG)[1]["gap_score"]
check("gap curve: 0% scores ~0", g(0.0)==0)
check("gap curve: 2% is the sweet spot (max 20)", g(2.0)==20)
check("gap curve: 11% is penalised, not rewarded", g(11.0) < g(2.0), (g(11.0),g(2.0)))
check("gap curve: MRVL's 11.2% scores near-zero", g(11.2) <= 4, g(11.2))
check("gap curve: decays smoothly through 4-6%", g(4.0) > g(5.0) > g(6.0))
check("gap curve: negative gap uses magnitude", g(-2.0)==g(2.0))
check("scoring failure -> 0, no raise", LB.open_burst_score(Broken(),"X",CFG)[0]==0)

print("\n=== F. RANKING + PRICE GATES ===")
fs = FakeScreener(vol={s:50.0 for s in "ABCDE"}, rvol={"A":3.0,"B":2.0,"C":1.5,"D":1.2,"E":1.0},
                  px={"A":50.0,"B":50.0,"C":50.0,"D":50.0,"E":50.0})
top = LB.rank_top_n(fs, list("ABCDE"), CFG, 3, "T")
check("returns exactly top_n", len(top)==3, top)
check("ordered best-first", top==["A","B","C"], top)
check("excluded symbols never returned", "A" not in LB.rank_top_n(fs,list("ABCDE"),CFG,3,"T",exclude={"A"}))
check("empty pool -> empty", LB.rank_top_n(fs,[],CFG,3,"T")==[])
check("all excluded -> empty", LB.rank_top_n(fs,list("ABC"),CFG,3,"T",exclude=set("ABC"))==[])
check("top_n larger than pool -> whole pool", len(LB.rank_top_n(fs,list("AB"),CFG,10,"T"))==2)
cheap = FakeScreener(px={"A":2.70,"B":50.0}, rvol={"A":9.0,"B":1.0})
check(f"min_stock_price {CFG['trading']['min_stock_price']} filters penny names even when top-ranked",
      LB.rank_top_n(cheap,["A","B"],CFG,5,"T")==["B"], LB.rank_top_n(cheap,["A","B"],CFG,5,"T"))
check("input order does not affect ranking",
      LB.rank_top_n(fs,list("EDCBA"),CFG,3,"T")==["A","B","C"])
check("duplicate inputs collapse", len(LB.rank_top_n(fs,list("AABBCC"),CFG,10,"T"))==3)

print("\n=== G. AUGMENT_SYMBOLS ===")
CAL = {"2026-08-21":[{"symbol":"ERNA","time":"time-pre-market","surprise":"12.0"},
                     {"symbol":"ERNB","time":"time-pre-market","surprise":"7.5"}], "2026-08-20":[]}
def mkfs(qqq_up=True):
    names = list("Z")+["ERNA","ERNB"]+LB.load_qqq_constituents()
    return FakeScreener(vol={n:60.0 for n in names+["QQQ"]},
                        rvol={n:2.0 for n in names+["QQQ"]},
                        gap={**{n:1.5 for n in names}, "QQQ": 0.5 if qqq_up else -0.5},
                        r5={**{n:3.0 for n in names}, "QQQ": 2.0 if qqq_up else -2.0},
                        px={**{n:50.0 for n in names}, "QQQ":100.0},
                        bars={"QQQ":qqq_bars([100,100,100,100,100] if qqq_up else [200]*5)})
cfg = copy.deepcopy(CFG)
full, added = LB.augment_symbols(cfg, mkfs(True), ["MARA","RIOT"], now)
check("existing symbols preserved, in order", full[:2]==["MARA","RIOT"], full[:2])
check("earnings names added", {"ERNA","ERNB"} <= set(added), added)
check("QQQ names added when trending up", len(added) > 2, added)
check("QQQ cut to top_n", len([a for a in added if a in LB.load_qqq_constituents()]) <= cfg["trading"]["qqq_list_top_n"])
check("result deduped", len(full)==len(set(full)))
check("never shrinks the watchlist", len(full) >= 2)

full2, added2 = LB.augment_symbols(cfg, mkfs(False), ["MARA"], now)
check("QQQ list skipped when not trending up",
      not (set(added2) & set(LB.load_qqq_constituents()) - {"ERNA","ERNB"}), added2)
check("earnings still added on a down-QQQ day", {"ERNA","ERNB"} <= set(added2), added2)

offcfg = copy.deepcopy(CFG); offcfg["trading"]["use_earnings_list"]=False; offcfg["trading"]["use_qqq_list"]=False
f3,a3 = LB.augment_symbols(offcfg, mkfs(True), ["MARA","RIOT"], now)
check("both toggles off -> watchlist byte-identical", f3==["MARA","RIOT"] and a3==[], (f3,a3))

check("no screener -> unchanged, no raise", LB.augment_symbols(cfg,None,["MARA"],now)==(["MARA"],[]))

LB._fetch_nasdaq_earnings = boom
f4,a4 = LB.augment_symbols(cfg, mkfs(True), ["MARA"], now)
check("earnings blowup contained, QQQ list still runs", "MARA" in f4 and len(a4)>0, (len(a4),))
LB._fetch_nasdaq_earnings = fake_fetch

class AllBroken(FakeScreener):
    def _get_volatility_percentile(s,x): raise RuntimeError("down")
    def _get_recent_gap(s,x): raise RuntimeError("down")
f5,a5 = LB.augment_symbols(cfg, AllBroken(), ["MARA","RIOT"], now)
check("total data outage -> original list survives intact", f5[:2]==["MARA","RIOT"], f5)

dupcfg = copy.deepcopy(CFG)
f6,a6 = LB.augment_symbols(dupcfg, mkfs(True), ["ERNA","AAPL"], now)
check("already-watched names are not re-added", "ERNA" not in a6 and "AAPL" not in a6, a6)

print("\n=== H. MAIN WIRING ===")
src = open(repo_file("src", "main.py")).read()
check("augment_symbols imported", "from src.screener.list_builder import augment_symbols" in src)
check("_augment_selection defined", "def _augment_selection(" in src)
check("scheduled slot reads list_builder_start_time", 'list_builder_start_time' in src)
check("gated on pending_augmented", "pending_augmented" in src)
check("late-start path augments too", src.count("_augment_selection(") >= 2)
check("flag reset when selection is consumed", "pending_augmented = False" in src)
check("RSI refreshed for added names", "RSI unavailable for added symbol" in src)
check("config: list_builder_start_time before the open",
      CFG["trading"]["list_builder_start_time"] < "09:30")
check("config: augmentation runs after the screener",
      CFG["trading"]["screener_start_time"] < CFG["trading"]["list_builder_start_time"])

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
