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

FATAL_STREAM_ERRORS = {
    "symbol limit exceeded": (
        "too many symbols subscribed for this feed - lower "
        "stream_max_subscriptions (bars and trades may each count as one)"
    ),
    "connection limit exceeded": (
        "the account already has a data websocket open - another process, or a "
        "previous run whose socket has not closed yet"
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
        self.hit = None

    def emit(self, record):
        try:
            text = str(record.getMessage()).lower()
            for needle, explanation in FATAL_STREAM_ERRORS.items():
                if needle in text:
                    self.hit = (record.getMessage(), explanation)
                    return
        except Exception:
            pass


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

    # ---- lifecycle -------------------------------------------------------

    def symbol_budget(self):
        """
        How many symbols this connection may carry.

        Bars and trades are counted as SEPARATE subscriptions, so enabling trade
        ticks halves the reach. That is the conservative reading of Alpaca's
        limit; if it turns out to count unique symbols instead, raising
        stream_max_subscriptions recovers the difference with no code change.
        """
        channels = 2 if self._subscribe_trades else 1
        return max(1, self._max_subscriptions // channels)

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
                f"{self._feed} feed allows {self._max_subscriptions} subscriptions "
                f"({'bars+trades' if self._subscribe_trades else 'bars'} = "
                f"{2 if self._subscribe_trades else 1} per symbol, so {budget} symbols). "
                f"Streaming the top {len(kept)}; the other {len(dropped)} use REST."
            )
            logger.info(f"PriceStream streaming: {', '.join(kept)}")
            logger.info(f"PriceStream on REST: {', '.join(dropped)}")
            requested = kept

        self._symbols = requested
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
            hit = self._error_watcher.hit if self._error_watcher else None
            if hit:
                raw, explanation = hit
                logger.error(
                    f"PriceStream FATAL: the feed rejected this subscription - "
                    f"{explanation}. Alpaca said: \"{raw}\". Giving up on the "
                    f"stream for this session and running on REST (~15 min "
                    f"delayed). Trading continues normally on the fallback."
                )
                self._gave_up = True
                self.stop()
                return

            if time.monotonic() - self._started_at < NO_DATA_GIVE_UP_SECONDS:
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

    def stop(self):
        """Signal the background thread to stop and close the connection."""
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
