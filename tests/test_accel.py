import sys
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
from src.main import _check_three_bar_momentum

def bars(*closes, green=True):
    """Build green bars with the given closes (open just under close)."""
    out = []
    prev = closes[0] - 0.02
    for c in closes:
        o = prev if green else c + 0.01
        out.append({"open": o, "close": c})
        prev = c
    return out

fails = []
def check(name, got, want):
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   got={got} want={want}"))
    if not ok: fails.append(name)

print("--- the two cases from the spec ---")
check("9.51->9.55->9.56 rejected (decelerating)", _check_three_bar_momentum(bars(9.51,9.55,9.56)), False)
check("9.51->9.53->9.58 accepted (accelerating)", _check_three_bar_momentum(bars(9.51,9.53,9.58)), True)

print("--- acceleration boundary ---")
check("equal gaps rejected (.02/.02)", _check_three_bar_momentum(bars(9.50,9.52,9.54)), False)
check("barely accelerating accepted (.02/.03)", _check_three_bar_momentum(bars(9.50,9.52,9.55)), True)
check("barely decelerating rejected (.03/.02)", _check_three_bar_momentum(bars(9.50,9.53,9.55)), False)
check("strongly accelerating accepted", _check_three_bar_momentum(bars(10.00,10.01,10.50)), True)

print("--- still enforces the original conditions ---")
check("flat close rejected", _check_three_bar_momentum(bars(9.50,9.50,9.58)), False)
check("falling close rejected", _check_three_bar_momentum(bars(9.60,9.50,9.58)), False)
check("red bars rejected even if accelerating",
      _check_three_bar_momentum(bars(9.51,9.53,9.58, green=False)), False)
check("fewer than 3 bars rejected", _check_three_bar_momentum(bars(9.51,9.53)), False)
check("empty rejected", _check_three_bar_momentum([]), False)

print("--- one red bar in an otherwise accelerating run ---")
b = bars(9.51,9.53,9.58); b[1]["open"] = b[1]["close"] + 0.01
check("middle bar red rejected", _check_three_bar_momentum(b), False)

print("--- toggle off restores old behavior ---")
check("decelerating accepted when toggle off",
      _check_three_bar_momentum(bars(9.51,9.55,9.56), require_acceleration=False), True)
check("equal gaps accepted when toggle off",
      _check_three_bar_momentum(bars(9.50,9.52,9.54), require_acceleration=False), True)
check("falling still rejected when toggle off",
      _check_three_bar_momentum(bars(9.60,9.50,9.58), require_acceleration=False), False)
check("red still rejected when toggle off",
      _check_three_bar_momentum(bars(9.51,9.53,9.58, green=False), require_acceleration=False), False)

print("--- float-noise sanity (tiny real gaps still count) ---")
check("sub-cent accelerating accepted", _check_three_bar_momentum(bars(9.5000,9.5010,9.5030)), True)
check("sub-cent decelerating rejected", _check_three_bar_momentum(bars(9.5000,9.5030,9.5040)), False)

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
