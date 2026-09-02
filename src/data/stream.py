"""
Real-time 1-minute bars over Alpaca's WebSocket feed.

Why this exists
---------------
The bot previously read prices exclusively through REST
(get_latest_bars -> get_historical_bars). On Alpaca's free tier the REST
historical endpoint is delayed ~15 minutes, and that delay is a property of
the REST endpoint, not of the account: the WebSocket stream delivers the same
IEX data live, on the same free plan. Every price the strategy acted on could
therefore be minutes stale - which is what forced
rapid_increase_lookback_minutes up from 2 to 5 (a window shorter than the feed
lag can never accumulate two in-window samples).

There was a second, quieter cost. get_latest_bar() was called once per symbol
per poll, so a 57-symbol watchlist meant 57 REST round-trips every cycle. That
is why walking the symbol list took 5-10 seconds of wall-clock on 2026-08-19,
during which prices moved and the account state the entry checks were reading
went stale.

Design
------
This deliberately does NOT restructure the bot around async event handlers.
The strategy, the entry/exit ordering and the position caps are the parts that
have been debugged the hardest, and rewriting the loop that drives them to be
event-driven would put all of that back in play at once. Instead the stream
runs in a background thread and maintains a dict of the newest bar per symbol;
the existing synchronous poll loop keeps its exact shape and simply reads from
memory instead of issuing a REST call.

The practical effect is the same as an event-driven rewrite: because reads are
now free (no API round-trip, no rate-limit budget), entry_check_interval_seconds
can be lowered well below 60s, so the loop reacts nearly as fast as an event
handler would, without moving the trading logic onto a new execution model.

Failure behavior is fail-soft, never fail-open: if the stream is down, has not
yet received a bar for a symbol, or is serving data that has gone stale, the
caller is told there is no streamed bar and falls back to REST. A dead stream
degrades the bot to exactly its previous behavior rather than trading on
frozen prices.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# A streamed bar older than this is treated as absent, so the caller falls back
# to REST rather than acting on a frozen price. Bars arrive once a minute, so
# this tolerates a couple of missed bars before giving up on the stream. A
# thinly traded symbol legitimately produces no bars in a quiet minute, which
# is exactly the case where falling back to REST is the right answer.
BAR_STALE_AFTER_SECONDS = 180

# A streamed trade older than this is treated as absent, so entry detection
# falls back to the bar close. Much tighter than the bar window because the
# entire point of consuming trades is freshness - a 30-second-old tick is no
# better than the bar it would be replacing.
TRADE_STALE_AFTER_SECONDS = 30

# How long to wait between reconnect attempts after the stream drops.
RECONNECT_DELAY_SECONDS = 5

# If the stream has been running this long during market hours without a
# single bar, stop trying for the rest of the session.
#
# This exists because alpaca-py reconnects INTERNALLY inside run(): when the
# endpoint refuses the handshake (HTTP 403 from a network it won't serve, or
# a second data connection on an account limited to one), run() never returns,
# so the supervisor's own backoff below never gets a chance to engage and
# alpaca-py retries in a tight loop. Left alone that is thousands of rejected
# handshakes over a session - the kind of traffic that earns an IP or account
# a throttle, and a throttle could reach the REST calls the bot actually
# depends on. Better to give up cleanly, log it loudly and run on REST.
#
# Two minutes with zero bars across a full watchlist during market hours is
# unambiguous: even one liquid symbol prints most minutes.
NO_DATA_GIVE_UP_SECONDS = 120

# Alpaca's free/IEX feed caps how many symbols one connection may subscribe to.
# Exceeding it does NOT fail the connect - the socket opens, reports "connected",
# and only then returns `error: symbol limit exceeded (405)`, after which not a
# single bar is ever delivered. That is what happened on 2026-08-21: 59 symbols
# subscribed, connection reported healthy, zero bars, and the watchdog fell back
# to REST two minutes later. Capping up front turns a silent total failure into
# a partial success - the most important symbols stream, the rest use REST,
# which is exactly what the fallback was built to do per-symbol anyway.
DEFAULT_MAX_SUBSCRIPTIONS = 30

# alpaca-py reports fatal subscription problems by LOGGING them on its own
# logger and then sitting there - it does not raise, and run() does not return.
# On 2026-08-21 `error: symbol limit exceeded (405)` was printed 258ms after
# connect, and this class could not see it: the watchdog spent its full 120s
# inferring the failure from silence, then blamed the wrong cause ("likely an
# Alpaca connection limit") in the log. Watching their logger turns a silent
# two-minute timeout into an immediate, correctly-named failure.
ALPACA_WS_LOGGER = "alpaca.data.live.websocket"

# Alpaca allows ONE data websocket per account. Two sources of a second one:
#   1. This process opening a second PriceStream without closing the first -
#      prevented outright by _ACTIVE_STREAM below.
#   2. A PREVIOUS process whose socket the server has not reaped yet. A
#      systemd restart is the common case: the new process can be connecting
#      while the old connection is still registered. That resolves itself in
#      seconds, so "connection limit exceeded" is treated as RETRYABLE rather
#      than fatal - giving up on it would throw away the whole session's live
#      data over a few seconds of overlap.
CONNECTION_LIMIT_RETRIES = 4
CONNECTION_LIMIT_RETRY_DELAY = 15

# `symbol limit exceeded` backoff. The unique-symbol cap (see
# PriceStream.symbol_budget) is an empirical claim, so a wrong guess must
# cost a reconnect rather than the session's stream. Multiplicative because
# the true limit is an unknown round number and probing it one symbol at a
# time would spend the opening window doing it.
SYMBOL_LIMIT_RETRIES = 3
SYMBOL_LIMIT_BACKOFF = 0.6
# Deliberately NOT CONNECTION_LIMIT_RETRY_DELAY (15s). That delay exists to let
# the server release another process's socket; a symbol-count rejection has
# nothing to release, it just resubscribes smaller. At 15s a full backoff would
# burn 45s of the 180-second opening-burst window waiting for nothing.
SYMBOL_LIMIT_RETRY_DELAY = 1

# The single live stream in this process, if any.
_ACTIVE_STREAM = None
_ACTIVE_LOCK = threading.Lock()

# Retried, not given up on - see CONNECTION_LIMIT_RETRIES.
RETRYABLE_STREAM_ERRORS = ("connection limit exceeded",)

FATAL_STREAM_ERRORS = {
    "symbol limit exceeded": (
        "too many UNIQUE SYMBOLS subscribed for this feed - lower "
        "stream_max_subscriptions. Channels (bars/trades) are free; only the "
        "symbol count is capped. Recoverable: _reduce_and_retry steps the "
        "count down and reconnects before this is treated as fatal"
    ),

    "auth failed": "the API key/secret were rejected by the stream endpoint",
    "not authenticated": "the stream endpoint rejected authentication",
    "insufficient subscription": (
        "this feed is not included in the account's data plan"
    ),
}


class _StreamErrorWatcher(logging.Handler):
    """Records fatal errors alpaca-py only ever logs."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.hit = None          # fatal: give up on the stream
        self.retryable = None    # transient: close, wait, try again

    def emit(self, record):
        try:
            text = str(record.getMessage()).lower()
            for needle in RETRYABLE_STREAM_ERRORS:
                if needle in text:
                    self.retryable = record.getMessage()
                    return
            for needle, explanation in FATAL_STREAM_ERRORS.items():
                if needle in text:
                    self.hit = (record.getMessage(), explanation)
                    return
        except Exception:
            pass

    def clear_retryable(self):
        self.retryable = None

    def clear(self):
        """Forget a recorded fatal error, so a retry that CHANGES the
        conditions (fewer symbols - see _reduce_and_retry) is judged on its
        own attempt rather than immediately re-reading the last one's."""
        self.hit = None
        self.retryable = None


