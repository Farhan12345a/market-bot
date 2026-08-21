import pandas as pd
import logging
from datetime import datetime, timedelta
import pytz
import yfinance as yf
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

class StockScreener:
    """Daily pre-market screener to identify high-volatility candidates"""

    def __init__(self, broker, candidates_file="candidates.txt"):
        self.broker = broker
        self.candidates = self._load_candidates(candidates_file)
        self.et = pytz.timezone("America/New_York")

    def _load_candidates(self, file) -> List[str]:
        """Load candidate symbols from file"""
        try:
            with open(file) as f:
                symbols = [line.strip().upper() for line in f if line.strip()]
            logger.info(f"Loaded {len(symbols)} candidate symbols")
            return symbols
        except FileNotFoundError:
            logger.error(f"Candidates file {file} not found")
            return []

    def _get_recent_gap(self, symbol) -> float:
        """
        Gap from yesterday's close to where the stock is trading NOW (%).

        Deliberately measured against the current/pre-market price rather than
        today's official opening print. The screener runs at 09:05 ET (see
        screener_start_time), and today's DAILY bar does not exist yet at that
        hour - so the previous implementation, which required two daily bars
        and returned 0 when it could not get both, scored a 0.0% gap for every
        single symbol. On 2026-08-20 that made MRVL, CADL and CMG tie at an
        identical 44.0 and cut the selection to 3 names; the accidental
        post-open re-run the same morning scored the same universe 79.0 / 66.7
        / 58.1 with real gaps (COIN 7.8%, HOOD 5.3%), which is what the
        component is worth when it works.

        Measuring against the live price is also the better signal: it is
        where the stock is actually changing hands right now, not one opening
        print, and it is available before the bell when the decision is made.

        Falls back to the old today's-open path when there is no current
        price (thin or absent pre-market prints on the free IEX feed), so a
        post-open run behaves exactly as before.
        """
        try:
            today = datetime.now(self.et).date()
            start = today - timedelta(days=7)  # buffer for weekends/holidays

            bars = self.broker.get_historical_bars(
                symbol, start, today + timedelta(days=1), "1Day"
            )
            if symbol not in bars or bars[symbol].empty:
                return 0.0

            df = bars[symbol].sort_values("timestamp")

            # Rows dated today are today's own (partial) daily bar - exclude
            # them so "yesterday's close" is genuinely the prior session.
            df["_date"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(self.et).dt.date
            prior = df[df["_date"] < today]
            if prior.empty:
                return 0.0
            yesterday_close = float(prior.iloc[-1]["close"])
            if yesterday_close <= 0:
                return 0.0

            current = self._get_current_price(symbol)
            if not current:
                # No live print - fall back to today's official open if the
                # daily bar has appeared (i.e. we are running after the bell).
                today_rows = df[df["_date"] == today]
                if today_rows.empty:
                    return 0.0
                current = float(today_rows.iloc[-1]["open"])

            return abs((current - yesterday_close) / yesterday_close * 100)

        except Exception as e:
            logger.debug(f"Error calculating gap for {symbol}: {e}")
            return 0.0

    def _get_current_price(self, symbol) -> float:
        """
        Latest traded price, including pre-market. Returns 0 when unavailable.

        Tries the most recent 1-minute bar first (covers extended hours on the
        IEX feed) and falls back to the latest quote midpoint. Both are
        best-effort: a thinly traded name may have neither before the bell,
        which the caller handles.
        """
        try:
            now = datetime.now(self.et)
            bars = self.broker.get_historical_bars(
                symbol, now - timedelta(hours=12), now, "1Min"
            )
            if symbol in bars and not bars[symbol].empty:
                price = float(bars[symbol].sort_values("timestamp").iloc[-1]["close"])
                if price > 0:
                    return price
        except Exception as e:
            logger.debug(f"No recent minute bar for {symbol}: {e}")

        try:
            quote = self.broker.get_latest_quote(symbol)
            if quote and quote.get("bid") and quote.get("ask"):
                return (float(quote["bid"]) + float(quote["ask"])) / 2
        except Exception as e:
            logger.debug(f"No quote for {symbol}: {e}")

        return 0.0

    def _get_5day_return(self, symbol) -> float:
        """Calculate 5-day return (%)"""
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=7)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or len(bars[symbol]) < 2:
                return 0

            df = bars[symbol].sort_values("timestamp")
            close_5d_ago = df.iloc[0]["close"]
            latest_close = df.iloc[-1]["close"]

            return ((latest_close - close_5d_ago) / close_5d_ago * 100)

        except Exception as e:
            logger.debug(f"Error calculating 5-day return for {symbol}: {e}")
            return 0

    def _get_volume_ratio(self, symbol) -> float:
        """
        Compare today's volume-so-far to the average volume accumulated by
        this same time of day over the past 10 trading days.

        The same-time-of-day comparison is what makes this work at any hour,
        including pre-market: screen() now runs before the open (see
        screener_start_time), so "today so far" is today's pre-market volume
        and each prior day is measured to the same pre-market cutoff. Heavy
        pre-market volume relative to the same stock's own norm is one of the
        few genuinely forward-looking signals available before the bell.

        Returns a neutral 1.0 when there isn't enough data to judge - thin or
        absent pre-market prints on the free IEX feed are common for less
        liquid names, and a missing measurement should not be scored as a
        volume surge.
        """
        try:
            now = datetime.now(self.et)
            today = now.date()
            cutoff_time = now.time()

            lookback_days = 10
            start = today - timedelta(days=lookback_days * 2)  # buffer for weekends/holidays

            # `end` must be a DATETIME, not a date. A bare date is coerced to
            # midnight, so passing `today` asked for bars strictly BEFORE
            # today - today's own bars were never returned, today_vol was
            # always 0, and the function returned its neutral 1.0 fallback
            # every single time. That is why every candidate scored an
            # identical "Vol Ratio: 1.00x" on 2026-08-19: volume was
            # contributing nothing to the ranking at all.
            bars = self.broker.get_historical_bars(symbol, start, now, "5Min")

            if symbol not in bars or bars[symbol].empty:
                return 1.0

            df = bars[symbol].copy()
            df["ts_et"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(self.et)
            df["date"] = df["ts_et"].dt.date
            df["time"] = df["ts_et"].dt.time

            today_vol = df.loc[(df["date"] == today) & (df["time"] <= cutoff_time), "volume"].sum()
            if today_vol == 0:
                return 1.0

            prior_days = sorted(d for d in df["date"].unique() if d < today)[-lookback_days:]

            prior_sums = []
            for d in prior_days:
                day_vol = df.loc[(df["date"] == d) & (df["time"] <= cutoff_time), "volume"].sum()
                if day_vol > 0:
                    prior_sums.append(day_vol)

            if not prior_sums:
                return 1.0

            avg_prior = sum(prior_sums) / len(prior_sums)
            return today_vol / avg_prior if avg_prior > 0 else 1.0

        except Exception as e:
            logger.debug(f"Error calculating volume ratio for {symbol}: {e}")
            return 1.0

    def _get_volatility_percentile(self, symbol) -> float:
        """
        Calculate where this stock's volatility ranks among candidates (0-100).
        Uses ATR (Average True Range) as volatility metric.
        """
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=30)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or len(bars[symbol]) < 2:
                return 50

            df = bars[symbol].sort_values("timestamp")

            # Calculate ATR (simplified: average of true ranges)
            df["tr"] = pd.concat([
                df["high"] - df["low"],
                abs(df["high"] - df["close"].shift()),
                abs(df["low"] - df["close"].shift())
            ], axis=1).max(axis=1)

            atr = df["tr"].tail(14).mean()
            atr_pct = (atr / df.iloc[-1]["close"]) * 100

            # Percentile relative to typical stock (assume 1-3% ATR is median)
            if atr_pct < 0.5:
                return 10
            elif atr_pct < 1.0:
                return 30
            elif atr_pct < 1.5:
                return 50
            elif atr_pct < 2.5:
                return 75
            else:
                return 95

        except Exception as e:
            logger.debug(f"Error calculating volatility for {symbol}: {e}")
            return 50

    def _has_earnings_nearby(self, symbol) -> bool:
        """Check if stock has earnings in the next 5 days"""
        try:
            # Use yfinance to get earnings dates
            ticker = yf.Ticker(symbol)
            calendar = ticker.quarterly_financials

            if calendar is None or calendar.empty:
                return False

            # This is a simplified check - yfinance's earnings data can be delayed
            return True  # Conservative: assume we can't reliably get earnings

        except Exception as e:
            logger.debug(f"Error checking earnings for {symbol}: {e}")
            return False

    def _get_price(self, symbol) -> float:
        """Get latest closing price"""
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=1)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or bars[symbol].empty:
                return 0

            df = bars[symbol].sort_values("timestamp")
            return float(df.iloc[-1]["close"])

        except Exception as e:
            logger.debug(f"Error getting price for {symbol}: {e}")
            return 0

    def score_stock(self, symbol) -> Tuple[float, Dict]:
        """
        Score a stock 0-100 for likelihood of volatility in first 30 min.
        Returns (score, details_dict)
        """
        score = 0
        details = {"symbol": symbol}

        try:
            # Recent gap (25 points max) - strong predictor of open volatility
            gap = self._get_recent_gap(symbol)
            gap_score = min(25, gap * 5)  # 5% gap = 25 points
            score += gap_score
            details["gap_pct"] = gap
            details["gap_score"] = gap_score

            # Momentum (25 points max) - continuation likely
            momentum = self._get_5day_return(symbol)
            if momentum > 5:
                momentum_score = 25
            elif momentum > 2:
                momentum_score = 15
            elif momentum > 0:
                momentum_score = 10
            else:
                momentum_score = max(0, 10 + momentum * 2)  # Penalize losses

            score += momentum_score
            details["5day_return_pct"] = momentum
            details["momentum_score"] = momentum_score

            # Volume surge (20 points max)
            vol_ratio = self._get_volume_ratio(symbol)
            if vol_ratio > 1.8:
                vol_score = 20
            elif vol_ratio > 1.5:
                vol_score = 15
            elif vol_ratio > 1.2:
                vol_score = 10
            else:
                vol_score = 0

            score += vol_score
            details["volume_ratio"] = vol_ratio
            details["volume_score"] = vol_score

            # Volatility rank (20 points max)
            vol_percentile = self._get_volatility_percentile(symbol)
            vol_rank_score = vol_percentile * 0.2
            score += vol_rank_score
            details["volatility_percentile"] = vol_percentile
            details["volatility_score"] = vol_rank_score

            # Price check (filter only)
            price = self._get_price(symbol)
            details["price"] = price

            return score, details

        except Exception as e:
            logger.error(f"Error scoring {symbol}: {e}")
            return 0, details

    def screen(self, top_n: int = 25, min_score: float = 40) -> List[str]:
        """
        Run daily screen on all candidates.
        Returns top N stocks by score that meet minimum threshold.
        """
        if not self.candidates:
            logger.error("No candidates loaded")
            return []

        logger.info(f"Screening {len(self.candidates)} stocks...")

        scores = {}
        details_dict = {}

        for i, symbol in enumerate(self.candidates):
            if i % 20 == 0:
                logger.info(f"  Progress: {i}/{len(self.candidates)}")

            try:
                score, details = self.score_stock(symbol)
                scores[symbol] = score
                details_dict[symbol] = details
            except Exception as e:
                logger.warning(f"Error screening {symbol}: {e}")
                continue

        # Sort by score
        sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Filter by minimum score and take top N
        selected = [
            sym for sym, score in sorted_stocks
            if score >= min_score
        ][:top_n]

        # Log results
        logger.info(f"=" * 60)
        logger.info(f"STOCK SCREENER RESULTS - {datetime.now(self.et).strftime('%Y-%m-%d')}")
        logger.info(f"=" * 60)

        for symbol in selected:
            details = details_dict[symbol]
            logger.info(
                f"{symbol:6} | Score: {scores[symbol]:6.1f} | "
                f"Gap: {details['gap_pct']:5.1f}% | "
                f"5d Return: {details['5day_return_pct']:6.1f}% | "
                f"Vol Ratio: {details['volume_ratio']:4.2f}x | "
                f"Price: ${details['price']:7.2f}"
            )

        logger.info(f"=" * 60)
        logger.info(f"Selected {len(selected)} stocks for trading today")
        logger.info(f"Stocks: {', '.join(selected)}")
        logger.info(f"=" * 60)

        return selected
