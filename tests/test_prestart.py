"""
Why the stream did not come up on 2026-08-31, and why it cannot happen that way
again.

The timeline, from logs/trading.log:

    09:05:14  screener starts
    09:05:47  dynamic universe done - 33s, not the culprit
    09:10:22  QQQ list starts
    09:28:06  earnings list starts        <- QQQ ran 17m44s
              (no PRE-OPEN subscribe line at all)
    09:30:09  stream connected, ZERO bars
    09:30:39  stream gives up; session runs on REST ~15 min delayed
    09:33:14  opening burst: 0 of 25 measured

Three separate defects, each of which alone would have been survivable:

  1. The QQQ build had NO deadline. The earnings build has had one since
     2026-08-27, for exactly this reason; the QQQ branch was never given the
     same treatment and ran 5x longer than the run that prompted it.
  2. The pre-open subscribe was checked ONCE per loop iteration. At 09:10:22 it
     was too early - the window opens at 09:26 - and the QQQ build then blocked
     the same iteration until 09:28. The window opened and closed inside a call.
  3. The no-bars watchdog counted from SUBSCRIBE, so its 120s budget was spent
     pre-market, where IEX silence is normal rather than diagnostic. It killed a
     working socket 39 seconds after the bell.

This suite is about ORDERING and BUDGETS, which no unit test of any single
function can see.
"""
import re, time, yaml
from _repo import REPO, CONFIG, repo_file

CFG = yaml.safe_load(open(CONFIG))
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


MAIN = open(repo_file("src", "main.py")).read()
STREAM = open(repo_file("src", "data", "stream.py")).read()


print("=== 1. THE QQQ BUILD IS BUDGETED ===")
check("it runs on a worker with a timeout, not inline",
      "_qqq_future = _qqq_pool.submit(" in MAIN)
check("...and the timeout is applied", "_qqq_future.result(timeout=_qqq_deadline)" in MAIN)
check("...and a timeout keeps the screener's picks rather than dying",
      "abandoning it" in MAIN and "keeping the screener's" in MAIN)
check("the budget protects the STREAM window, not merely the open",
      "_stream_needs = market_open_today - timedelta(minutes=prestart or 0)" in MAIN)
check("...and it can never go negative", "_qqq_deadline = max(" in MAIN)

# The arithmetic that matters: with today's config, the QQQ build must be
# forced to yield with the stream's window still intact.
t = CFG["trading"]
qqq_at = t.get("qqq_list_start_time", "09:10")
prestart = t.get("stream_prestart_minutes", 0)
buf = t.get("augment_deadline_buffer_seconds", 20)
mins = lambda s: int(s[:2]) * 60 + int(s[3:])
budget = (mins("09:30") - prestart - mins(qqq_at)) * 60 - buf
print(f"      QQQ starts {qqq_at}, stream needs 09:{30 - prestart:02d}, budget {budget:.0f}s")
check("the QQQ build gets a positive budget", budget > 0, budget)
check("...and it expires BEFORE the subscribe window opens",
      mins(qqq_at) + budget / 60 <= mins("09:30") - prestart, budget)
# 2026-08-31 took 1064s. The budget must be smaller than that, or nothing changed.
check("a 17m44s run would have been cut off", budget < 1064, budget)


print("\n=== 2. THE SUBSCRIBE IS RETRIED AFTER EVERY BLOCKING STAGE ===")
check("the subscribe is a callable, not an inline branch",
      "def _try_prestart_stream():" in MAIN)
calls = len(re.findall(r"_try_prestart_stream\(\)", MAIN))
check("it is called more than once", calls >= 3, calls)
# Specifically: after the two stages that can block for minutes.
qqq_i = MAIN.index("pending_qqq_done = True")
# rindex, not index: `pending_augmented = True` also appears where the flag is
# initialised, and matching that would test nothing.
earn_i = MAIN.rindex("pending_augmented = True")
check("...including right after the QQQ build",
      "_try_prestart_stream()" in MAIN[qqq_i:qqq_i + 400])
check("...and right after the earnings build",
      "_try_prestart_stream()" in MAIN[earn_i:earn_i + 400])
# It must read the clock itself. Reusing the iteration's stale `now` would put
# it right back to deciding on a timestamp from before the blocking call.
check("it re-reads the clock rather than trusting the iteration's",
      "_now = datetime.now(et)" in MAIN)
check("...and compares the window against that fresh clock",
      "<= _now < market_open_today" in MAIN)
check("it does nothing if the stream is already up",
      "not price_stream.is_running()" in MAIN)

# The regression this nearly shipped with: if the ONLY calls are inside the QQQ
# and earnings branches, then on a morning where both lists are already done
# neither branch is entered and the stream never subscribes at all. There must
# be a call that does not depend on a list build being in flight.
_defn = MAIN.index("def _try_prestart_stream():")
_qqq_branch = MAIN.index("# --- QQQ list, early slot ---")
_between = MAIN[_defn:_qqq_branch]
check("there is an unconditional call every iteration",
      "\n            _try_prestart_stream()" in _between, _between[-200:])
check("...and it is at loop indentation, not inside a branch",
      any(l == "            _try_prestart_stream()" for l in _between.splitlines()))