class PriceStream:
    """
    Background WebSocket consumer maintaining the latest 1-minute bar per
    symbol. Thread-safe: the websocket thread writes, the trading loop reads.
    """

    def __init__(self, api_key, api_secret, feed="iex", subscribe_trades=False,
                 max_subscriptions=DEFAULT_MAX_SUBSCRIPTIONS):
        self._api_key = api_key
        self._api_secret = api_secret
        self._feed = feed
        self._subscribe_trades = subscribe_trades
        self._max_subscriptions = max_subscriptions
        self._dropped_symbols = []

        self._bars = {}  # symbol -> {open, high, low, close, volume, timestamp}
        self._received_at = {}  # symbol -> time.monotonic() when the bar landed

        # Last trade price per symbol, consumed ONLY by entry detection.
        # Deliberately never reaches the exit path - see get_last_trade_price.
        self._last_trade = {}  # symbol -> price
        self._trade_received_at = {}  # symbol -> time.monotonic() when the tick landed
        self._trades_received = 0
        self._lock = threading.Lock()

        self._stream = None
        self._thread = None
        self._symbols = []
        self._stop_requested = threading.Event()
        self._connected = False
        self._bars_received = 0
        self._started_at = None
        self._gave_up = False
        self._watchdog = None
        self._error_watcher = None
        self._connection_attempts = 0

    # ---- lifecycle -------------------------------------------------------

    def symbol_budget(self):
        """
        How many symbols this connection may carry.

        COUNTING MODEL CHANGED 2026-09-02: Alpaca caps UNIQUE SYMBOLS, not
        channel-subscriptions, so trade ticks are free rather than halving
        reach.

        The evidence is the 2026-09-01 subscribe acknowledgement, which the
        old model cannot explain:

            subscribed to trades: [14 symbols], bars: [14],
                       corrections: [14], cancelErrors: [14]

        That is FOUR channels x 14 symbols = 56 channel-subscriptions,
        accepted with no error, against a configured cap of 28. Under the old
        "bars and trades each count as one" reading it should have been
        rejected at 28. It wasn't. And 2026-08-21's `symbol limit exceeded
        (405)` came at 59 SYMBOLS. Both readings fit one rule: ~30 unique
        symbols on the free IEX feed, whatever you subscribe them to.

        The old model therefore spent half the budget on nothing: 14 symbols
        streamed where 28-30 were available, which is precisely the gap that
        left the 2026-09-01 opening burst choosing from a field of 2.

        Still empirically unconfirmed at 30 - if the cap is lower, the socket
        returns `symbol limit exceeded` in milliseconds and _reduce_and_retry
        below steps the count down and reconnects rather than abandoning the
        session to REST.
        """
        return max(1, self._max_subscriptions)

    def is_running(self):
        """
        True if the socket thread is alive.

        Distinct from is_healthy(), which asks whether the feed is DELIVERING.
        This asks only whether start() has already been called and taken - which
        is what a caller needs before deciding to start it again. The stream is
        now started pre-market and would otherwise be started a second time at
        the open; start() already refuses that, but refusing with a warning
        every session reads like a fault when it is the normal path.
        """
        return bool(self._thread and self._thread.is_alive())

    def start(self, symbols, priority=()):
        """
        Connect and subscribe to 1-minute bars for `symbols`.

        Only the first symbol_budget() symbols are subscribed. `priority` names
        the ones that must make the cut - the screener's picks and the day's
        earnings adds - because those are where a signal is actually likely to
        fire. Everything dropped still gets prices, just over REST.
        """
        if self._thread and self._thread.is_alive():
            logger.warning("PriceStream.start() called while already running - ignoring")
            return

        requested = list(dict.fromkeys(symbols))
        budget = self.symbol_budget()

        if len(requested) > budget:
            ranked = ([s for s in priority if s in requested]
                      + [s for s in requested if s not in set(priority)])
            kept, dropped = ranked[:budget], ranked[budget:]
            self._dropped_symbols = dropped
            logger.warning(
                f"PriceStream: {len(requested)} symbols requested but the "
                f"{self._feed} feed allows {budget} UNIQUE SYMBOLS "
                f"({'bars+trades' if self._subscribe_trades else 'bars only'} - "
                f"channels are free, only the symbol count is capped). "
                f"Streaming the top {len(kept)}; the other {len(dropped)} use REST."
            )
            logger.info(f"PriceStream streaming: {', '.join(kept)}")
            logger.info(f"PriceStream on REST: {', '.join(dropped)}")
            requested = kept

        # Only one data websocket per account, so only one per process. If an
        # earlier PriceStream is still alive - a mid-session re-screen, a retry
        # path, a future caller that forgets - close it before opening another,
        # rather than racing it for the account's single slot.
        global _ACTIVE_STREAM
        with _ACTIVE_LOCK:
            previous = _ACTIVE_STREAM
            _ACTIVE_STREAM = self

        # stop() OUTSIDE the lock. It takes _ACTIVE_LOCK itself, and a plain
        # Lock is not reentrant, so calling it from inside the critical section
        # deadlocks the caller - which here is the main trading thread at
        # session start. Claiming the slot first also means the stop() below
        # cannot clear the entry we just made: it only clears the slot when it
        # still points at the stream being stopped.
        if previous is not None and previous is not self:
            logger.warning(
                "PriceStream: another stream is still active - closing it "
                "first (Alpaca permits one data websocket per account)"
            )
            try:
                previous.stop()
            except Exception as e:
                logger.debug(f"Could not stop the previous stream: {e}")

        self._symbols = requested
        self._connection_attempts = 0
        # Clear last session's cache. Bars and ticks are keyed by symbol with no
        # session marker, so a long-lived process starting a second day would
        # otherwise inherit yesterday's closing prices. They are old enough that
        # the staleness checks would reject them anyway - but only by accident,
        # and "correct because a different check happens to catch it" is not a
        # property worth relying on for prices the bot trades against.
        with self._lock:
            self._bars.clear()
            self._received_at.clear()
            self._last_trade.clear()
            self._trade_received_at.clear()
        self._error_watcher = _StreamErrorWatcher()
        logging.getLogger(ALPACA_WS_LOGGER).addHandler(self._error_watcher)
        self._stop_requested.clear()
        self._gave_up = False
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_forever, name="price-stream", daemon=True
        )
        self._thread.start()
        self._watchdog = threading.Thread(
            target=self._watch_for_silence, name="price-stream-watchdog", daemon=True
        )
        self._watchdog.start()
        logger.info(
            f"PriceStream starting for {len(self._symbols)} symbols on the {self._feed} feed"
        )

    def _reduce_and_retry(self, raw):
        """
        Step the symbol count down and reconnect after a `symbol limit
        exceeded` rejection. True if a retry was started, False once there is
        nothing sensible left to try (caller then gives up to REST).

        The cap is an empirical claim (see symbol_budget), so being wrong
        about it must cost a reconnect, not a session. Each attempt keeps the
        highest-priority symbols - the screener's picks - because those are
        where a signal is actually likely to fire.

        Steps down multiplicatively rather than by one: the limit is a round
        number we do not know, and probing it one symbol at a time would burn
        the opening window doing it.
        """
        self._symbol_limit_retries = getattr(self, "_symbol_limit_retries", 0) + 1
        if self._symbol_limit_retries > SYMBOL_LIMIT_RETRIES:
            return False

        current = len(self._symbols)
        reduced = max(1, int(current * SYMBOL_LIMIT_BACKOFF))
        if reduced >= current:
            return False

        dropped = self._symbols[reduced:]
        self._dropped_symbols = list(self._dropped_symbols) + list(dropped)
        self._symbols = self._symbols[:reduced]
        self._max_subscriptions = reduced

        watcher = self._error_watcher
        if watcher:
            watcher.clear()

        logger.warning(
            f"PriceStream: {raw} at {current} symbols - retrying with "
            f"{reduced} (attempt {self._symbol_limit_retries} of "
            f"{SYMBOL_LIMIT_RETRIES}). Dropped to REST: "
            f"{', '.join(dropped) if dropped else 'none'}. This is the "
            f"unique-symbol cap being probed; the session is NOT lost."
        )
        self._restart_connection(delay=SYMBOL_LIMIT_RETRY_DELAY)
        self._started_at = time.monotonic()
        return True

    def _watch_for_silence(self):
        """
        Shut the stream down if it never delivers anything. See
        NO_DATA_GIVE_UP_SECONDS - the point is to stop alpaca-py's internal
        retry loop from hammering an endpoint that is refusing us, since the
        supervisor below cannot see that happening from outside run().
        """
        while not self._stop_requested.wait(2):
            with self._lock:
                received = self._bars_received
            if received:
                return  # data is flowing; nothing to police

            # A named error from alpaca-py beats waiting out the timeout: it
            # arrives in milliseconds and says exactly what is wrong.
            watcher = self._error_watcher
            retryable = watcher.retryable if watcher else None
            if retryable:
                watcher.clear_retryable()
                self._connection_attempts += 1
                if self._connection_attempts <= CONNECTION_LIMIT_RETRIES:
                    logger.warning(
                        f"PriceStream: {retryable} - the account's single data "
                        f"websocket is still held, almost always a previous "
                        f"process whose socket has not been reaped yet. "
                        f"Retrying in {CONNECTION_LIMIT_RETRY_DELAY}s "
                        f"(attempt {self._connection_attempts} of "
                        f"{CONNECTION_LIMIT_RETRIES}). REST is serving prices "
                        f"meanwhile."
                    )
                    self._restart_connection()
                    # Give the retry its own full grace period rather than
                    # letting the original no-data timeout expire underneath it.
                    self._started_at = time.monotonic()
                    continue
                logger.error(
                    f"PriceStream: still cannot get the account's data websocket "
                    f"after {CONNECTION_LIMIT_RETRIES} attempts ({retryable}). "
                    f"Another process is holding it - check for a second "
                    f"market-bot, or an Alpaca dashboard streaming. Running on "
                    f"REST (~15 min delayed) for this session."
                )
                self._gave_up = True
                self.stop()
                return

            hit = watcher.hit if watcher else None
            if hit:
                raw, explanation = hit
                # A SYMBOL-COUNT rejection is recoverable and should not cost
                # the session. Every other fatal error (auth, entitlement) is
                # not - no number of symbols fixes a rejected key.
                #
                # Added 2026-09-02 alongside the unique-symbol counting model.
                # Raising the cap to 30 is an empirical bet; without this, a
                # wrong bet drops the whole session to ~15-min REST and takes
                # the opening-burst measurement with it, which is exactly the
                # outcome the last four sessions kept producing. With it, the
                # cost of being wrong is one reconnect and a smaller watchlist.
                if "symbol limit exceeded" in (raw or "").lower():
                    if self._reduce_and_retry(raw):
                        continue

                logger.error(
                    f"PriceStream FATAL: the feed rejected this subscription - "
                    f"{explanation}. Alpaca said: \"{raw}\". Giving up on the "
                    f"stream for this session and running on REST (~15 min "
                    f"delayed). Trading continues normally on the fallback."
                )
                self._gave_up = True
                self.stop()
                return

            # The clock starts at the OPEN, not at subscribe.
            #
            # The stream now subscribes up to 4 minutes pre-market, where IEX
            # genuinely has almost no bars - it carries ~2% of US volume and
            # most names simply do not print before the bell. A watchdog counting
            # from subscribe therefore spends its whole 120s budget in a period
            # where silence is the CORRECT observation, and kills a working
            # socket seconds after the open. That is what happened on
            # 2026-08-31: connected, zero bars, gave up at 09:30:39.
            #
            # 120 seconds of silence during MARKET HOURS is genuinely broken.
            # 120 seconds of silence before the bell is just early.
            since = max(self._started_at, self._market_open_monotonic())
            if time.monotonic() - since < NO_DATA_GIVE_UP_SECONDS:
                continue

            logger.error(
                f"PriceStream received ZERO bars in "
                f"{NO_DATA_GIVE_UP_SECONDS}s across {len(self._symbols)} symbols - "
                f"giving up on the stream for this session and running on REST "
                f"(~15 min delayed) instead. Likely an Alpaca connection limit "
                f"(one data websocket per account) or a network its stream "
                f"endpoint refuses. Trading continues normally on the fallback."
            )
            self._gave_up = True
            self.stop()
            return

    def clear_give_up(self):
        """Allow one more connection attempt after the stream wrote itself off.

        A give-up is meant to be final FOR A REASON that has been observed -
        the feed rejected the subscription, or the socket is genuinely dead.
        Before 2026-08-31 it could also fire for a reason that had not been
        observed at all: silence during pre-market, when silence is normal. The
        watchdog no longer counts that time, but a stream that gave up for any
        reason before the bell has still been judged on pre-market evidence.

        So the caller gets exactly one reset, at the open, where the evidence
        actually means something. Returns True if there was a give-up to clear,
        so the caller can say so rather than silently retrying.
        """
        if not self._gave_up:
            return False
        self._gave_up = False
        self._stop_requested.clear()
        self._started_at = time.monotonic()
        return True

    def _market_open_monotonic(self):
        """Today's 09:30 ET as a monotonic timestamp, or -inf outside a session.

        Returns -inf when the open has already passed, so the watchdog behaves
        exactly as it always did once the session is under way: the max() in the
        caller then falls through to _started_at. Only the pre-market case is
        changed.
        """
        try:
            import pytz
            from datetime import datetime
            et = pytz.timezone("America/New_York")
            now = datetime.now(et)
            open_at = now.replace(hour=9, minute=30, second=0, microsecond=0)
            if now >= open_at:
                return float("-inf")
            return time.monotonic() + (open_at - now).total_seconds()
        except Exception:
            # Never let a clock problem disable the watchdog entirely.
            return float("-inf")

    def stop(self):
        """Signal the background thread to stop and close the connection."""
        global _ACTIVE_STREAM
        with _ACTIVE_LOCK:
            if _ACTIVE_STREAM is self:
                _ACTIVE_STREAM = None
        self._stop_requested.set()
        if self._error_watcher is not None:
            try:
                logging.getLogger(ALPACA_WS_LOGGER).removeHandler(self._error_watcher)
            except Exception:
                pass
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
            except Exception as e:
                logger.debug(f"Error stopping stream (usually harmless): {e}")
        self._connected = False

    def _restart_connection(self, delay=None):
        """
        Drop the current socket so the supervisor loop reconnects.

        Closes only the connection, NOT the PriceStream: _stop_requested is
        left clear so _run_forever falls straight into its reconnect branch.

        `delay` defaults to CONNECTION_LIMIT_RETRY_DELAY, which exists for the
        CONNECTION-limit case: another process is holding the account's single
        data websocket, and the server needs time to release it. A SYMBOL-count
        rejection is a different thing entirely - nothing has to be released,
        the same connection just resubscribes with fewer symbols - so that path
        passes a much shorter delay.

        The difference matters at exactly the wrong moment: the opening burst
        window is 180 seconds long, and three retries at 15s each would spend
        45 of them waiting for a release that was never pending.
        """
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
            except Exception as e:
                logger.debug(f"Error closing the socket before retry: {e}")
        wait = CONNECTION_LIMIT_RETRY_DELAY if delay is None else delay
        if wait:
            self._stop_requested.wait(wait)

    def _resolve_feed(self):
        """Turn the config feed string into alpaca-py's DataFeed enum."""
        from alpaca.data.enums import DataFeed

        try:
            return DataFeed(self._feed)
        except ValueError:
            valid = ", ".join(f.value for f in DataFeed)
            logger.error(
                f"Unknown websocket_feed '{self._feed}' (valid: {valid}) - falling back to iex"
            )
            return DataFeed.IEX

    def _run_forever(self):
        """
        Supervise the connection: alpaca-py's run() blocks until the socket
        drops, so a plain call would silently stop delivering bars for the
        rest of the session after one network blip. Reconnect until asked to
        stop.
        """
        from alpaca.data.live import StockDataStream

        # StockDataStream wants the DataFeed enum, not the raw string from
        # config - passing a str fails at connect time with a bare
        # "'str' object has no attribute 'value'", which the supervisor below
        # would otherwise retry forever while REST quietly served every read.
        feed = self._resolve_feed()

        while not self._stop_requested.is_set():
            try:
                self._stream = StockDataStream(
                    self._api_key, self._api_secret, feed=feed
                )
                self._stream.subscribe_bars(self._on_bar, *self._symbols)
                subs = len(self._symbols)
                if self._subscribe_trades:
                    self._stream.subscribe_trades(self._on_trade, *self._symbols)
                    subs *= 2
                # NOT "connected" yet. subscribe_bars/subscribe_trades only
                # register local handlers; no network I/O has happened. Saying
                # "connected" here is what made the 2026-08-21 log read
                # "PriceStream connected, subscribed to 59 symbols" 258ms
                # BEFORE the feed rejected those 59 symbols outright.
                self._connected = True
                logger.info(
                    f"PriceStream opening {self._feed} connection for "
                    f"{len(self._symbols)} symbols "
                    f"({subs} subscriptions: bars"
                    f"{' + trades' if self._subscribe_trades else ''}) - "
                    f"waiting for the first bar to confirm it works"
                )
                self._stream.run()  # blocks until the connection drops
            except Exception as e:
                logger.error(f"PriceStream connection error: {e}")
            finally:
                self._connected = False

            if self._stop_requested.is_set():
                break

            logger.warning(
                f"PriceStream disconnected - reconnecting in {RECONNECT_DELAY_SECONDS}s "
                f"(REST fallback is serving prices meanwhile)"
            )
            self._stop_requested.wait(RECONNECT_DELAY_SECONDS)

        logger.info("PriceStream stopped")

    # ---- data ------------------------------------------------------------

    async def _on_bar(self, bar):
        """Handler invoked by alpaca-py for each incoming 1-minute bar."""
        try:
            record = {
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "timestamp": bar.timestamp,
            }
            with self._lock:
                self._bars[bar.symbol] = record
                self._received_at[bar.symbol] = time.monotonic()
                self._bars_received += 1
        except Exception as e:
            # Never let a malformed bar kill the socket - one bad message
            # would otherwise take down the feed for every symbol.
            logger.error(f"PriceStream error handling bar for {getattr(bar, 'symbol', '?')}: {e}")

    async def _on_trade(self, trade):
        """Handler for each incoming trade tick. Kept trivial - this fires
        far more often than _on_bar and must never become a bottleneck."""
        try:
            price = float(trade.price)
            if price <= 0:
                return
            with self._lock:
                self._last_trade[trade.symbol] = price
                self._trade_received_at[trade.symbol] = time.monotonic()
                self._trades_received += 1
        except Exception as e:
            logger.debug(f"PriceStream error handling trade for {getattr(trade,'symbol','?')}: {e}")

    def get_last_trade_price(self, symbol):
        """
        Most recent trade price, or None if there is no fresh one.

        ENTRY DETECTION ONLY. Exits deliberately keep reading 1-minute bar
        closes via get_bar(), because the two paths want opposite things:
        entries want speed (2026-08-20 measurement: losing entries landed at
        the 87th percentile of the surrounding half-hour, i.e. systematically
        buying the local top, consistent with acting on delayed prices), while
        exits want stability (RESISTANCE fired 14 times that day on moves as
        small as 0.08%, so making the exit path tick-sensitive would re-create
        a problem just fixed).

        Consequence of a bad print or a momentary spread blip is therefore
        bounded: it can cause a missed or slightly early ENTRY, and can never
        stop out a good position.
        """
        with self._lock:
            price = self._last_trade.get(symbol)
            at = self._trade_received_at.get(symbol)
        if price is None or at is None:
            return None
        if time.monotonic() - at > TRADE_STALE_AFTER_SECONDS:
            return None
        return price

    def get_bar(self, symbol):
        """
        Latest streamed bar for `symbol`, or None if there isn't a usable one
        (never streamed, or older than BAR_STALE_AFTER_SECONDS). None means
        "fall back to REST" - it never means "no price".

        Deliberately keeps serving the cache after the stream dies, unlike
        is_healthy(). The two answer different questions: is_healthy() asks "is
        the feed alive" and must say no immediately, while this asks "what is
        the best price available", and a bar up to 180 seconds old is still far
        fresher than REST's ~15-minute delay. Once it ages past the staleness
        window the symbol falls back on its own.
        """
        with self._lock:
            bar = self._bars.get(symbol)
            received_at = self._received_at.get(symbol)

        if bar is None or received_at is None:
            return None
        if time.monotonic() - received_at > BAR_STALE_AFTER_SECONDS:
            return None
        return dict(bar)

    # ---- introspection ---------------------------------------------------

    @property
    def is_connected(self):
        """
        Whether the supervisor currently has a live run() call.

        NOT a health check - see is_healthy(). alpaca-py runs its own internal
        reconnect loop inside run(), so run() keeps blocking (and this keeps
        reporting True) even while every handshake underneath is being
        rejected. A connection refused at the HTTP upgrade - what Alpaca
        returns to a network it won't serve, before credentials are ever
        presented - looks exactly like a healthy connection from out here.
        Judge the stream by whether bars are arriving, not by this.
        """
        return self._connected

    def is_healthy(self):
        """
        True only if a bar has actually arrived recently. This is the signal
        worth alerting on: a stream that is nominally connected but silently
        delivering nothing is indistinguishable from a working one by
        connection state alone, and would otherwise leave the bot quietly
        running on REST fallback for a whole session without saying so.
        """
        # A stream that has given up, or been stopped, is not healthy no matter
        # how recent its last bar was. Without this check a stream that ran fine
        # and then died at 09:45 kept reporting healthy for BAR_STALE_AFTER_SECONDS
        # afterwards, purely because its final bar was still recent - so the run
        # context, and anything else gating on this, would describe a dead feed
        # as a live one for the three minutes when it most mattered.
        if self._gave_up or self._stop_requested.is_set():
            return False
        with self._lock:
            if not self._received_at:
                return False
            newest = max(self._received_at.values())
        return (time.monotonic() - newest) <= BAR_STALE_AFTER_SECONDS

    def stats(self):
        """Snapshot for logging: connection state and coverage."""
        with self._lock:
            fresh = sum(
                1
                for s in self._bars
                if time.monotonic() - self._received_at.get(s, 0) <= BAR_STALE_AFTER_SECONDS
            )
            total_symbols = len(self._bars)
            received = self._bars_received
        return {
            "connected": self._connected,
            "healthy": self.is_healthy(),
            "gave_up": self._gave_up,
            "symbols_with_fresh_bars": fresh,
            "symbols_seen": total_symbols,
            "subscribed": len(self._symbols),
            "bars_received": received,
            "trades_received": self._trades_received,
        }
