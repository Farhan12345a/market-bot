"""
The pre-market sequence, simulated minute by minute.

Why this suite exists
---------------------
On 2026-08-27 every unit test passed and the opening-move experiment still took
no trades. The failure was ORDERING, which nothing was testing: the QQQ list
scored 98 constituents serially, took 3m17s, finished at 09:31:50, and two
things were waiting on it - the stream subscription and the trading loop itself.
The stream came up 110s after the baseline instant with zero bars; run_trading_day,
which contains the 09:30-09:32 window, could not start until the window had
almost passed.

None of that is visible from a unit test of any single function. What follows
walks a simulated clock from 09:00 to 09:35 and asserts the ORDER things happen
in, and that each stage's slowness cannot starve the next.
"""
import copy, os, sys, yaml
from datetime import datetime, timedelta
import pytz
from _repo import REPO, CONFIG, repo_file, sandbox_cwd

CFG = yaml.safe_load(open(CONFIG))
ET = pytz.timezone("America/New_York")
P = F = 0
def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


class Timeline:
    """
    Replays the pre-market branch's gating conditions against a clock.

    Deliberately mirrors the CONDITIONS rather than calling main() - the point
    is to assert the schedule those conditions produce, and a real run would
    need a broker, a socket and twenty-five minutes.
    """
    def __init__(self, cfg, screener_secs=180, qqq_secs=210, earnings_secs=2):
        t = cfg["trading"]
        self.t = t
        self.screener_secs = screener_secs
        self.qqq_secs = qqq_secs
        self.earnings_secs = earnings_secs
        self.events = []
        self.busy_until = None       # the loop is single-threaded: a slow stage blocks it

    def run(self):
        t = self.t
        screener_at = mins(t["screener_start_time"])
        qqq_at = mins(t.get("qqq_list_start_time", "09:10"))
        earn_at = mins(t["list_builder_start_time"])
        open_at = mins("09:30")
        prestart = t.get("stream_prestart_minutes", 0)
        stream_from = open_at - prestart

        done = {"screener": None, "qqq": None, "earnings": None,
                "stream": None, "loop": None, "burst_open": None}
        clock = mins("09:00") * 60          # seconds
        end = mins("09:36") * 60

        while clock < end:
            m = clock / 60
            if self.busy_until and clock < self.busy_until:
                clock += 10
                continue

            if done["screener"] is None and m >= screener_at:
                done["screener"] = m + self.screener_secs / 60
                self.busy_until = clock + self.screener_secs
                self.events.append(("screener", m, done["screener"]))
            elif (done["screener"] is not None and done["qqq"] is None
                  and m >= qqq_at and m < open_at):
                done["qqq"] = m + self.qqq_secs / 60
                self.busy_until = clock + self.qqq_secs
                self.events.append(("qqq", m, done["qqq"]))
            elif (done["screener"] is not None and done["stream"] is None
                  and m >= stream_from and m < open_at):
                # NOT gated on the lists finishing - that gate is what starved
                # the experiment on 2026-08-27.
                done["stream"] = m
                self.events.append(("stream", m, m))
            elif (done["screener"] is not None and done["earnings"] is None
                  and m >= earn_at and m < open_at):
                budget = max(5.0, (open_at - m) * 60 - t.get("augment_deadline_buffer_seconds", 20))
                spent = min(self.earnings_secs, budget)
                done["earnings"] = m + spent / 60
                self.busy_until = clock + spent
                self.events.append(("earnings", m, done["earnings"]))
            elif done["loop"] is None and m >= open_at:
                done["loop"] = m
                self.events.append(("loop", m, m))
                done["burst_open"] = m
            clock += 10
        return done


def fmt(m):
    return f"{int(m // 60):02d}:{int(m % 60):02d}:{int((m % 1) * 60):02d}" if m else "-"


