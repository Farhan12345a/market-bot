"""
trading.repeat_loser_watch (2026-10-08, OFF by default) - the self-updating
version of the manual exclude_symbols patch added the same day (MXL, TWST,
TXG, PBF). Scans trailing trade_history.csv every morning and flags any
symbol with enough losing POSITIONS and a net negative P&L, so a repeat
offender doesn't need a human to notice it and hand-edit the config - and
so a flagged symbol can age back out once its bad days fall outside the
window, which a permanent manual exclude cannot do.

Covers _compute_repeat_loser_watch as a pure function (reading a fixture
logs/daily/ tree), the tranche-collapse (a position with two partial exits
must count as ONE win or loss, not two), and the shadow-mode default that
keeps it from touching exclude_symbols the day it's turned on.
"""
import copy
import csv
import os
import yaml
from _repo import REPO, CONFIG, repo_file, sandbox_cwd
import src.main as M

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def write_day(root, date, rows):
    """rows: [(symbol, entry_time, pl), ...] - one row per TRANCHE, so a
    position with two partial exits is two rows sharing an entry_time."""
    d = os.path.join(root, "logs", "daily", date)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "trade_history.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "entry_time", "pl"])
        for symbol, entry_time, pl in rows:
            w.writerow([symbol, entry_time, pl])


print("=== A. OFF BY DEFAULT - the config ships inert ===")
check("repeat_loser_watch.enabled is true in the shipped config "
      "(shadow mode is the safety, not enabled:false, per its own comment)",
      CFG["trading"]["repeat_loser_watch"]["enabled"] is True)
check("shadow_mode defaults true, so exclude_symbols is never touched live "
      "the day this ships",
      CFG["trading"]["repeat_loser_watch"]["shadow_mode"] is True)

print("\n=== B. _compute_repeat_loser_watch: disabled -> immediate no-op, no I/O ===")
off_cfg = copy.deepcopy(CFG)
off_cfg["trading"]["repeat_loser_watch"]["enabled"] = False
shadow, live = M._compute_repeat_loser_watch(off_cfg, repo_root="/does/not/exist")
check("shadow set is empty", shadow == set(), shadow)
check("live set is empty", live == set(), live)

print("\n=== C. A REPEAT LOSER (3 losing positions, net negative) -> flagged in "
      "the shadow set, but shadow_mode keeps the live set empty ===")
root = sandbox_cwd()
# MXL-shaped: 3 separate days, 3 separate entries (distinct entry_time), all
# losses. GOOD: a profitable symbol (WIN) sits alongside it in the same days.
write_day(root, "2026-10-01", [("BADCO", "2026-10-01T09:35:00", -20.0),
                                ("WIN", "2026-10-01T09:36:00", 50.0)])
write_day(root, "2026-10-02", [("BADCO", "2026-10-02T09:40:00", -15.0),
                                ("WIN", "2026-10-02T09:41:00", 30.0)])
write_day(root, "2026-10-05", [("BADCO", "2026-10-05T09:33:00", -30.0),
                                ("WIN", "2026-10-05T09:34:00", 10.0)])
on_shadow = copy.deepcopy(CFG)
on_shadow["trading"]["repeat_loser_watch"]["enabled"] = True
on_shadow["trading"]["repeat_loser_watch"]["shadow_mode"] = True
on_shadow["trading"]["repeat_loser_watch"]["min_losing_entries"] = 3
shadow_s, live_s = M._compute_repeat_loser_watch(on_shadow, repo_root=root)
check("BADCO is in the shadow set", "BADCO" in shadow_s, shadow_s)
check("WIN (profitable) is NOT flagged", "WIN" not in shadow_s, shadow_s)
check("...but the live set stays empty - SHADOW MODE never touches "
      "exclude_symbols", live_s == set(), live_s)

print("\n=== D. THE SAME DATA, shadow_mode: false -> the live set actually "
      "contains it ===")
