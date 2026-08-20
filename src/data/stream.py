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

# How long to wait between reconnect attempts after the stream drops.
RECONNECT_DELAY_SECONDS = 5


class PriceStream:
    """
    Background WebSocket consumer maintaining the latest 1-minute bar per
    symbol. Thread-safe: the websocket thread writes, the trading loop reads.
    """

    def __init__(self, api_key, api_secret, feed="iex"):
        self._api_key = api_key
        self._api_secret = api_secret
        self._feed = feed

        self._bars = {}  # symbol -> {open, high, low, close, volume, timestamp}
        self._received_at = {}  # symbol -> time.monotonic() when the bar landed
        self._lock = threading.Lock()

        self._stream = None
        self._thread = None
        self._symbols = []
        self._stop_requested = threading.Event()
        self._connected = False
        self._bars_received = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self, symbols):
        """Connect and subscribe to 1-minute bars for `symbols`."""
        if self._thread and self._thread.is_alive():
            logger.warning("PriceStream.start() called while already running - ignoring")
            return

        self._symbols = list(symbols)
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="price-stream", daemon=True
        )
        self._thread.start()
        logger.info(
            f"PriceStream starting for {len(self._symbols)} symbols on the {self._feed} feed"
        )

    def stop(self):
        """Signal the background thread to stop and close the connection."""
        self._stop_requested.set()
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
                self._connected = True
                logger.info(f"PriceStream connected, subscribed to {len(self._symbols)} symbols")
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
            "symbols_with_fresh_bars": fresh,
            "symbols_seen": total_symbols,
            "subscribed": len(self._symbols),
            "bars_received": received,
        }
