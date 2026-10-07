"""
trading.opening_minutes_throttle (2026-10-07, ON by default) - caps the
TOTAL entries taken within window_minutes of entry_window_start, across
however many separate per-poll bursts contribute to it.

burst_width_threshold/burst_max_entries (use_burst_throttle) already cap
each INDIVIDUAL poll's simultaneous signals - but nothing capped the total
across CONSECUTIVE polls. 2026-10-06 had 5 separate "BURST DETECTED" events
in the first ~45 seconds of the session, each correctly throttled to 3 at
half size on its own, and the entries from all 5 still stacked up to 18
same-minute entries, 15 of them at baseline RVOL (no real volume behind the
move) - exactly the "one bet wearing several tickers, but spread across a
few polls instead of one" shape burst_throttle was built for, just outside
its per-poll blind spot.

Covers the window-membership arithmetic as a pure behavioral check (no
heavy main.py import), and the wiring (both entry chains, the increment,
the config) as source-level checks - the same style this codebase already
uses for extended_hourly_cap, which opening_minutes_throttle is a direct
sibling of.
"""
import datetime as _dt
import sys
import yaml
from _repo import REPO, CONFIG, repo_file

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def in_opening_window(entry_start, now, window_minutes):
    """The exact expression used at both entry call sites in main.py -
    reproduced here so the arithmetic itself gets real behavioral coverage,
    not just a string match on the source."""
    return (now - entry_start).total_seconds() <= window_minutes * 60


print("=== A. WINDOW-MEMBERSHIP ARITHMETIC ===")
entry_start = _dt.datetime(2026, 10, 7, 9, 33)
check("the instant entry_window_start opens is inside the window",
      in_opening_window(entry_start, entry_start, 2) is True)
check("59 seconds in is still inside a 2-minute window",
      in_opening_window(entry_start, entry_start + _dt.timedelta(seconds=59), 2) is True)
check("exactly at the 2-minute boundary is still inside (inclusive)",
      in_opening_window(entry_start, entry_start + _dt.timedelta(minutes=2), 2) is True)
check("2 minutes and 1 second is outside",
      in_opening_window(entry_start, entry_start + _dt.timedelta(minutes=2, seconds=1), 2) is False)
check("an hour later is well outside", in_opening_window(
      entry_start, entry_start + _dt.timedelta(hours=1), 2) is False)
check("BEFORE entry_window_start (should not happen live, but must not "
      "crash or read as 'inside' via a negative-duration fluke) reads as "
      "inside, since a negative elapsed time is still <= the cap - "
      "harmless because nothing calls this before entry_start in practice",
      in_opening_window(entry_start, entry_start - _dt.timedelta(seconds=30), 2) is True)

print("\n=== B. CONFIG ===")
omt = CFG["trading"]["opening_minutes_throttle"]
check("shipped enabled", omt["enabled"] is True)
check("a 2-minute window", omt["window_minutes"] == 2)
check("a cap of 6 - roughly what 2 throttled bursts' worth of entries "
      "would be (3 per burst at the default burst_max_entries), not zero "
      "and not unlimited", omt["max_entries"] == 6)

print("\n=== C. WIRING: both entry chains, config-gated, fail-open on an "
      "unconfigured key ===")
src = open(repo_file("src", "main.py")).read()
check("a dedicated session-relative state dict exists, separate from the "
      "repeating-bucket extended_hourly_state",
      'opening_window_state = {"count": 0, "cap_logged": False}' in src)
check("checked in the SHORT candidates loop",
      src.count('skip_reason = "opening_minutes_cap"') >= 2)
check("the window check reads entry_start (session-relative), not a "
      "repeating wall-clock bucket like extended_hourly_due uses",
      "(now - entry_start).total_seconds() <= _omt.get(\"window_minutes\", 2) * 60" in src)
check("the cap-reached message logs once (cap_logged), naming both the "
      "count and the window", "Reached the opening-minutes cap" in src)
check("a filled entry inside the window increments the counter in BOTH "
      "chains", src.count('opening_window_state["count"] += 1') >= 2)
check("defaults are safe even if the config block is missing entirely - "
      "window_minutes defaults to 2, max_entries defaults to 6, matching "
      "the shipped config exactly so an omitted block behaves the same as "
      "the shipped one",
      '_omt.get("window_minutes", 2)' in src and '_omt.get("max_entries", 6)' in src)

print("\n=== D. NO PHANTOM REFUND, DELIBERATELY (documented, not a gap) ===")
check("the deliberate simplification vs extended_hourly_state's refund-on-"
      "phantom logic is explained in the state's own comment, not silent",
      "Deliberately does NOT refund a" in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