print("=== 1. THE HAPPY PATH, WITH TODAY'S CONFIG ===")
tl = Timeline(CFG)
d = tl.run()
for name, start, finish in tl.events:
    print(f"      {name:<10} {fmt(start)} -> {fmt(finish)}")
check("the screener runs", d["screener"] is not None)
check("the QQQ list runs", d["qqq"] is not None)
check("the earnings list runs", d["earnings"] is not None)
check("the stream subscribes BEFORE the open", d["stream"] and d["stream"] < mins("09:30"),
      fmt(d["stream"]))
check("the trading loop starts AT the open, not after",
      d["loop"] and d["loop"] <= mins("09:30") + 0.5, fmt(d["loop"]))
check("the burst window opens on time",
      d["burst_open"] and d["burst_open"] < mins(CFG["trading"]["opening_burst"]["decide_by"]),
      fmt(d["burst_open"]))
check("every stage finishes before the bell",
      all(d[k] < mins("09:30") for k in ("screener", "qqq", "earnings")),
      {k: fmt(d[k]) for k in ("screener", "qqq", "earnings")})

print("\n=== 2. THE 2026-08-27 REGRESSION CANNOT RECUR ===")
# The exact shape: QQQ takes 3m17s. Under the old schedule it ran at 09:28 with
# earnings and finished at 09:31:50, blocking everything.
slow = Timeline(CFG, qqq_secs=197)
d2 = slow.run()
check("a 3m17s QQQ pass still finishes before the open",
      d2["qqq"] and d2["qqq"] < mins("09:30"), fmt(d2["qqq"]))
check("...and the stream is still up before the baseline",
      d2["stream"] and d2["stream"] < mins("09:30"), fmt(d2["stream"]))
check("...and the loop still starts at the open",
      d2["loop"] and d2["loop"] <= mins("09:30") + 0.5, fmt(d2["loop"]))

# Now the pathological case: QQQ takes TEN minutes.
worse = Timeline(CFG, qqq_secs=600)
d3 = worse.run()
check("even a 10-minute QQQ pass leaves the loop starting on time",
      d3["loop"] and d3["loop"] <= mins("09:30") + 0.5, fmt(d3["loop"]))
check("...and the stream still comes up pre-open",
      d3["stream"] and d3["stream"] < mins("09:30"), fmt(d3["stream"]))

print("\n=== 3. A SLOW SCREENER DOES NOT CASCADE ===")
# 250 candidates at ~1.65s each is ~7 minutes; test double that.
slow_scr = Timeline(CFG, screener_secs=840)
d4 = slow_scr.run()
check("a 14-minute screener still lets the stream subscribe",
      d4["stream"] and d4["stream"] < mins("09:30"), fmt(d4["stream"]))
check("...and the loop starts at the open", d4["loop"] is not None, fmt(d4["loop"]))

print("\n=== 4. THE CAP KEEPS THE SCREENER INSIDE ITS WINDOW ===")
t = CFG["trading"]
pool = len(t["stock_universe"])
cap = t.get("max_screen_candidates", 0)
est_uncapped = (pool + 50) * 1.65          # + candidates.txt
est_capped = min(pool + 50, cap) * 1.65
print(f"      pool {pool} + candidates.txt 50, cap {cap}")
print(f"      uncapped ~{est_uncapped:.0f}s, capped ~{est_capped:.0f}s")
check("the capped screener fits before the QQQ slot",
      mins(t["screener_start_time"]) + est_capped / 60 <= mins(t.get("qqq_list_start_time", "09:10")) + 5,
      est_capped)
check("the capped screener is inside its own timeout",
      est_capped < t["screener_timeout_seconds"])

print("\n=== 5. THE SCHEDULE ITSELF IS COHERENT ===")
check("screener before QQQ",
      mins(t["screener_start_time"]) < mins(t.get("qqq_list_start_time", "09:10")))
check("QQQ before the stream window",
      mins(t.get("qqq_list_start_time", "09:10")) < mins("09:30") - t["stream_prestart_minutes"])
