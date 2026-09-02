"""
FULL-SESSION SIMULATION: drive run_trading_day through an entire trading day
against a scripted market, with a fake clock, and assert nothing explodes.

Why this exists, on top of the unit and integration suites. Every other test
here calls functions directly. None of them runs THE LOOP - and the loop is
where the new 2026-09-02 code actually lives: regime state threaded across
polls, the path recorder buffering per sweep, the burst passing history it
did not used to receive, exclusions filtering a list built three stages
earlier. A NameError or a mis-scoped variable on any of those paths is
invisible to a unit test and fatal at 09:31.

The 2026-09-01 session is the argument for this file. Every component had
passing tests; the session still produced four phantom positions, because
what broke was the INTERACTION between entry recording and the exit sweep,
inside the loop, under live timing.

The clock is fake and time.sleep is a no-op, so a 6.5-hour session runs in
under a second. Prices are scripted per symbol so each scenario is
deterministic and the assertions are about behaviour, not luck.
"""
import copy
import io
import logging
import os
import sys
import types
from datetime import datetime, timedelta
import pytz
import yaml

from _repo import REPO, CONFIG, repo_file, sandbox_cwd

P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


ET = pytz.timezone("America/New_York")


# --------------------------------------------------------------------------
# fake clock
# --------------------------------------------------------------------------

class FakeClock:
    """Advances on every sleep and by a small tick on every now() read, so a
    session progresses even if the loop never sleeps on some path."""

    def __init__(self, start):
        self.t = start
        self.reads = 0

    def now(self, tz=None):
        self.reads += 1
        # A tiny per-read drift keeps successive timestamps distinct (the
        # path recorder keys on them) without materially moving the clock.
        self.t += timedelta(milliseconds=200)
        return self.t.astimezone(tz) if tz else self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=max(0.0, float(seconds or 0)))


class ClockDatetime:
    """Stands in for `datetime` inside src.main - only .now() is redirected;
    everything else defers to the real class so timedelta arithmetic,
    .replace(), .strftime() and isinstance checks all keep working."""

    def __init__(self, clock):
        self._clock = clock

    def now(self, tz=None):
        return self._clock.now(tz)

    def __getattr__(self, name):
        return getattr(datetime, name)


# --------------------------------------------------------------------------
# scripted market
# --------------------------------------------------------------------------

class Script:
    """Price for a symbol as a function of the session clock."""

    def __init__(self, open_px, moves):
        # moves: [(minutes_after_open, pct_from_open)]
        self.open_px = open_px
        self.moves = sorted(moves)

    def price_at(self, minutes):
        pct = 0.0
        for m, p in self.moves:
            if minutes >= m:
                pct = p
            else:
                break
        return round(self.open_px * (1 + pct / 100.0), 4)


class FakeMarketData:
    def __init__(self, scripts, clock, open_dt, streamed=None, spread=0.02):
        self.scripts = scripts
        self.clock = clock
        self.open_dt = open_dt
        self.streamed = set(streamed if streamed is not None else scripts)
        self.spread = spread
        self.broker = types.SimpleNamespace(
            get_latest_quote=lambda s: {"spread": self.spread})
        self.calls = 0

    def _minutes(self):
        return (self.clock.t - self.open_dt).total_seconds() / 60.0

    def is_streamed(self, s):
        return s in self.streamed

    def get_latest_bar(self, s, tf="1Min"):
        self.calls += 1
        sc = self.scripts.get(s)
        if not sc:
            return None
        px = sc.price_at(self._minutes())
        return {"open": px, "high": px * 1.001, "low": px * 0.999,
                "close": px, "volume": 25000, "timestamp": self.clock.t}

    def get_entry_price(self, s, bar):
        return bar.get("close") if bar else None

    def get_rsi(self, s, period=14):
        return 55.0

    def stats(self):
        return {"stream_hits": 0, "rest_fallbacks": 0, "stream_pct": 0.0,
                "entry_prices_from_ticks": 0, "entry_prices_from_bars": 0}

    def get_last_trade_price(self, s):
        return None


class FakeOrder:
    def __init__(self, oid, symbol, qty, side):
        self.id = oid
        self.symbol = symbol
        self.qty = str(qty)
        self.side = side
        self.filled_qty = str(qty)
        self.status = "filled"


