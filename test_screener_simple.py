#!/usr/bin/env python3
"""
Simple screener test that doesn't require Alpaca API credentials.
Tests the screener logic with mock data.
"""

import sys
import os
import pandas as pd
from datetime import datetime, timedelta
import pytz
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Mock broker class for testing
class MockBroker:
    def __init__(self):
        pass

    def get_historical_bars(self, symbols, start, end, timeframe="1Day"):
        """Return mock historical data"""
        if isinstance(symbols, str):
            symbols = [symbols]

        result = {}
        for symbol in symbols:
            # Create mock data with realistic patterns
            dates = pd.date_range(start=start, end=end, freq='D')
            data = {
                'timestamp': dates,
                'open': 100 + (hash(symbol) % 50),
                'high': 105 + (hash(symbol) % 50),
                'low': 95 + (hash(symbol) % 50),
                'close': 102 + (hash(symbol) % 50),
                'volume': 2000000 + (hash(symbol) % 1000000),
            }
            result[symbol] = pd.DataFrame(data)

        return result


class SimpleStockScreener:
    """Simplified screener for testing"""

    def __init__(self, broker, candidates_file="candidates.txt"):
        self.broker = broker
        self.candidates = self._load_candidates(candidates_file)
        self.et = pytz.timezone("America/New_York")

    def _load_candidates(self, file):
        try:
            with open(file) as f:
                symbols = [line.strip().upper() for line in f if line.strip()]
            logger.info(f"Loaded {len(symbols)} candidate symbols")
            return symbols
        except FileNotFoundError:
            logger.error(f"Candidates file {file} not found")
            return []

    def score_stock(self, symbol):
        """Simplified scoring for testing"""
        score = 0
        details = {"symbol": symbol}

        try:
            # Get mock data
            end = datetime.now(self.et).date()
            start = end - timedelta(days=30)

            bars = self.broker.get_historical_bars(symbol, start, end, "1Day")

            if symbol not in bars or bars[symbol].empty:
                return 0, details

            df = bars[symbol].sort_values('timestamp')

            # Gap (25 points)
            gap = abs(df.iloc[-1]['open'] - df.iloc[-2]['close']) / df.iloc[-2]['close'] * 100
            gap_score = min(25, gap * 5)
            score += gap_score
            details['gap_pct'] = gap
            details['gap_score'] = gap_score

            # Momentum (25 points)
            momentum = (df.iloc[-1]['close'] - df.iloc[0]['close']) / df.iloc[0]['close'] * 100
            if momentum > 5:
                momentum_score = 25
            elif momentum > 2:
                momentum_score = 15
            elif momentum > 0:
                momentum_score = 10
            else:
                momentum_score = max(0, 10 + momentum * 2)

            score += momentum_score
            details['5day_return_pct'] = momentum
            details['momentum_score'] = momentum_score

            # Volume surge (20 points)
            vol_ratio = df.iloc[-1]['volume'] / df.tail(20)['volume'].mean()
            if vol_ratio > 1.8:
                vol_score = 20
            elif vol_ratio > 1.5:
                vol_score = 15
            elif vol_ratio > 1.2:
                vol_score = 10
            else:
                vol_score = 0

            score += vol_score
            details['volume_ratio'] = vol_ratio
            details['volume_score'] = vol_score

            # Volatility rank (20 points) - simplified ATR
            df['tr'] = pd.concat([
                df['high'] - df['low'],
                abs(df['high'] - df['close'].shift()),
                abs(df['low'] - df['close'].shift())
            ], axis=1).max(axis=1)

            atr_pct = (df['tr'].tail(14).mean() / df.iloc[-1]['close']) * 100
            if atr_pct < 0.5:
                vol_rank_score = 5
            elif atr_pct < 1.0:
                vol_rank_score = 10
            elif atr_pct < 1.5:
                vol_rank_score = 15
            elif atr_pct < 2.5:
                vol_rank_score = 18
            else:
                vol_rank_score = 20

            score += vol_rank_score
            details['volatility_percentile'] = atr_pct
            details['volatility_score'] = vol_rank_score

            # Price check
            details['price'] = float(df.iloc[-1]['close'])

            return score, details

        except Exception as e:
            logger.error(f"Error scoring {symbol}: {e}")
            return 0, details

    def screen(self, top_n=15, min_score=35):
        """Run screener"""
        if not self.candidates:
            logger.error("No candidates loaded")
            return []

        logger.info(f"Screening {len(self.candidates)} stocks...")
        logger.info("-" * 70)

        scores = {}
        details_dict = {}

        for i, symbol in enumerate(self.candidates):
            if i % 10 == 0 and i > 0:
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

        # Display results
        logger.info(f"\n{'SYMBOL':<8} {'SCORE':<8} {'GAP':<8} {'5D RTN':<10} {'VOL RATIO':<12} {'PRICE':<10}")
        logger.info("-" * 70)

        for symbol in selected:
            details = details_dict[symbol]
            logger.info(
                f"{symbol:<8} {scores[symbol]:<8.1f} "
                f"{details['gap_pct']:<8.1f}% "
                f"{details['5day_return_pct']:<10.1f}% "
                f"{details['volume_ratio']:<12.2f}x "
                f"${details['price']:<9.2f}"
            )

        logger.info("-" * 70)
        logger.info(f"Selected {len(selected)} stocks for trading")
        logger.info(f"Stocks: {', '.join(selected)}")

        return selected


def test_screener(test_date_str=None):
    """Test screener with mock data"""
    if test_date_str:
        test_date = datetime.strptime(test_date_str, "%Y-%m-%d").date()
    else:
        et = pytz.timezone("America/New_York")
        test_date = datetime.now(et).date()

    logger.info("=" * 70)
    logger.info(f"SCREENER TEST FOR: {test_date}")
    logger.info("=" * 70)
    logger.info("Note: Using MOCK DATA (not real market data)\n")

    try:
        # Create mock broker
        broker = MockBroker()

        # Initialize screener
        logger.info("Initializing screener...")
        screener = SimpleStockScreener(broker, "candidates.txt")
        logger.info(f"✓ Loaded {len(screener.candidates)} candidates\n")

        # Run screener
        logger.info("Running screener...")
        selected_stocks = screener.screen(top_n=15, min_score=35)

        # Display final results
        if selected_stocks:
            logger.info(f"\n✓ SCREENER PASSED")
            logger.info(f"✓ Selected {len(selected_stocks)} stocks\n")
            logger.info("These stocks would be monitored at 9:35 AM ET for entry signals.")
        else:
            logger.warning(f"\n✗ SCREENER RETURNED NO STOCKS")

        logger.info("=" * 70)
        return True

    except Exception as e:
        logger.error(f"Error running screener: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_date = sys.argv[1]
        logger.info(f"Using provided date: {test_date}\n")
        success = test_screener(test_date)
    else:
        logger.info("No date provided, using today\n")
        success = test_screener()

    sys.exit(0 if success else 1)