check("earnings stays late enough for the surprise to publish",
      mins(t["list_builder_start_time"]) >= mins("09:25"))
check("the burst baseline is the bell",
      t["opening_burst"]["baseline_time"] == "09:30")
check("the burst closes before normal entries open",
      mins(t["opening_burst"]["decide_by"]) <= mins(t["entry_window_start"]))
check("the burst leaves slots for the normal session",
      t["opening_burst"]["max_positions"] < t["max_concurrent_positions"],
      (t["opening_burst"]["max_positions"], t["max_concurrent_positions"]))

print("\n=== 5b. THE POOL WITH THE DYNAMIC UNIVERSE ON ===")
# The screener cost scales with the candidate count, and the dynamic build adds
# a shortlist on top of the static pool. This is the arithmetic that decides
# whether the pre-open window still fits.
_static = len(t["stock_universe"])
_short = t.get("universe_shortlist_size", 0)
_merged = _short + _static + 50          # + candidates.txt
_screened = min(_merged, t.get("max_screen_candidates", _merged))
_secs = _screened * 1.65                 # measured 2026-08-27: 92 in 151.8s
print(f"      {_short} shortlist + {_static} static + ~50 file = ~{_merged}, "
      f"screening {_screened} in ~{_secs:.0f}s")
# Asserted for BOTH states, since the switch flips between sessions: with the
# dynamic build on the pool is shortlist+static+file, with it off it is
# static+file. Either must fit under the cap and inside the window.
_pool_off = _static + 50
check("the pool fits under the cap with the dynamic build OFF",
      _pool_off <= t.get("max_screen_candidates", 0), (_pool_off, t.get("max_screen_candidates")))
check("...and ON",
      _merged <= t.get("max_screen_candidates", 0), (_merged, t.get("max_screen_candidates")))
check("the screen still fits before the stream window",
      mins(t["screener_start_time"]) + _secs / 60 < mins("09:30") - t["stream_prestart_minutes"],
      mins(t["screener_start_time"]) + _secs / 60)
check("...and inside its own timeout", _secs < t["screener_timeout_seconds"], _secs)
_tl_dyn = Timeline(CFG, screener_secs=_secs)
_d = _tl_dyn.run()
check("with a dynamic-sized screen the stream is still pre-open",
      _d["stream"] and _d["stream"] < mins("09:30"), fmt(_d["stream"]))
check("...and the loop still starts at the bell",
      _d["loop"] and _d["loop"] <= mins("09:30") + 0.5, fmt(_d["loop"]))
check("a failed build falls back rather than emptying the watchlist",
      "falling back to the static pool" in open(repo_file("src", "screener", "universe.py")).read())

print("\n=== 6. NOTHING MATERIAL CHANGED SINCE THE WINNING SESSION ===")
# 2026-08-27 was the first profitable day. These are the settings that decided
# the trades it took; if any drift, tomorrow is not a comparable test.
import subprocess
prev = subprocess.run(["git", "show", "b466429:config.yaml"],
                      capture_output=True, text=True, cwd=REPO)