class FakePos:
    def __init__(self, symbol, qty, avg, cur):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = str(avg)
        self.current_price = str(cur)
        self.market_value = str(qty * cur)
        self.unrealized_pl = str(qty * (cur - avg))
        self.unrealized_plpc = "0"


class FakeBroker:
    """Fills market orders immediately unless told otherwise."""

    def __init__(self, market, cash=100000.0, fill=True):
        self.market = market
        self.cash = cash
        self.positions = {}
        self.orders = []
        self.n = 0
        self.fill = fill
        self.cancelled = []

    def _price(self, symbol):
        bar = self.market.get_latest_bar(symbol)
        return bar["close"] if bar else 100.0

    def get_account(self):
        equity = self.cash + sum(
            int(p.qty) * self._price(s) for s, p in self.positions.items())
        return types.SimpleNamespace(cash=str(self.cash), equity=str(equity),
                                     buying_power=str(self.cash * 2),
                                     daytrading_buying_power=str(self.cash * 4))

    def get_positions(self):
        out = {}
        for s, p in self.positions.items():
            px = self._price(s)
            out[s] = FakePos(s, int(p.qty), float(p.avg_entry_price), px)
        return out

    def get_position(self, s):
        return self.get_positions().get(s)

    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        return 0

    def get_filled_sell_orders_since(self, symbol, since):
        return []

    def submit_market_order(self, symbol, qty, side="buy"):
        self.n += 1
        order = FakeOrder(f"o{self.n}", symbol, qty, side)
        self.orders.append((symbol, qty, side))
        if not self.fill:
            return order                      # accepted but never fills
        px = self._price(symbol)
        # Signed arithmetic, so a BUY that covers a short lands at zero and the
        # position disappears - rather than being treated as "adding to a long"
        # and dividing by a zero share count. A fake that cannot model a cover
        # cannot verify the sign-bug fix that exists to handle one.
        existing = self.positions.get(symbol)
        old_q = int(existing.qty) if existing else 0
        delta = qty if side == "buy" else -qty
        new_q = old_q + delta
        self.cash += (-delta) * px

        if new_q == 0:
            self.positions.pop(symbol, None)
        elif old_q == 0 or (old_q > 0) != (new_q > 0):
            # opened, or flipped sign - the new average is simply this fill
            self.positions[symbol] = FakePos(symbol, new_q, px, px)
        elif abs(new_q) > abs(old_q):
            avg = ((abs(old_q) * float(existing.avg_entry_price)) + qty * px) / abs(new_q)
            self.positions[symbol] = FakePos(symbol, new_q, avg, px)
        else:
            # reduced - the average entry is unchanged
            self.positions[symbol] = FakePos(
                symbol, new_q, float(existing.avg_entry_price), px)
        return order


class FakeNotifier:
    def __init__(self):
        self.run_context = {}
        self.sent = []

    def send_daily_summary(self, *a, **k):
        self.sent.append(("daily", k))
        return True

    def send_report(self, *a, **k):
        self.sent.append(("report", k))
        return True


# --------------------------------------------------------------------------
# the harness
# --------------------------------------------------------------------------

