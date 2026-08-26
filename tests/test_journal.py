"""Signal journal durability: incremental flush, crash safety, day-over-day append."""
import sys, os, csv, copy, tempfile, yaml, time
from datetime import datetime, timedelta
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.analytics.signal_journal as SJ
from src.analytics.signal_journal import SignalJournal

CFG = yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

def mk(path, horizons=(15,30)):
    c=copy.deepcopy(CFG)
    c["analytics"]={"log_signals":True,"signal_log_file":path,
                    "forward_return_minutes":list(horizons)}
    return SignalJournal(c)

def rec(j, sym, taken=True, price=100.0):
    j.record(symbol=sym, price=price, signal_type="RAPID_INCREASE", signal_pct=0.4,
             spy_pct=0.1, excess_vs_spy_pct=0.3, rvol=1.5, spread_pct=0.02,
             burst_width=2, taken=taken, skip_reason=None, qty=10, size_multiplier=1.0)

def rows(path):
    if not os.path.exists(path): return []
    return list(csv.DictReader(open(path)))

print("=== A. CONFIG ===")
a=CFG["analytics"]
check("log_signals enabled", a["log_signals"] is True)
check("path configured", a["signal_log_file"]=="logs/signal_journal.csv")
check("forward horizons configured", a["forward_return_minutes"]==[15,30])

print("\n=== B. INCREMENTAL FLUSH KEEPS INCOMPLETE ROWS ===")
d=tempfile.mkdtemp(); p=os.path.join(d,"j.csv")
j=mk(p); rec(j,"AAA"); rec(j,"BBB")
check("nothing written before any flush", rows(p)==[])
check("incremental flush writes nothing while horizons pending", j.flush(final=False) is None)
check("rows still buffered", len(j._pending)==2, len(j._pending))
# age one row past the 30-min horizon
j._pending[0]["born"] = datetime.now() - timedelta(minutes=31)
j.flush(final=False)
r=rows(p)
check("mature row written", len(r)==1 and r[0]["symbol"]=="AAA", r)
check("immature row still buffered", len(j._pending)==1 and j._pending[0]["symbol"]=="BBB")
check("header written once", open(p).readline().startswith("date,") or "symbol" in open(p).readline())
j._pending[0]["born"] = datetime.now() - timedelta(minutes=31)
j.flush(final=False)
check("second row written on its own schedule", len(rows(p))==2, len(rows(p)))
check("header NOT duplicated", sum(1 for l in open(p) if l.startswith("date,"))<=1)

print("\n=== C. FINAL FLUSH WRITES EVERYTHING ===")
d2=tempfile.mkdtemp(); p2=os.path.join(d2,"j.csv")
j2=mk(p2); rec(j2,"LATE1"); rec(j2,"LATE2")
j2.flush(final=True)
r2=rows(p2)
check("late signals written despite immature horizons", len(r2)==2, len(r2))
check("their forward-return columns are blank, not fabricated",
      r2[0].get("pct_30min") in (None,"",), r2[0].get("pct_30min"))
check("buffer emptied", j2._pending==[])

print("\n=== D. CRASH SAFETY (the actual bug) ===")
d3=tempfile.mkdtemp(); p3=os.path.join(d3,"j.csv")
j3=mk(p3)
for i in range(5): rec(j3,f"S{i}")
for e in j3._pending: e["born"]=datetime.now()-timedelta(minutes=31)
j3.flush(final=False)          # what the poll loop now does
check("poll-loop flush persists matured signals mid-session", len(rows(p3))==5, len(rows(p3)))
# simulate a hard kill: new object, same file, no finish_day ever ran
j3b=mk(p3)
check("data survives a process restart", len(rows(p3))==5)
for i in range(3): rec(j3b,f"T{i}")
for e in j3b._pending: e["born"]=datetime.now()-timedelta(minutes=31)
j3b.flush(final=False)
check("next session APPENDS, never truncates", len(rows(p3))==8, len(rows(p3)))
syms=[x["symbol"] for x in rows(p3)]
check("both sessions' symbols present", "S0" in syms and "T0" in syms, syms)

print("\n=== E. DAY-OVER-DAY ACCUMULATION ===")
d4=tempfile.mkdtemp(); p4=os.path.join(d4,"j.csv")
total=0
for day in range(5):
    jd=mk(p4)
    for i in range(4): rec(jd,f"D{day}S{i}")
    jd.flush(final=True); total+=4
check("5 sessions accumulate into one file", len(rows(p4))==total, len(rows(p4)))
check("exactly one header across 5 sessions",
      sum(1 for l in open(p4) if l.startswith("date,"))==1,
      sum(1 for l in open(p4) if l.startswith("date,")))
check("every row carries a date", all(x.get("date") for x in rows(p4)))
dates={x["date"] for x in rows(p4)}
check("date column populated", len(dates)>=1, dates)

print("\n=== F. CONTROL GROUP + ROBUSTNESS ===")
d5=tempfile.mkdtemp(); p5=os.path.join(d5,"j.csv")
j5=mk(p5); rec(j5,"TAKEN",taken=True); rec(j5,"SKIPPED",taken=False)
j5.flush(final=True); r5=rows(p5)
check("skipped signals recorded too (the control group)",
      any(x["taken"] in ("False","false") for x in r5), [x["taken"] for x in r5])
check("taken signals recorded", any(x["taken"] in ("True","true") for x in r5))
check("stats counts both", j5.stats()["written"]==2, j5.stats())

j6=mk(os.path.join(tempfile.mkdtemp(),"j.csv"))
check("flush with empty buffer -> None, no file, no raise", j6.flush(final=True) is None)
off=copy.deepcopy(CFG); off["analytics"]={"log_signals":False,"signal_log_file":"x.csv"}
j7=SignalJournal(off); rec(j7,"X")
check("disabled -> records nothing, writes nothing", j7.flush(final=True) is None)
j8=mk("/proc/self/cannot-create-here/j.csv"); rec(j8,"X")
check("unwritable path -> returns None, does not raise into the loop",
      j8.flush(final=True) is None)

d9=tempfile.mkdtemp(); p9=os.path.join(d9,"j.csv")
j9=mk(p9, horizons=())
rec(j9,"NOHORIZON")
check("no horizons configured -> row is immediately complete", j9.flush(final=False) is not None)
check("...and written", len(rows(p9))==1)

print("\n=== G. MAIN WIRING ===")
src=open(repo_file("src", "main.py")).read()
check("poll loop flushes incrementally", "signal_journal.flush(final=False)" in src)
check("finish_day still does a final flush", "signal_journal.flush()" in src)
check("KeyboardInterrupt path flushes", src.count("_flush_journal_safely(signal_journal)")>=2)
check("shutdown flush is exception-guarded", "Could not flush the signal journal on shutdown" in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
