"""
2026-09-08: is_trading_day/is_market_open had no way to tell a market
holiday from a normal weekday - both were `now.weekday() < 5` and nothing
else, a limitation the code openly documented in its own docstring rather
than fixing. On a holiday the pre-market pipeline would still screen,
subscribe the stream, and arm the opening burst against a market that was
never going to open.

Fixed by consulting Alpaca's own trading calendar (the authoritative
source, not a hardcoded holiday list that goes stale), cached once per
calendar day since is_trading_day is polled repeatedly through the
pre-market loop, and failing OPEN to the old weekday-only behaviour if the
calendar call itself fails - a wrong "no" here silently cancels a real
trading day with nothing else to catch it.
"""
import sys
import types
from datetime import datetime

import pytz

from _repo import REPO, CONFIG, repo_file
from src.data.market_data import MarketDataManager
from src.broker.alpaca_broker import AlpacaBroker

ET = pytz.timezone("America/New_York")
P = F = 0


def check(n, c, d=""):
    global P, F
    if c: P += 1; print(f"PASS  {n}")
    else: F += 1; print(f"FAIL  {n}   <- {d}")


def at(y, m, d, hh=8, mm=0):
    return ET.localize(datetime(y, m, d, hh, mm))


class Broker:
    """calendar_response is what get_calendar returns; None means raise
    instead, modelling a real API failure."""
    def __init__(self, calendar_response=()):
        self.calendar_response = calendar_response
        self.calls = []

    def get_calendar(self, day):
        self.calls.append(day)
        if self.calendar_response is None:
            raise ConnectionError("simulated Alpaca API failure")
        return list(self.calendar_response)


def mk(broker):
    return MarketDataManager(broker)


print("=== 1. A NORMAL WEEKDAY WITH A CALENDAR ENTRY -> TRADING DAY ===")
b1 = Broker(calendar_response=[types.SimpleNamespace(date="2026-09-08")])
md1 = mk(b1)
tuesday = at(2026, 9, 8)  # a real Tuesday
check("Tuesday with a calendar entry is a trading day",
      md1.is_trading_day(tuesday) is True)
check("the calendar was actually consulted", b1.calls == [tuesday.date()], b1.calls)

print("\n=== 2. A WEEKDAY WITH AN EMPTY CALENDAR -> HOLIDAY, NOT A TRADING DAY ===")
# e.g. Thanksgiving or Labor Day: a real weekday, empty calendar response.
b2 = Broker(calendar_response=[])
md2 = mk(b2)
labor_day = at(2026, 9, 7)  # a Monday
check("weekday but no calendar entry -> NOT a trading day",
      md2.is_trading_day(labor_day) is False)
check("the calendar was consulted (this is the whole point of the fix)",
      b2.calls == [labor_day.date()], b2.calls)

print("\n=== 3. A WEEKEND NEVER EVEN CALLS THE CALENDAR ===")
b3 = Broker(calendar_response=[types.SimpleNamespace(date="x")])
md3 = mk(b3)
saturday = at(2026, 9, 12)
check("Saturday is not a trading day", md3.is_trading_day(saturday) is False)
check("the calendar API was never called - weekday check short-circuits first",
      b3.calls == [], b3.calls)

print("\n=== 4. CACHED PER CALENDAR DAY - NOT ONE API CALL PER POLL ===")
b4 = Broker(calendar_response=[types.SimpleNamespace(date="2026-09-08")])
md4 = mk(b4)
t1 = at(2026, 9, 8, 8, 0)
t2 = at(2026, 9, 8, 8, 30)   # later the same day
check("first call hits the calendar", md4.is_trading_day(t1) is True)
check("second call, same date, is served from cache",
      md4.is_trading_day(t2) is True and len(b4.calls) == 1, b4.calls)

print("\n=== 5. A NEW CALENDAR DAY INVALIDATES THE CACHE ===")
next_day = at(2026, 9, 9, 8, 0)
b4.calendar_response = []  # tomorrow happens to be a holiday in this test
check("a new date triggers a fresh lookup, not the stale cached value",
      md4.is_trading_day(next_day) is False and len(b4.calls) == 2, b4.calls)

print("\n=== 6. CALENDAR API FAILURE -> FAILS OPEN TO WEEKDAY-ONLY ===")
b6 = Broker(calendar_response=None)  # raises
md6 = mk(b6)
wednesday = at(2026, 9, 9)
check("a failed calendar check still treats a weekday as a trading day "
      "(missing a real day is worse than occasionally running on a holiday)",
      md6.is_trading_day(wednesday) is True)

print("\n=== 7. is_market_open() IS NOW HOLIDAY-AWARE TOO ===")
class FrozenMD(MarketDataManager):
    """Freezes datetime.now(self.et) to a fixed instant for is_market_open,
    which (unlike is_trading_day) reads the clock itself rather than taking
    `now` as a parameter."""
    def __init__(self, broker, frozen):
        super().__init__(broker)
        self._frozen = frozen

    def is_trading_day(self, now=None):
        return super().is_trading_day(now or self._frozen)

    def is_market_open(self):
        now = self._frozen
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if not self.is_trading_day(now):
            return False
        return market_open <= now <= market_close

b7 = Broker(calendar_response=[])  # holiday
fmd_holiday = FrozenMD(b7, at(2026, 9, 7, 10, 0))  # 10am on Labor Day
check("10am on a market holiday now reads as CLOSED, not open",
      fmd_holiday.is_market_open() is False)

b8 = Broker(calendar_response=[types.SimpleNamespace(date="2026-09-08")])
fmd_normal = FrozenMD(b8, at(2026, 9, 8, 10, 0))  # 10am on a real trading day
check("10am on a genuine trading day is still open (no regression)",
      fmd_normal.is_market_open() is True)

b9 = Broker(calendar_response=[types.SimpleNamespace(date="2026-09-08")])
fmd_afterhours = FrozenMD(b9, at(2026, 9, 8, 20, 0))  # 8pm on a trading day
check("8pm on a trading day is still closed (hours check still applies)",
      fmd_afterhours.is_market_open() is False)

print("\n=== 8. AlpacaBroker.get_calendar DELEGATES CORRECTLY ===")
class FakeTradingClient:
    def __init__(self):
        self.requests = []

    def get_calendar(self, filters):
        self.requests.append(filters)
        return ["ok"]

broker = AlpacaBroker.__new__(AlpacaBroker)  # bypass __init__ - no real creds needed
broker.trading_client = FakeTradingClient()
result = broker.get_calendar(at(2026, 9, 8).date())
check("returns whatever the trading client returns", result == ["ok"], result)
check("start and end are both the requested single day",
      broker.trading_client.requests[0].start == broker.trading_client.requests[0].end
      == at(2026, 9, 8).date())

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