print("\n=== 3. THE WATCHDOG COUNTS MARKET TIME ===")
check("the no-bars clock starts at the later of subscribe and the open",
      "since = max(self._started_at, self._market_open_monotonic())" in STREAM)
check("...and the comparison uses it", "time.monotonic() - since < NO_DATA_GIVE_UP_SECONDS" in STREAM)
check("the helper exists", "def _market_open_monotonic(self):" in STREAM)
check("...and is inert once the session is under way",
      'return float("-inf")' in STREAM)
check("...and a clock failure does not disable the watchdog",
      STREAM.count('return float("-inf")') >= 2)

# Drive it. Pre-market, the deadline must sit AFTER the open; intraday it must
# fall through to the subscribe time.
import importlib, sys
sys.path.insert(0, REPO)
from src.data import stream as S
import pytz
from datetime import datetime
et = pytz.timezone("America/New_York")
now_et = datetime.now(et)
ps = S.PriceStream.__new__(S.PriceStream)
got = S.PriceStream._market_open_monotonic(ps)
if now_et.hour * 60 + now_et.minute >= 9 * 60 + 30:
    check("after the open it falls through to subscribe time", got == float("-inf"), got)
else:
    check("before the open it defers to the bell", got > time.monotonic(), got)
check("the give-up budget is unchanged at 120s", S.NO_DATA_GIVE_UP_SECONDS == 120,
      S.NO_DATA_GIVE_UP_SECONDS)


print("\n=== 4. ONE RETRY AT THE BELL ===")
check("the stream can clear a give-up", "def clear_give_up(self):" in STREAM)
check("...only when there was one to clear",
      "if not self._gave_up:\n            return False" in STREAM)
check("...and it resets the watchdog clock too",
      "self._started_at = time.monotonic()" in STREAM.split("def clear_give_up")[1][:1200])
check("main retries once at the open", "price_stream.clear_give_up()" in MAIN)
check("...and says so rather than retrying silently",
      "retrying once now" in MAIN)
# The retry must come BEFORE the normal at-open start, or it does nothing.
check("the clear precedes the at-open start",
      MAIN.index("price_stream.clear_give_up()")
      < MAIN.index("if price_stream is not None and not price_stream.is_running():"))


print("\n=== 5. THE ORDER, END TO END ===")
# Every pre-market stage must finish, or be cut off, with the stream's window
# still ahead of it.
screener_at = mins(t["screener_start_time"])
check("screener starts before the QQQ slot", screener_at < mins(qqq_at))
check("QQQ slot is before the subscribe window", mins(qqq_at) < mins("09:30") - prestart)
check("earnings slot is inside the subscribe window or later, so the stream wins the race",
      mins(t["list_builder_start_time"]) >= mins("09:30") - prestart,
      (t["list_builder_start_time"], prestart))
check("the burst still decides before normal entries",
      t["opening_burst"]["decide_by"] <= t["entry_window_start"])

print("\n=== 6. THE DYNAMIC UNIVERSE ACTUALLY WIDENS THE POOL ===")
# 2026-08-31: 10,999 symbols scored -> 64 kept, against universe_size 1000. The
# floor was doing the selecting instead of the top_n sort, because free-tier
# bars are IEX-only (~2% of consolidated volume), so a $50M floor on IEX volume
# asks for ~$2.5B of real volume. A pool that shrinks to the static list is not
# a dynamic universe.
from src.screener.universe import liquidity_cut
_u = CFG["trading"]
check("the liquidity floor is not a mega-cap filter",
      _u["universe_min_dollar_volume"] <= 5_000_000, _u["universe_min_dollar_volume"])
check("universe_size is what does the selecting", _u["universe_size"] >= 500, _u["universe_size"])
check("...and the shortlist is smaller than the universe",
      _u["universe_shortlist_size"] < _u["universe_size"])

# A synthetic tape at realistic IEX scale.
# $3M to $46M of IEX volume, i.e. roughly $150M-$2.3B consolidated. This is
# where the tradeable middle of the market actually sits, and the old floor
# excluded all of it.
_stats = {f"S{i:03d}": {"price": 50.0, "dollar_volume": 3_000_000 + i * 143_000}
          for i in range(300)}
check("a realistic IEX-scale tape survives the floor",
      len(liquidity_cut(_stats, CFG)) >= 200, len(liquidity_cut(_stats, CFG)))
_old = {"trading": dict(_u, universe_min_dollar_volume=50_000_000)}
check("...and the old floor would have kept none of it",
      len(liquidity_cut(_stats, _old)) == 0, len(liquidity_cut(_stats, _old)))

# The price band must still bite - an unbuyable name must never take a slot,
# a stream subscription or a poll.
_band = {"PENNY": {"price": 2.0, "dollar_volume": 900_000_000},
         "RICH":  {"price": 900.0, "dollar_volume": 900_000_000},
         "GOOD":  {"price": 50.0, "dollar_volume": 900_000_000}}
check("the price band still excludes unbuyable names",
      liquidity_cut(_band, CFG) == ["GOOD"], liquidity_cut(_band, CFG))
# And a genuinely illiquid name must still be refused.
_thin = {"THIN": {"price": 50.0, "dollar_volume": 100_000}}
check("a truly illiquid name is still refused", liquidity_cut(_thin, CFG) == [])


print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
