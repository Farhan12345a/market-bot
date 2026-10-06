"""
trading.conviction_gate (2026-10-06, OFF by default) - a day-level size
scalar driven by SIGNAL QUALITY, orthogonal to regime_sizing (which only
ever says long/short/neither).

Built after 2026-10-05: regime read bullish all session (both indices above
VWAP), longs were the only side gated open, and the day still lost -$142.93
at a 31% win rate - SPY/QQQ barely moved all day and the entire signal pool
that day (signal_journal.csv, taken or not) averaged a NEGATIVE forward
return before any selection even happened. regime_sizing cannot see that
distinction; it only reads price vs VWAP.

Covers _compute_conviction_read as a pure function (reading a fixture
logs/daily/ tree), its composition into _position_size via
executor.conviction_size_multiplier (the same multiplicative pattern
regime_size_multiplier and loss_tier_multiplier already use), and the
shadow-mode default that keeps it from touching real sizing the day it's
turned on.
"""
import copy
import csv
import os
import sys
import types
import yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.main as M
from src.executor.executor import Executor

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def write_day(root, date, pct_15min_values):
    d = os.path.join(root, "logs", "daily", date)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "signal_journal.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "pct_15min"])
        for v in pct_15min_values:
            w.writerow(["XYZ", v])


print("=== A. OFF BY DEFAULT - the config ships inert ===")
check("conviction_gate.enabled is false in the shipped config",
      CFG["trading"]["conviction_gate"]["enabled"] is False)
check("shadow_mode defaults true, so even a future enable doesn't bite live "
      "sizing the same day",
      CFG["trading"]["conviction_gate"]["shadow_mode"] is True)

print("\n=== B. _compute_conviction_read: OFF -> an immediate no-op, no I/O ===")
off_cfg = copy.deepcopy(CFG)
off_cfg["trading"]["conviction_gate"]["enabled"] = False
mult, label, pool = M._compute_conviction_read(off_cfg, repo_root="/does/not/exist")
check("multiplier is 1.0", mult == 1.0, mult)
check("label is None (nothing was computed)", label is None, label)
check("pool return is None too", pool is None, pool)

print("\n=== C. A WEAK trailing pool -> shadow label 'weak', but shadow_mode "
      "keeps the real multiplier at 1.0 ===")
root = sandbox_cwd()
# 3 trailing days, all clearly negative mean pct_15min (well past the -0.05
# default threshold), today's own (not-yet-complete) date excluded by
# construction since _compute_conviction_read only looks at COMPLETED days.
write_day(root, "2026-10-01", [-0.3, -0.5, 0.1])
write_day(root, "2026-10-02", [-0.2, -0.4])
write_day(root, "2026-10-05", [-0.6, 0.0, -0.1])
on_shadow = copy.deepcopy(CFG)
on_shadow["trading"]["conviction_gate"]["enabled"] = True
on_shadow["trading"]["conviction_gate"]["shadow_mode"] = True
mult_s, label_s, pool_s = M._compute_conviction_read(on_shadow, repo_root=root)
check("label reads 'weak' off the trailing pool", label_s == "weak", label_s)
check("the pooled mean is actually negative (sanity on the fixture math)",
      pool_s is not None and pool_s < 0, pool_s)
check("...but the returned multiplier stays 1.0 - SHADOW MODE never touches "
      "real sizing", mult_s == 1.0, mult_s)

print("\n=== D. THE SAME WEAK POOL, shadow_mode: false -> the real multiplier "
      "actually moves ===")
on_live = copy.deepcopy(CFG)
on_live["trading"]["conviction_gate"]["enabled"] = True
on_live["trading"]["conviction_gate"]["shadow_mode"] = False
on_live["trading"]["conviction_gate"]["weak_size_multiplier"] = 0.4
mult_l, label_l, pool_l = M._compute_conviction_read(on_live, repo_root=root)
check("label still reads 'weak'", label_l == "weak", label_l)
check("the multiplier now actually reflects weak_size_multiplier",
      mult_l == 0.4, mult_l)

print("\n=== E. A NORMAL trailing pool -> label 'normal', multiplier 1.0 "
      "regardless of shadow_mode ===")
root2 = sandbox_cwd()
write_day(root2, "2026-10-01", [0.2, 0.3, -0.05])
write_day(root2, "2026-10-02", [0.15, 0.1])
mult_n, label_n, pool_n = M._compute_conviction_read(on_live, repo_root=root2)
check("label reads 'normal'", label_n == "normal", label_n)
check("multiplier is 1.0 even in live mode - nothing to scale down",
      mult_n == 1.0, mult_n)

print("\n=== F. NO DATA AT ALL -> inert, not a crash, not a false 'weak' ===")
root3 = sandbox_cwd()
mult_e, label_e, pool_e = M._compute_conviction_read(on_live, repo_root=root3)
check("no trailing days exist -> multiplier 1.0", mult_e == 1.0, mult_e)
check("...and label is None, not a manufactured 'weak'", label_e is None, label_e)

print("\n=== G. TODAY ITSELF IS NEVER READ, even if present on disk ===")
root4 = sandbox_cwd()
write_day(root4, "2026-10-01", [0.1, 0.2])
import datetime as _dt
today_str = _dt.datetime.now().strftime("%Y-%m-%d")
write_day(root4, today_str, [-99.0, -99.0])   # would dominate the mean if read
mult_t, label_t, pool_t = M._compute_conviction_read(on_live, repo_root=root4)
check("today's own catastrophic values never entered the pool",
      pool_t is not None and pool_t > -1, pool_t)

print("\n=== H. COMPOSITION INTO _position_size: a THIRD independent "
      "multiplier, same pattern as regime and loss-tier ===")
esrc = open(repo_file("src", "main.py")).read()
check("_position_size multiplies conviction_size_multiplier into regime_mult, "
      "not a separate, parallel ceiling",
      "regime_mult *= getattr(executor, \"conviction_size_multiplier\", 1.0)"
      in esrc)
check("it's applied AFTER loss_tier_multiplier, same composable chain",
      esrc.index("regime_mult *= executor.loss_tier_multiplier()")
      < esrc.index('regime_mult *= getattr(executor, "conviction_size_multiplier"'))

print("\n=== I. run_trading_day RESETS IT EVERY SESSION, same as the regime "
      "multipliers right above it (the exact carry-over bug class this file "
      "already guards against for those two) ===")
check("conviction_size_multiplier is set from _compute_conviction_read every "
      "session, right alongside the regime multiplier resets",
      "executor.regime_size_multiplier = 1.0" in esrc
      and "_compute_conviction_read(config)" in esrc
      and esrc.index("executor.regime_size_multiplier = 1.0")
      < esrc.index("_compute_conviction_read(config)"))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
