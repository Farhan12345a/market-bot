import pandas as pd
import logging
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger(__name__)

class MarketDataManager:
    """Manages market data fetching and calculations"""

    def __init__(self, broker, stream=None):
        self.broker = broker
        self.cache = {}  # Cache for volume averages and historical data
        self.et = pytz.timezone("America/New_York")

        # Optional real-time WebSocket source (see src/data/stream.py). When
        # present it serves get_latest_bar; REST remains the fallback for any
        # symbol the stream hasn't delivered a fresh bar for.
        self.stream = stream
        self._stream_hits = 0
        self._rest_fallbacks = 0

    def get_20day_avg_volume(self, symbol):
        """Get 20-day average volume"""
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=30)

            bars_data = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars_data or bars_data[symbol].empty:
                logger.warning(f"No historical data for {symbol}")
                return 1000000  # Default fallback

            df = bars_data[symbol]
            avg_volume = df["volume"].tail(20).mean()

            self.cache[f"{symbol}_avg_volume"] = avg_volume
            logger.debug(f"{symbol}: 20-day avg volume = {avg_volume:.0f}")

            return avg_volume
        except Exception as e:
            logger.error(f"Error calculating avg volume for {symbol}: {e}")
            return 1000000  # Fallback

    def get_rsi(self, symbol, period=14):
        """
        Standard RSI (simple moving average of gains/losses, not Wilder-smoothed)
        computed from daily closes. Returns None if there isn't enough history.
        """
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=period * 3)  # buffer for weekends/holidays

            bars_data = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars_data or bars_data[symbol].empty:
                logger.warning(f"No historical data for {symbol} RSI calc")
                return None

            closes = bars_data[symbol]["close"].tolist()
            if len(closes) < period + 1:
                logger.warning(f"{symbol}: only {len(closes)} daily closes, need {period + 1} for RSI")
                return None

            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            recent = deltas[-period:]
            gains = [d for d in recent if d > 0]
            losses = [-d for d in recent if d < 0]
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period

            if avg_loss == 0:
                return 100.0

            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))
        except Exception as e:
            logger.error(f"Error calculating RSI for {symbol}: {e}")
            return None

    def get_open_bar(self, symbol):
        """Get the opening 5-minute bar (9:30-9:35 ET)"""
        try:
            now = datetime.now(self.et)

            # Get bars from market open (9:30) until now
            start = now.replace(hour=9, minute=30, second=0, microsecond=0)
            end = now

            bars_data = self.broker.get_historical_bars(
                symbol, start, end, "5Min"
            )

            if symbol not in bars_data or bars_data[symbol].empty:
                logger.warning(f"No intraday data for {symbol}")
                return None

            df = bars_data[symbol]
            first_bar = df.iloc[0]

            return {
                "open": first_bar["open"],
                "high": first_bar["high"],
                "low": first_bar["low"],
                "close": first_bar["close"],
                "volume": first_bar["volume"],
                "timestamp": first_bar["timestamp"],
            }
        except Exception as e:
            logger.error(f"Error fetching open bar for {symbol}: {e}")
            return None

    def get_latest_bar(self, symbol, timeframe="1Min"):
        """
        Latest bar for a symbol, preferring the real-time WebSocket stream and
        falling back to REST.

        The preference matters: Alpaca's free tier delays the REST historical
        endpoint by ~15 minutes, while the WebSocket carries the same IEX data
        live. Every entry and exit decision reads prices through here, so a
        REST-only path meant acting on prices that could be minutes old.

        The fallback is what keeps this safe. The stream returns None for any
        symbol it hasn't delivered a fresh bar for - not yet connected, no
        trades in the last few minutes, or the socket dropped - and the REST
        path then serves the request exactly as before. A dead stream degrades
        the bot to its previous behavior instead of freezing prices.
        """
        if self.stream is not None and timeframe == "1Min":
            bar = self.stream.get_bar(symbol)
            if bar is not None:
                self._stream_hits += 1
                return bar
            self._rest_fallbacks += 1

        try:
            bars = self.broker.get_latest_bars(symbol, timeframe)
            if symbol in bars:
                return bars[symbol]
            return None
        except Exception as e:
            logger.error(f"Error fetching latest bar for {symbol}: {e}")
            return None

    def data_source_stats(self):
        """Counts of stream-served vs REST-served bar reads, for logging."""
        total = self._stream_hits + self._rest_fallbacks
        return {
            "stream_hits": self._stream_hits,
            "rest_fallbacks": self._rest_fallbacks,
            "stream_pct": (100.0 * self._stream_hits / total) if total else 0.0,
        }

    def is_trading_day(self, now=None):
        """
        True if today is a weekday. Mirrors is_market_open's own weekday-only
        notion of a trading day - neither consults a holiday calendar, so both
        will treat a market holiday as a trading day and simply find no data.
        """
        now = now or datetime.now(self.et)
        return now.weekday() < 5

    def is_market_open(self):
        """Check if the market is currently open"""
        now = datetime.now(self.et)
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

        # Check if it's a weekday
        if now.weekday() >= 5:  # Saturday or Sunday
            return False

        return market_open <= now <= market_close