def run_session(cfg, scripts, *, start="09:26", end="16:05", streamed=None,
                fill=True, spread=0.02, capture_errors=True):
    """
    Drive a whole session. Returns (result, errors, broker, executor, strategy).

    `errors` collects every ERROR/CRITICAL log line, because the trading loop
    catches almost everything per-symbol - a bug shows up as a logged error
    and a quiet day, not as a traceback, which is exactly how the 2026-09-01
    phantom positions hid.
    """
    import src.main as M
    from src.executor.executor import Executor
    from src.strategy.strategy import Strategy

    today = datetime.now(ET).replace(second=0, microsecond=0)
    h, m = (int(x) for x in start.split(":"))
    clock = FakeClock(today.replace(hour=h, minute=m))
    open_dt = today.replace(hour=9, minute=30)

    market = FakeMarketData(scripts, clock, open_dt, streamed=streamed, spread=spread)
    broker = FakeBroker(market, fill=fill)
    executor = Executor(broker, cfg)
    strategy = Strategy(cfg)
    notifier = FakeNotifier()

    errors = []

    class Collect(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                try:
                    errors.append(record.getMessage())
                except Exception:
                    errors.append("<unformattable>")

    handler = Collect()
    root = logging.getLogger()
    root.addHandler(handler)

    real_dt, real_sleep = M.datetime, M.time.sleep
    end_h, end_m = (int(x) for x in end.split(":"))
    stop_at = today.replace(hour=end_h, minute=end_m)

    def guarded_sleep(seconds):
        clock.sleep(seconds)
        if clock.t > stop_at:
            raise KeyboardInterrupt("simulated session end")

    M.datetime = ClockDatetime(clock)
    M.time.sleep = guarded_sleep

    result = None
    try:
        result = M.run_trading_day(
            cfg, market, strategy, executor,
            list(scripts.keys()), {s: 55.0 for s in scripts},
            notifier, ET,
            signal_journal=M.SignalJournal(cfg),
        )
    except KeyboardInterrupt:
        result = "TIMEBOX"
    except Exception as e:
        result = e
        if not capture_errors:
            raise
    finally:
        M.datetime, M.time.sleep = real_dt, real_sleep
        root.removeHandler(handler)

    return result, errors, broker, executor, strategy


def base_config():
    cfg = yaml.safe_load(open(CONFIG))
    t = cfg["trading"]
    # Keep the live rules, but make the session cheap to simulate.
    t["entry_check_interval_seconds"] = 10
    t["use_websocket_stream"] = False      # no socket in a simulation
    t["use_daily_screener"] = False
    t["log_signals"] = False
    cfg.setdefault("analytics", {})["log_signals"] = False
    return cfg


UNEXPECTED = ("Traceback", "NameError", "AttributeError", "KeyError",
              "TypeError", "UnboundLocalError", "ValueError", "IndexError",
              "ZeroDivisionError", "not defined", "object has no attribute")


def unexpected(errors):
    return [e for e in errors if any(u in e for u in UNEXPECTED)]


# ==========================================================================

sandbox_cwd()   # chdir to a throwaway repo-shaped dir; never touch live logs
if True:
    print("=== 1. A NORMAL BULLISH SESSION RUNS END TO END ===")
    cfg = base_config()
    scripts = {
        # rises through the open, keeps running - should be bought and exit on a tier
        "AAA": Script(100.0, [(0, 0.0), (1, 0.5), (3, 0.9), (10, 1.4), (30, 1.8)]),
        # rises then fades - should be bought and stopped/trailed out
        "BBB": Script(50.0, [(0, 0.0), (1, 0.4), (5, 0.9), (12, 0.1), (20, -0.8)]),
        # never moves - should never be bought
        "CCC": Script(75.0, [(0, 0.0), (30, 0.02)]),
        # the market benchmarks, both rising: a bullish regime
        "SPY": Script(500.0, [(0, 0.0), (5, 0.2), (15, 0.35), (60, 0.5)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.25), (15, 0.4), (60, 0.6)]),
    }
    res, errs, brk, ex, st = run_session(cfg, scripts)
    check("the session completed without an exception escaping the loop",
          not isinstance(res, Exception), res)
    bad = unexpected(errs)
    check("no unexpected error types were logged", not bad, bad[:4])
    check("orders were actually placed", len(brk.orders) > 0, brk.orders[:5])
    check("the mover was bought", any(o[0] == "AAA" for o in brk.orders), brk.orders[:8])
    check("the flat symbol was never bought",
          not any(o[0] == "CCC" for o in brk.orders), brk.orders[:8])
    check("benchmarks were never traded",
          not any(o[0] in ("SPY", "QQQ") for o in brk.orders), brk.orders[:8])
    check("the book is flat by the end of the session", brk.positions == {},
          list(brk.positions))

    print("\n=== 2. BEARISH REGIME: both indices under VWAP -> NO NEW LONGS ===")
    cfg2 = base_config()
    # Indices open, tick up briefly (setting VWAP above the later price), then
    # fall and stay below it for the rest of the session.
    down = [(0, 0.0), (1, 0.3), (8, -0.5), (20, -0.9), (60, -1.2)]
    scripts2 = {
        "AAA": Script(100.0, [(0, 0.0), (1, 0.6), (5, 1.0), (20, 1.4)]),
        "BBB": Script(50.0, [(0, 0.0), (1, 0.5), (5, 0.9), (20, 1.2)]),
        "SPY": Script(500.0, down),
        "QQQ": Script(400.0, down),
    }
    res2, errs2, brk2, ex2, st2 = run_session(cfg2, scripts2)
    check("bearish session completed cleanly", not isinstance(res2, Exception), res2)
    check("no unexpected errors", not unexpected(errs2), unexpected(errs2)[:4])
    entries_after_regime = [o for o in brk2.orders if o[2] == "buy"]
    check("a bearish regime was declared",
          any("REGIME" in e for e in errs2) or ex2.regime_size_multiplier == 0.0,
          ex2.regime_size_multiplier)
    check("the regime multiplier ended at 0x (no new longs)",
          ex2.regime_size_multiplier == 0.0, ex2.regime_size_multiplier)

    print("\n=== 3. THE 2026-09-01 FAILURE: entries that NEVER FILL ===")
    # The exact shape that produced four phantom positions: orders accepted,
    # never filled, exits then firing against stock that was never owned.
    cfg3 = base_config()
    scripts3 = {
        "NOW": Script(100.0, [(0, 0.0), (1, 0.6), (2, -0.4), (5, -1.2)]),
        "PLTR": Script(60.0, [(0, 0.0), (1, 0.5), (2, -0.5), (5, -1.5)]),
        "SPY": Script(500.0, [(0, 0.0), (5, 0.2), (30, 0.3)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.2), (30, 0.3)]),
    }
    res3, errs3, brk3, ex3, st3 = run_session(cfg3, scripts3, fill=False)
    check("a session of never-filling entries still completes",
          not isinstance(res3, Exception), res3)
    check("no unexpected errors", not unexpected(errs3), unexpected(errs3)[:4])
    sells = [o for o in brk3.orders if o[2] == "sell"]
    check("NOT ONE sell was submitted against an unfilled entry - the phantom "
          "guard held for a whole session", sells == [], sells[:6])
    check("...and nothing was left tracked as open",
          st3.get_open_trades() == {}, list(st3.get_open_trades()))

    print("\n=== 4. A BROKER THAT FAILS EVERY ORDER ===")
    cfg4 = base_config()

    class RefusingBroker(FakeBroker):
        def submit_market_order(self, symbol, qty, side="buy"):
            raise RuntimeError("simulated broker refusal")

    import src.main as M4
    from src.executor.executor import Executor as Ex4
    from src.strategy.strategy import Strategy as St4
    scripts4 = {
        "AAA": Script(100.0, [(0, 0.0), (1, 0.6), (10, 1.2)]),
        "SPY": Script(500.0, [(0, 0.0), (5, 0.3)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.3)]),
    }
    today4 = datetime.now(ET).replace(second=0, microsecond=0)
    clock4 = FakeClock(today4.replace(hour=9, minute=26))
    mkt4 = FakeMarketData(scripts4, clock4, today4.replace(hour=9, minute=30))
    brk4 = RefusingBroker(mkt4)
    ex4, st4 = Ex4(cfg4, ) if False else Ex4(brk4, cfg4), St4(cfg4)
    errors4 = []

    class C4(logging.Handler):
        def emit(self, rec):
            if rec.levelno >= logging.ERROR:
                errors4.append(rec.getMessage())
    h4 = C4(); logging.getLogger().addHandler(h4)
    rdt, rsl = M4.datetime, M4.time.sleep
    stop4 = today4.replace(hour=10, minute=30)

    def gs4(s):
        clock4.sleep(s)
        if clock4.t > stop4:
            raise KeyboardInterrupt
    M4.datetime = ClockDatetime(clock4); M4.time.sleep = gs4
    try:
        M4.run_trading_day(cfg4, mkt4, st4, ex4, list(scripts4),
                           {s: 55.0 for s in scripts4}, FakeNotifier(), ET,
                           signal_journal=M4.SignalJournal(cfg4))
        out4 = "completed"
    except KeyboardInterrupt:
        out4 = "TIMEBOX"
    except Exception as e:
        out4 = e
    finally:
        M4.datetime, M4.time.sleep = rdt, rsl
        logging.getLogger().removeHandler(h4)
    check("a broker refusing every order does not crash the session",
          not isinstance(out4, Exception), out4)
    check("no unexpected error types", not unexpected(errors4), unexpected(errors4)[:4])
    check("nothing was committed to strategy state on a refused order",
          st4.get_open_trades() == {}, list(st4.get_open_trades()))

    print("\n=== 5. EVERY SYMBOL IS AN EXCLUDED ETF ===")
    cfg5 = base_config()
    scripts5 = {
        "SOXL": Script(100.0, [(0, 0.0), (1, 0.8), (10, 1.5)]),
        "TQQQ": Script(90.0, [(0, 0.0), (1, 0.7), (10, 1.4)]),
        "SPY": Script(500.0, [(0, 0.0), (5, 0.3)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.3)]),
    }
    res5, errs5, brk5, ex5, st5 = run_session(cfg5, scripts5)
    check("an all-ETF watchlist completes cleanly", not isinstance(res5, Exception), res5)
    check("no unexpected errors", not unexpected(errs5), unexpected(errs5)[:4])
    check("NOT ONE leveraged ETF was bought, however well it moved",
          not any(o[0] in ("SOXL", "TQQQ") and o[2] == "buy" for o in brk5.orders),
          brk5.orders[:6])

    print("\n=== 6. MARKET DATA GOES DARK MID-SESSION ===")
    cfg6 = base_config()

    class FlakyMarket(FakeMarketData):
        def get_latest_bar(self, s, tf="1Min"):
            mins = self._minutes()
            if 12 < mins < 25:          # a blackout during the entry window
                return None
            if 25 <= mins < 30:         # then malformed data
                return {"close": None, "volume": None, "timestamp": self.clock.t}
            return super().get_latest_bar(s, tf)

    today6 = datetime.now(ET).replace(second=0, microsecond=0)
    clock6 = FakeClock(today6.replace(hour=9, minute=26))
    mkt6 = FlakyMarket({
        "AAA": Script(100.0, [(0, 0.0), (1, 0.6), (10, 1.2), (40, 0.4)]),
        "SPY": Script(500.0, [(0, 0.0), (5, 0.3)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.3)]),
    }, clock6, today6.replace(hour=9, minute=30))
    brk6 = FakeBroker(mkt6)
    from src.executor.executor import Executor as Ex6
    from src.strategy.strategy import Strategy as St6
    import src.main as M6
    ex6, st6 = Ex6(brk6, cfg6), St6(cfg6)
    errors6 = []

    class C6(logging.Handler):
        def emit(self, rec):
            if rec.levelno >= logging.ERROR:
                errors6.append(rec.getMessage())
    h6 = C6(); logging.getLogger().addHandler(h6)
    rdt6, rsl6 = M6.datetime, M6.time.sleep
    stop6 = today6.replace(hour=16, minute=5)

    def gs6(s):
        clock6.sleep(s)
        if clock6.t > stop6:
            raise KeyboardInterrupt
    M6.datetime = ClockDatetime(clock6); M6.time.sleep = gs6
    try:
        M6.run_trading_day(cfg6, mkt6, st6, ex6, list(mkt6.scripts),
                           {s: 55.0 for s in mkt6.scripts}, FakeNotifier(), ET,
                           signal_journal=M6.SignalJournal(cfg6))
        out6 = "completed"
    except KeyboardInterrupt:
        out6 = "TIMEBOX"
    except Exception as e:
        out6 = e
    finally:
        M6.datetime, M6.time.sleep = rdt6, rsl6
        logging.getLogger().removeHandler(h6)
    check("a data blackout plus malformed bars does not crash the session",
          not isinstance(out6, Exception), out6)
    check("no unexpected error types", not unexpected(errors6), unexpected(errors6)[:4])
    check("the book is still flat at the end", brk6.positions == {}, list(brk6.positions))

    print("\n=== 7. A SHORT ALREADY IN THE ACCOUNT AT THE OPEN ===")
    # The 2026-08-28 shape: the bot starts with a short it never meant to
    # hold. It must refuse to adopt it, and the 16:00 flatten must COVER it.
    cfg7 = base_config()
    scripts7 = {
        "AAA": Script(100.0, [(0, 0.0), (1, 0.5), (20, 1.0)]),
        "SPY": Script(500.0, [(0, 0.0), (5, 0.3)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.3)]),
    }
    today7 = datetime.now(ET).replace(second=0, microsecond=0)
    clock7 = FakeClock(today7.replace(hour=9, minute=26))
    mkt7 = FakeMarketData(scripts7, clock7, today7.replace(hour=9, minute=30))
    brk7 = FakeBroker(mkt7)
    brk7.positions["CRWD"] = FakePos("CRWD", -39, 212.74, 228.0)
    mkt7.scripts["CRWD"] = Script(228.0, [(0, 0.0), (30, 0.5)])
    from src.executor.executor import Executor as Ex7
    from src.strategy.strategy import Strategy as St7
    import src.main as M7
    ex7, st7 = Ex7(brk7, cfg7), St7(cfg7)
    M7.reconcile_existing_positions(brk7, st7, ex7)
    check("the pre-existing SHORT is refused, not adopted",
          "CRWD" not in st7.get_open_trades(), list(st7.get_open_trades()))
    flat7 = ex7.flatten_all_positions()
    cover = [o for o in brk7.orders if o[0] == "CRWD"]
    check("the flatten COVERS it with a buy, never sells it deeper",
          cover and cover[0][2] == "buy", cover)
    check("...and it is gone from the account afterwards",
          "CRWD" not in brk7.positions, list(brk7.positions))

    print("\n=== 8. THE RECORDERS PRODUCE REPLAYABLE DATA FROM A REAL SESSION ===")
    cfg8 = base_config()
    scripts8 = {
        "AAA": Script(100.0, [(0, 0.0), (1, 0.6), (4, 1.1), (9, 1.6), (25, 0.9)]),
        "BBB": Script(40.0, [(0, 0.0), (1, 0.5), (6, 1.0), (14, 0.2), (25, -0.7)]),
        "SPY": Script(500.0, [(0, 0.0), (5, 0.3), (30, 0.45)]),
        "QQQ": Script(400.0, [(0, 0.0), (5, 0.35), (30, 0.5)]),
    }
    res8, errs8, brk8, ex8, st8 = run_session(cfg8, scripts8)
    check("session with recording completed", not isinstance(res8, Exception), res8)
    ctx_exists = os.path.exists("logs/trade_context.csv")
    path_exists = os.path.exists("logs/trade_paths.csv")
    check("trade_context.csv was written", ctx_exists)
    check("trade_paths.csv was written", path_exists)
    if ctx_exists and path_exists:
        import csv as _csv
        ctx = list(_csv.DictReader(open("logs/trade_context.csv")))
        pth = list(_csv.DictReader(open("logs/trade_paths.csv")))
        check("at least one context row", len(ctx) >= 1, len(ctx))
        check("multiple path samples per trade", len(pth) > len(ctx), (len(pth), len(ctx)))
        ids_pth = {r["trade_id"] for r in pth}
        # A position ADOPTED at startup and closed by the 16:00 flatten never
        # passes through the exit sweep, so it legitimately has no path - it
        # was never a trade this session opened. The invariant that matters is
        # that every trade the bot actually ENTERED has one.
        entered = [r for r in ctx if r.get("entry_method") not in ("RECONCILED", "")]
        orphans = [r["trade_id"] for r in entered if r["trade_id"] not in ids_pth]
        check("every trade the bot ENTERED has a recorded price path",
              not orphans, orphans[:4])
        check("...and adopted positions are the only rows without one",
              all(r.get("entry_method") == "RECONCILED"
                  for r in ctx if r["trade_id"] not in ids_pth),
              [r.get("entry_method") for r in ctx if r["trade_id"] not in ids_pth])
        check("the market context was actually captured, not left blank",
              any(r.get("spy_vs_vwap") not in (None, "") for r in ctx),
              [r.get("spy_vs_vwap") for r in ctx][:3])
        check("gain_pct is populated on the path rows",
              all(r.get("gain_pct") not in (None, "") for r in pth[:20]))

        # And the whole point: replay actually runs on what the session wrote.
        import importlib.util as _il
        _s = _il.spec_from_file_location("rp", repo_file("ops", "replay.py"))
        RP = _il.module_from_spec(_s); _s.loader.exec_module(RP)
        trades, skipped = RP.load_trades("logs/trade_context.csv", "logs/trade_paths.csv")
        check("replay can load a REAL session's output", len(trades) >= 1,
              (len(trades), skipped))
        if trades:
            rows = RP.replay_all(trades, RP.ExitConfig(stop_pct=-0.5, tiers=[(1.0, 1.0)]))
            check("...and score it without error", len(rows) == len(trades))
            check("...producing a finite result per trade",
                  all(isinstance(r["gain_pct"], float) for r in rows))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
