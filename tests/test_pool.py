"""stock_universe as a candidate pool + watchlist-vs-stream warning."""
import sys, copy, types, yaml, logging, tempfile, os
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.main as M
from src.screener.stock_screener import StockScreener
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

class FakeScreener:
    def __init__(s, picks, candidates=None, boom=False, hang=False):
        s.picks=picks; s.candidates=candidates or []; s.boom=boom; s.hang=hang
    def screen(s, top_n=15, min_score=35):
        if s.boom: raise RuntimeError("screener died")
        if s.hang:
            import time; time.sleep(30)
        return list(s.picks)
class FakeMD:
    def get_rsi(s,sym,period=14): return 50.0

def run(cfg, picks, **kw):
    M.stream_priority["symbols"]=[]
    return M.select_symbols(cfg, FakeScreener(picks, **kw), FakeMD())

DEF = CFG["trading"]["stock_universe"]
print("=== A. POOL MODE (merge off) ===")
c=copy.deepcopy(CFG); c["trading"]["merge_default_universe"]=False
syms,_=run(c, ["AAA","BBB","CCC"])
check("watches ONLY the screener's picks", syms==["AAA","BBB","CCC"], syms[:6])
check("the 50 defaults are NOT auto-included", not (set(DEF) & set(syms)), set(DEF)&set(syms))
check("watchlist is small enough to stream", len(syms) <= 14, len(syms))
check("stream priority seeded with the picks", M.stream_priority["symbols"]==["AAA","BBB","CCC"])

print("\n=== B. MERGE MODE STILL WORKS (old behaviour) ===")
c2=copy.deepcopy(CFG); c2["trading"]["merge_default_universe"]=True
syms2,_=run(c2, ["AAA","BBB"])
check("merge on -> picks + full default list", len(syms2)==len(dict.fromkeys(["AAA","BBB"]+DEF)), len(syms2))
check("picks come first", syms2[:2]==["AAA","BBB"])
check("defaults present", set(DEF) <= set(syms2))

print("\n=== C. FALLBACK: NEVER TRADE AN EMPTY WATCHLIST ===")
syms3,_=run(c, [])
check("no picks -> falls back to the static list", set(syms3)==set(DEF), len(syms3))
syms4,_=run(c, [], boom=True)
check("screener CRASH -> falls back, no raise", set(syms4)==set(DEF), len(syms4))
c5=copy.deepcopy(c); c5["trading"]["screener_timeout_seconds"]=1
syms5,_=run(c5, ["X"], hang=True)
check("screener TIMEOUT -> falls back", set(syms5)==set(DEF), len(syms5))
c6=copy.deepcopy(c); c6["trading"]["use_daily_screener"]=False
syms6,_=run(c6, ["X"])
check("screener disabled -> static list", set(syms6)==set(DEF))
check("fallback list is never empty", len(syms3)>0 and len(syms4)>0 and len(syms5)>0)

print("\n=== D. DEFAULTS STILL GET SCORED ===")
d=tempfile.mkdtemp(); cf=os.path.join(d,"cand.txt")
open(cf,"w").write("AAA\nBBB\nMARA\n")
sc=StockScreener(types.SimpleNamespace(), cf, extra_candidates=["MARA","RIOT","HUT"])
check("pool = file + extras", set(sc.candidates)=={"AAA","BBB","MARA","RIOT","HUT"}, sc.candidates)
check("no duplicates across the two sources", len(sc.candidates)==len(set(sc.candidates)))
check("file order preserved first", sc.candidates[:3]==["AAA","BBB","MARA"], sc.candidates)
sc2=StockScreener(types.SimpleNamespace(), cf)
check("no extras -> unchanged behaviour", sc2.candidates==["AAA","BBB","MARA"])
sc3=StockScreener(types.SimpleNamespace(), "/nope/x.txt", extra_candidates=["ONLY"])
check("missing file + extras -> extras still usable", sc3.candidates==["ONLY"], sc3.candidates)
sc4=StockScreener(types.SimpleNamespace(), cf, extra_candidates=["  hut  ","", None])
check("extras normalised and blanks dropped", "HUT" in sc4.candidates and "" not in sc4.candidates, sc4.candidates)
msrc=open(repo_file("src", "main.py")).read()
check("main passes stock_universe as extras only when merge is off",
      'else config["trading"].get("stock_universe", [])' in msrc)

print("\n=== E. WATCHLIST vs STREAM WARNING ===")
class Cap:
    def __init__(s): s.msgs=[]
    def handle(s,r): s.msgs.append(r.getMessage())
cap=Cap(); h=logging.Handler(); h.emit=cap.handle
lg=logging.getLogger("src.main"); lg.addHandler(h); lg.setLevel(logging.INFO)
cap.msgs.clear(); M._warn_if_watchlist_outruns_the_stream(CFG, [f"S{i}" for i in range(14)])
check("14 watched, 14 budget -> 'every tradeable name gets live prices'",
      any("every tradeable name" in m for m in cap.msgs), cap.msgs)
cap.msgs.clear(); M._warn_if_watchlist_outruns_the_stream(CFG, [f"S{i}" for i in range(59)])
check("59 watched -> explicit warning", any("exceeds the stream budget" in m for m in cap.msgs), cap.msgs)
check("names how many run on REST", any("45 symbol" in m for m in cap.msgs), cap.msgs)
check("quantifies it as a percentage", any("76% of the" in m for m in cap.msgs), cap.msgs)
noticks=copy.deepcopy(CFG); noticks["trading"]["use_trade_ticks_for_entry"]=False
cap.msgs.clear(); M._warn_if_watchlist_outruns_the_stream(noticks, [f"S{i}" for i in range(28)])
check("ticks off -> budget doubles to 28, no warning",
      any("every tradeable name" in m for m in cap.msgs), cap.msgs)
off=copy.deepcopy(CFG); off["trading"]["use_websocket_stream"]=False
cap.msgs.clear(); M._warn_if_watchlist_outruns_the_stream(off, ["A","B"])
check("stream off -> says so plainly", any("stream is OFF" in m for m in cap.msgs), cap.msgs)
cap.msgs.clear(); M._warn_if_watchlist_outruns_the_stream(CFG, [])
check("empty watchlist -> no crash, no divide-by-zero", True)
lg.removeHandler(h)

print("\n=== F. CONFIG ===")
t=CFG["trading"]
check("merge_default_universe is off", t["merge_default_universe"] is False)
check("num_stocks_to_trade set", isinstance(t["num_stocks_to_trade"], int) and t["num_stocks_to_trade"]>0, t["num_stocks_to_trade"])
budget = t["stream_max_subscriptions"] // (2 if t["use_trade_ticks_for_entry"] else 1)
check(f"top-N ({t['num_stocks_to_trade']}) close to the stream budget ({budget})",
      abs(t["num_stocks_to_trade"] - budget) <= 2, (t["num_stocks_to_trade"], budget))
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
