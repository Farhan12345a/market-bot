"""Gaps found by the coverage audit: continuation factors, adaptive polling,
post-exit notes, and the signal ceiling's actual gate behaviour."""
import sys, copy, types, yaml, time
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.analytics.continuation as C
import src.main as M
from src.executor.executor import Executor
CFG=yaml.safe_load(open(CONFIG))
P=F=0
def check(n,c,d=""):
    global P,F
    if c: P+=1; print(f"PASS  {n}")
    else: F+=1; print(f"FAIL  {n}   <- {d}")

print("=== A. EFFICIENCY RATIO (trend vs whipsaw) ===")
trend=[100,105,107,106,108,110]; whip=[100,108,104,109,103,110]
check("both paths end at the same price", trend[-1]==whip[-1])
check("trend scores far higher than whipsaw", C.efficiency_ratio(trend) > C.efficiency_ratio(whip)*2,
      (C.efficiency_ratio(trend), C.efficiency_ratio(whip)))
check("a straight line scores 100", C.efficiency_ratio([100,102,104,106])==100.0)
check("a net DOWN move scores 0", C.efficiency_ratio([100,99,98,97])==0.0)
check("flat -> None (no signal, not a bad one)", C.efficiency_ratio([100,100,100])is None)
check("too few points -> None", C.efficiency_ratio([100,101]) is None)
check("empty -> None", C.efficiency_ratio([]) is None)
check("bounded 0-100", 0 <= C.efficiency_ratio(trend) <= 100)

print("\n=== B. RELATIVE STRENGTH ===")
check("matching the benchmark = 50", C.relative_strength(0.5,0.5)==50)
check("+2% excess saturates at 100", C.relative_strength(2.5,0.5)==100)
check("underperforming scores below 50", C.relative_strength(0.2,0.8)<50)
check("-2% excess floors at 0", C.relative_strength(0.0,3.0)==0)
check("missing benchmark -> None", C.relative_strength(1.0,None) is None)
check("beta move scores near 50 (the point of the factor)",
      abs(C.relative_strength(0.8,0.7)-50)<5, C.relative_strength(0.8,0.7))

print("\n=== C. VOLUME ACCELERATION ===")
rising=[100,150,200,300,450,700,1100]; falling=[1000,700,500,400,300,200,100]
check("rising volume scores high", C.volume_acceleration(rising)>70, C.volume_acceleration(rising))
check("falling volume scores low", C.volume_acceleration(falling)<30, C.volume_acceleration(falling))
check("flat volume ~50", abs(C.volume_acceleration([100]*8)-50)<1)
check("too few samples -> None", C.volume_acceleration([100,200,300]) is None)
check("all zeros -> None, no divide-by-zero", C.volume_acceleration([0]*8) is None)

print("\n=== D. RVOL + SPREAD ===")
check("1.0x normal = 50", C.relative_volume(1.0)==50)
check("2.5x saturates at 100", C.relative_volume(2.5)==100)
check("half normal scores low", C.relative_volume(0.5)<35)
check("None/zero -> None", C.relative_volume(None) is None and C.relative_volume(0) is None)
check("tight spread scores 100", C.spread_quality(0.05)==100)
check("wide spread scores 0", C.spread_quality(0.5)==0)
check("mid spread is between", 0 < C.spread_quality(0.25) < 100)
check("negative spread -> None", C.spread_quality(-1) is None)

print("\n=== E. VWAP POSITION + EXHAUSTION ===")
check("at VWAP = 50", C.vwap_position(100,100)==50)
check("above VWAP > 50", C.vwap_position(101,100)>50)
check("below VWAP < 50", C.vwap_position(99,100)<50)
check("no vwap -> None", C.vwap_position(100,None) is None)
check("zero vwap -> None, no divide-by-zero", C.vwap_position(100,0) is None)
check("small signal = low exhaustion", C.exhaustion(0.4,None,None)<15)
check("2% signal = fully exhausted", C.exhaustion(2.0,None,None)==100)
check("exhaustion rises with signal size",
      C.exhaustion(0.4,None,None) < C.exhaustion(1.0,None,None) < C.exhaustion(1.8,None,None))
check("stretched from VWAP raises it even on a small signal",
      C.exhaustion(0.4,110,100) > C.exhaustion(0.4,100,100))
check("no signal -> None", C.exhaustion(None,100,100) is None)

print("\n=== F. BREAKOUT QUALITY ===")
check("clearing both levels = 100", C.breakout_quality(105,100,102)==100)
check("prior-day high is worth more than opening range",
      C.breakout_quality(101,100,102) > C.breakout_quality(103,110,102))
check("clearing neither = 0", C.breakout_quality(99,100,102)==0)
check("no levels known -> None", C.breakout_quality(100,None,None) is None)

print("\n=== G. SCORE COMPOSITION ===")
W=CFG["trading"]["continuation_weights"]
strong={"efficiency":90,"rel_strength":85,"vol_accel":80,"rvol":80,"vwap_pos":75,"breakout":90,"spread":90,"exhaustion":10}
weak={"efficiency":20,"rel_strength":30,"vol_accel":25,"rvol":20,"vwap_pos":30,"breakout":0,"spread":40,"exhaustion":95}
check("strong profile outscores weak", C.continuation_score(strong,W) > C.continuation_score(weak,W)*1.5,
      (C.continuation_score(strong,W), C.continuation_score(weak,W)))