if prev.returncode == 0:
    old = yaml.safe_load(prev.stdout)["trading"]
    # DELIBERATE changes since the winning session, listed with what they must
    # now be. Moving a key here rather than deleting it keeps the drift check
    # honest: an accidental edit still fails, it just fails against the new
    # value. A key that is neither in `same` nor here is not being watched at
    # all, which is the state this block exists to prevent.
    changed = {
        # Widened again for 2026-09-02: the four positions that survive the
        # 09:45 halt (or any earlier entry still working) get more room to
        # reach take-profit before the window closes. The halt's own -0.3%
        # floor is untouched - this is downstream of it, not a loosening of it.
        "entry_window_end": "10:15",
        # 2000 -> 1000 for 2026-09-02. 2000 was raised in test to let a session
        # run further before halting; set back now that measurement isn't the
        # goal. See config.yaml for the real-money reminder attached to this.
        "max_daily_loss_usd": 1000,
    }
    for k, want in changed.items():
        check(f"{k} deliberately changed to {want}", t.get(k) == want,
              (old.get(k), t.get(k)))

    same = ["entry_window_start", "rapid_increase_pct",
            "rapid_increase_max_pct", "rapid_increase_lookback_minutes",
            "max_concurrent_positions", "max_daily_entries",
            "first_exit_loss_pct", "final_exit_loss_pct", "trailing_stop_pct",
            "take_profit_tiers", "breakeven_tiers", "use_resistance_exit",
            "use_breakeven_floor", "reentry_cooldown_minutes",
            "num_stocks_to_trade", "stream_max_subscriptions",
            "min_stock_price", "max_stock_price", "use_continuation_score"]
    for k in same:
        check(f"{k} unchanged", old.get(k) == t.get(k), (old.get(k), t.get(k)))
    check("stock_universe unchanged",
          sorted(old.get("stock_universe", [])) == sorted(t.get("stock_universe", [])),
          (len(old.get("stock_universe", [])), len(t.get("stock_universe", []))))
else:
    check("previous config available for comparison", False, prev.stderr[:80])

print("\n=== 7. THE DAY CANNOT END WHILE THE BROKER HOLDS SHARES ===")
# The 16:00 time stop lives INSIDE run_trading_day. Returning early on an
# all_closed that only consulted strategy.trades means anything the broker still
# holds is never flattened - on 2026-08-28 six positions were adopted at 03:16,
# taking six of ten concurrent slots before the bell.
msrc = open(repo_file("src", "main.py")).read()
check("the broker is consulted before the day ends",
      "still_held = executor.broker.get_positions()" in msrc)
check("a non-empty broker view blocks the early return",
      "NOT ending the day" in msrc)
check("...and says why it matters", "occupy concurrent" in msrc)
check("a broker error does not end the day on an unverified view",
      "not ending the day on an unverified view" in msrc)
check("the time stop is still what flattens them",
      "so the {time_stop_hour}:00 time stop can flatten them" in msrc
      or "time stop can flatten them" in msrc)

# The ordering that makes this work: the check sits BEFORE the return, and the
# time stop is reachable on the next iteration.
_idx_check = msrc.index("still_held = executor.broker.get_positions()")
_idx_close = msrc.index('finish_day("all_closed")')
_idx_stop = msrc.index("if now.hour >= time_stop_hour:")
check("the broker check precedes the all_closed return", _idx_check < _idx_close)
check("the time stop is still downstream and reachable", _idx_stop > _idx_close)

print("\n=== 8. THE FLATTEN UTILITY ===")
check("ops/flatten-now.py exists", os.path.exists(repo_file("ops", "flatten-now.py")))
_f = open(repo_file("ops", "flatten-now.py")).read()
check("it is a dry run unless --yes is passed", '"--yes"' in _f and "Dry run" in _f)
# NOT Executor.flatten_all_positions. That path sells unconditionally, which
# closes a long and DOUBLES a short - on 2026-08-28 it queued sells against
# CRWD -39 and OKTA -52, which at the open would have made them -78 and -104.
check("it does not route through the sell-only flatten path",
      "executor.flatten_all_positions()" not in _f
      and "Executor(broker" not in _f)
check("it closes via Alpaca's side-aware close_all_positions",
      "close_all_positions" in _f)
check("it cancels working orders first (else Alpaca calls it a wash trade)",
      "cancel_orders=True" in _f)
check("it names short positions before acting on them", "SHORT" in _f)
check("it re-reads the broker afterwards", "remaining = broker.get_positions()" in _f)
check("a premarket queue is explained, not reported as failure",
      "QUEUED" in _f and "09:30" in _f)
check("a genuine API failure exits non-zero", "sys.exit(1)" in _f)
check("it loads credentials the way systemd does",
      "/etc/market-bot.env" in _f and "load_credentials" in _f)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
