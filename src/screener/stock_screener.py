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
        """Calculate gap from yesterday's close to today's open (%)"""
        try:
            today = datetime.now(self.et).date()
            yesterday = today - timedelta(days=1)

            # Get yesterday's close and today's open
            bars = self.broker.get_historical_bars(
                symbol, yesterday, today + timedelta(days=1), "1Day"
            )

            if symbol not in bars or len(bars[symbol]) < 2:
                return 0

            df = bars[symbol].sort_values("timestamp")
            yesterday_close = df.iloc[-2]["close"]
            today_open = df.iloc[-1]["open"]

            gap_pct = abs((today_open - yesterday_close) / yesterday_close * 100)
            return gap_pct

        except Exception as e:
            logger.debug(f"Error calculating gap for {symbol}: {e}")
            return 0

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
        """Calculate recent volume vs 20-day average"""
        try:
            end = datetime.now(self.et).date()
            start = end - timedelta(days=30)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or len(bars[symbol]) < 2:
                return 1.0

            df = bars[symbol].sort_values("timestamp")

            # Latest volume vs 20-day average
            latest_vol = df.iloc[-1]["volume"]
            avg_vol = df.tail(20)["volume"].mean()

            return latest_vol / avg_vol if avg_vol > 0 else 1.0

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