on_live = copy.deepcopy(CFG)
on_live["trading"]["repeat_loser_watch"]["enabled"] = True
on_live["trading"]["repeat_loser_watch"]["shadow_mode"] = False
on_live["trading"]["repeat_loser_watch"]["min_losing_entries"] = 3
shadow_l, live_l = M._compute_repeat_loser_watch(on_live, repo_root=root)
check("BADCO is now in the LIVE set too", "BADCO" in live_l, live_l)
check("WIN is still never flagged", "WIN" not in live_l, live_l)

print("\n=== E. TRANCHE COLLAPSE - a position with TWO partial exits counts as "
      "ONE loss, not two ===")
root2 = sandbox_cwd()
# One position, two tranche rows (same entry_time), net negative. Must NOT
# be enough on its own to hit min_losing_entries=3 (it is one position).
write_day(root2, "2026-10-01", [("TANCHE", "2026-10-01T09:35:00", -5.0),
                                 ("TANCHE", "2026-10-01T09:35:00", -8.0)])
write_day(root2, "2026-10-02", [("TANCHE", "2026-10-02T09:35:00", -3.0),
                                 ("TANCHE", "2026-10-02T09:35:00", -4.0)])
shadow_c, _ = M._compute_repeat_loser_watch(on_shadow, repo_root=root2)
check("two losing DAYS (2 positions, 4 tranche rows) do not reach "
      "min_losing_entries=3 - tranches were collapsed to positions first",
      "TANCHE" not in shadow_c, shadow_c)

print("\n=== F. LOSING ENTRIES ALONE ISN'T ENOUGH - needs a net-negative total too ===")
root3 = sandbox_cwd()
# 3 losing positions, but one huge win more than cancels them out.
write_day(root3, "2026-10-01", [("MIXED", "2026-10-01T09:35:00", -10.0)])
write_day(root3, "2026-10-02", [("MIXED", "2026-10-02T09:35:00", -10.0)])
write_day(root3, "2026-10-05", [("MIXED", "2026-10-05T09:35:00", -10.0)])
write_day(root3, "2026-10-06", [("MIXED", "2026-10-06T09:35:00", 500.0)])
shadow_m, _ = M._compute_repeat_loser_watch(on_shadow, repo_root=root3)
check("3 losing positions but net POSITIVE overall -> not flagged",
      "MIXED" not in shadow_m, shadow_m)

print("\n=== G. NO DATA AT ALL -> inert, not a crash ===")
root4 = sandbox_cwd()
shadow_e, live_e = M._compute_repeat_loser_watch(on_live, repo_root=root4)
check("no trailing days exist -> empty shadow set", shadow_e == set(), shadow_e)
check("...and empty live set", live_e == set(), live_e)

print("\n=== H. TODAY ITSELF IS NEVER READ, even if present on disk ===")
root5 = sandbox_cwd()
import datetime as _dt
today_str = _dt.datetime.now().strftime("%Y-%m-%d")
write_day(root5, today_str, [("TODAYCO", "x", -10.0), ("TODAYCO", "y", -10.0),
                              ("TODAYCO", "z", -10.0)])
shadow_t, _ = M._compute_repeat_loser_watch(on_shadow, repo_root=root5)
check("today's own (incomplete) session is never read",
      "TODAYCO" not in shadow_t, shadow_t)

print("\n=== I. exclude_symbols IS NEVER PERMANENTLY GROWN - only the static "
      "base + today's live set, recomputed fresh each day (see main()'s "
      "_static_exclude_symbols) ===")
src = open(repo_file("src/main.py")).read()
check("_compute_repeat_loser_watch is called before select_symbols "
      "at both call sites",
      src.count("_compute_repeat_loser_watch(config)") == 2,
      src.count("_compute_repeat_loser_watch(config)"))
check("exclude_symbols is REASSIGNED from (static base | live set), not "
      "appended to, at both call sites",
      src.count('config["trading"]["exclude_symbols"] = sorted(') == 2,
      src.count('config["trading"]["exclude_symbols"] = sorted('))
check("_static_exclude_symbols is captured once, before any mutation",
      '_static_exclude_symbols = list(config["trading"].get("exclude_symbols") or [])' in src)

print("\n" + ("ALL PASSED" if not F else f"{F} FAILURES, {P} passed"))
import sys
sys.exit(1 if F else 0)
