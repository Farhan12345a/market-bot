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

print("\n=== H. SHORT-SIDE EVIDENCE GATHERING (2026-09-23) ===")
# Same class, a second instance pointed at its own file/flag - see
# SignalJournal.__init__'s path/enabled overrides.
d10=tempfile.mkdtemp(); p10=os.path.join(d10,"long.csv"); p10s=os.path.join(d10,"short.csv")
c10=copy.deepcopy(CFG)
c10["analytics"]={"log_signals":True,"signal_log_file":p10,
                   "forward_return_minutes":[15,30],
                   "log_short_signals":True,"short_signal_log_file":p10s}
jlong=SignalJournal(c10)
jshort=SignalJournal(c10, path=c10["analytics"]["short_signal_log_file"],
                      enabled=c10["analytics"]["log_short_signals"])
check("the two instances use DIFFERENT paths", jlong.path != jshort.path,
      (jlong.path, jshort.path))
check("short path came from the override, not the long-side default",
      jshort.path == p10s, jshort.path)

rec(jlong,"UPMOVE"); jlong.flush(final=True)
rec(jshort,"DOWNMOVE"); jshort.flush(final=True)
check("a long-side record never lands in the short file",
      "UPMOVE" not in "".join(x["symbol"] for x in rows(p10s)))
check("a short-side record never lands in the long file",
      "DOWNMOVE" not in "".join(x["symbol"] for x in rows(p10)))
check("short file actually got its row", any(x["symbol"]=="DOWNMOVE" for x in rows(p10s)))

c11=copy.deepcopy(CFG)
c11["analytics"]={"log_signals":True,"signal_log_file":os.path.join(tempfile.mkdtemp(),"l.csv"),
                   "forward_return_minutes":[15,30]}
jshort_off=SignalJournal(c11, path=os.path.join(tempfile.mkdtemp(),"s.csv"), enabled=False)
rec(jshort_off,"X")
check("enabled=False on the override disables that instance independently "
      "of the long side's own log_signals flag",
      jshort_off.flush(final=True) is None)

check("short_candidate_pct is configured", "short_candidate_pct" in CFG["trading"])
check("...defaults equal to rapid_increase_pct as the least-biased starting point",
      CFG["trading"]["short_candidate_pct"] == CFG["trading"]["rapid_increase_pct"])
sa=CFG["analytics"]
check("log_short_signals configured", sa.get("log_short_signals") is True)
check("short_signal_log_file configured",
      sa.get("short_signal_log_file") == "logs/short_signal_journal.csv")

print("\n=== I. MAIN WIRING - SHORT SIDE IS RECORD-ONLY, NEVER TRADED ===")
check("run_trading_day accepts a short_signal_journal parameter",
      "short_signal_journal=None" in src)
check("a second SignalJournal instance is created for it (both call sites - "
      "the default fallback inside run_trading_day, and main()'s own setup)",
      src.count("short_signal_log_file") >= 2, src.count("short_signal_log_file"))
check("short candidates flow through their own list, never burst_candidates",
      "short_candidates = []" in src and "short_candidates.append(" in src)
check("short candidates are recorded directly - taken is a hardcoded False, "
      "never a variable a decision could have set",
      'taken=False, skip_reason="short_side_not_traded_evidence_only"' in src)
check("the short-candidate block contains no _attempt_entry call at all "
      "(the safety property that makes this purely observational)",
      "_attempt_entry(" not in src.split("SHORT-SIDE EVIDENCE GATHERING")[1].split(
          "Best-first, so the throttle")[0])
check("both journals flush incrementally every poll",
      "short_signal_journal.flush(final=False)" in src)
check("both journals get their forward returns updated every poll",
      "short_signal_journal.update_forward_returns(" in src)
check("both journals flush on finish_day", "short_signal_journal.flush()" in src)
check("both journals flush on a crash/interrupt, not just the long one",
      src.count("_flush_journal_safely(short_signal_journal)") >= 2)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