check("bounded 0-100", 0 <= C.continuation_score(strong,W) <= 100)
check("high exhaustion DRAGS the score down",
      C.continuation_score({**strong,"exhaustion":95},W) < C.continuation_score(strong,W))
check("missing factors are dropped, not scored as zero",
      C.continuation_score({"efficiency":90,"rel_strength":None},W) > 80,
      C.continuation_score({"efficiency":90,"rel_strength":None},W))
check("all missing -> None", C.continuation_score({"efficiency":None},W) is None)
check("no weights -> None", C.continuation_score(strong,{}) is None)

print("\n=== H. ADAPTIVE POLLING ===")
class MD:
    def __init__(s,healthy): s.stream=types.SimpleNamespace(is_healthy=lambda: healthy)
st={"last":None}
check("stream healthy -> fast interval", M._poll_interval(CFG,MD(True),10,60,st)==10)
check("stream down -> slow interval", M._poll_interval(CFG,MD(False),10,60,st)==60)
check("recovers to fast automatically", M._poll_interval(CFG,MD(True),10,60,st)==10)
class NoStream: stream=None
check("no stream at all -> slow", M._poll_interval(CFG,NoStream(),10,60,st)==60)
class Boom:
    stream=types.SimpleNamespace(is_healthy=lambda: (_ for _ in ()).throw(RuntimeError("x")))
check("is_healthy raising -> slow, no crash", M._poll_interval(CFG,Boom(),10,60,st)==60)

print("\n=== I. POST-EXIT NOTES ===")
def note(pl, pct):
    e=Executor(types.SimpleNamespace(), copy.deepcopy(CFG))
    row={"pl":pl,"symbol":"X"}
    e._post_exit_pending=[{"row":row,"symbol":"X","exit_price":100.0,"due_at":time.time()-1}]
    e.note_post_exit_prices(lambda s: 100.0*(1+pct/100))
    return row.get("post_exit_note"), row.get("post_exit_pct")
check("loser that kept falling -> 'exit was right'", "exit was right" in note(-50, -1.0)[0], note(-50,-1.0))
check("loser that bounced -> 'exit was early'", "exit was early" in note(-50, 1.0)[0], note(-50,1.0))
check("winner that ran further", "ran further" in note(50, 1.0)[0], note(50,1.0))
check("winner that gave it back", "gave it back" in note(50, -1.0)[0], note(50,-1.0))
check("small move -> flat", note(-50, 0.05)[0]=="flat", note(-50,0.05))
check("percentage recorded", abs(note(-50,-1.0)[1]+1.0)<0.01)
e=Executor(types.SimpleNamespace(), copy.deepcopy(CFG))
row={"pl":-10}; e._post_exit_pending=[{"row":row,"symbol":"X","exit_price":100.0,"due_at":time.time()+9999}]
e.note_post_exit_prices(lambda s: 105.0)
check("not due yet -> untouched", "post_exit_pct" not in row)
check("still pending", len(e._post_exit_pending)==1)
e2=Executor(types.SimpleNamespace(), copy.deepcopy(CFG))
row2={"pl":-10}; e2._post_exit_pending=[{"row":row2,"symbol":"X","exit_price":100.0,"due_at":time.time()-1}]
e2.note_post_exit_prices(lambda s: None)
check("no price available -> skipped, no crash", "post_exit_pct" not in row2)
e3=Executor(types.SimpleNamespace(), copy.deepcopy(CFG))
e3.note_post_exit_prices(lambda s: 100.0)
check("empty queue -> no crash", True)

print("\n=== J. SIGNAL CEILING GATE ===")
src=open(repo_file("src", "main.py")).read()
check("ceiling is read from config", 'get("rapid_increase_max_pct", 0)' in src)
check("compares the signal against it", "pct_change > max_signal" in src)
check("skips rather than entering", "signal skipped" in src)
check("streamed-only mode exempts REST symbols", 'rapid_increase_max_pct_streamed_only' in src)
check("exemption works by zeroing the ceiling", "max_signal = 0" in src)
# 2.0 -> 1.25 for 2026-08-27. The 2.0 setting never bound once: the largest
# signal on 2026-08-26 was 1.452%, so it had refused nothing since it shipped
# and could not be evaluated at all. What the ceiling must satisfy - that it
# sits above the entry floor, and below the top of the observed distribution
# so it actually produces a control group - is asserted rather than the literal.
check("live ceiling is 1.25", CFG["trading"]["rapid_increase_max_pct"]==1.25,
      CFG["trading"]["rapid_increase_max_pct"])
check("ceiling is above the entry floor",
      CFG["trading"]["rapid_increase_max_pct"] > CFG["trading"]["rapid_increase_pct"])
check("ceiling is low enough to bind on a normal session (2026-08-26 peak 1.452%)",
      CFG["trading"]["rapid_increase_max_pct"] < 1.452)
check("streamed-only is on", CFG["trading"]["rapid_increase_max_pct_streamed_only"] is True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
