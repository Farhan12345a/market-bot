#!/usr/bin/env python3
"""
Screener Backtest Runner - Test screener across multiple dates
with proper historical data handling (no forward-looking bias).
"""

import sys
import os
import pandas as pd
import yaml
from datetime import datetime, timedelta
import pytz
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Mock broker for testing
class MockBroker:
    """Mock broker that simulates historical data"""

    def __init__(self):
        pass

    def get_historical_bars(self, symbols, start, end, timeframe="1Day"):
        """Return mock historical data between start and end dates (inclusive)"""
        if isinstance(symbols, str):
            symbols = [symbols]

        result = {}
        for symbol in symbols:
            # Create date-dependent seed (includes end date for variation)
            symbol_seed = hash(symbol) % 1000
            date_seed = (end.year * 10000 + end.month * 100 + end.day) % 1000
            combined_seed = (symbol_seed + date_seed) % 1000

            dates = pd.date_range(start=start, end=end, freq='B')  # Business days only

            # Generate realistic patterns that vary by date
            n = len(dates)
            trend = (combined_seed % 100) / 100.0  # 0-1 trend
            base_price = 100 + (symbol_seed % 50)

            prices = []
            volumes = []
            current_price = base_price

            for i, date in enumerate(dates):
                # Date-aware variation: momentum changes over time
                date_factor = (date.year * 365 + date.timetuple().tm_yday) % 100
                dynamic_trend = ((trend + date_factor / 100) % 1.0) - 0.5

                # Add trend and random walk
                change = dynamic_trend * 2 + (combined_seed * (i + 1) % 10 - 5) / 100
                current_price = max(10, current_price * (1 + change / 100))
                prices.append(current_price)

                # Volume pattern (varies by date)
                vol_seed = (combined_seed * (i + 1) + date_factor) % 1000000
                vol = 1500000 + vol_seed
                if (i + date_factor) % 7 == 0:  # Spike volume on certain days
                    vol *= (1.3 + (date_factor % 10) / 100)
                volumes.append(vol)

            # Create OHLC data with date-dependent variations
            open_var = 0.98 + (date_seed % 3) / 100
            high_var = 1.01 + (date_seed % 2) / 100
            low_var = 0.97 + (date_seed % 3) / 100

            data = {
                'timestamp': dates,
                'open': [p * open_var for p in prices],
                'high': [p * high_var for p in prices],
                'low': [p * low_var for p in prices],
                'close': prices,
                'volume': volumes,
            }
            result[symbol] = pd.DataFrame(data)

        return result


class StockScreener:
    """Stock screener with date-aware historical data handling"""

    def __init__(self, broker, candidates_file="candidates.txt"):
        self.broker = broker
        self.candidates = self._load_candidates(candidates_file)
        self.et = pytz.timezone("America/New_York")

    def _load_candidates(self, file):
        try:
            with open(file) as f:
                symbols = [line.strip().upper() for line in f if line.strip()]
            return symbols
        except FileNotFoundError:
            logger.error(f"Candidates file {file} not found")
            return []

    def score_stock(self, symbol, test_date, min_momentum=0, trend_days=5):
        """
        Score a stock using only data up to test_date.

        Args:
            min_momentum: Minimum required momentum (%) to consider stock.
                         Default 0 means any stock with positive momentum.
                         Set to -999 to disable filter.
            trend_days: Number of days to check for bullish trend
        """
        score = 0
        details = {"symbol": symbol}

        try:
            # Calculate date ranges - always looking back from test_date
            end_date = test_date
            start_date = test_date - timedelta(days=30)

            # Fetch historical data UP TO test_date (no future data)
            bars = self.broker.get_historical_bars(symbol, start_date, end_date, "1Day")

            if symbol not in bars or bars[symbol].empty:
                return 0, details

            df = bars[symbol].sort_values('timestamp')

            # Must have at least 2 days for gap calculation
            if len(df) < 2:
                return 0, details

            # === BULLISH TREND FILTER ===
            # PRIMARY: Simple momentum filter (ACTIVE)
            trend_score = 0
            if min_momentum > -999:
                # Check recent close vs close from N days ago
                lookback_idx = min(trend_days, len(df) - 1)
                price_lookback = df.iloc[-lookback_idx - 1]['close']
                latest_price = df.iloc[-1]['close']
                recent_trend = ((latest_price - price_lookback) / price_lookback) * 100

                # Reject if trending bearish
                if recent_trend < min_momentum:
                    details['trend_pct'] = round(recent_trend, 2)
                    details['status'] = 'REJECTED_BEARISH_TREND'
                    return 0, details

                # Award bonus points for bullish trend strength
                if recent_trend > 5:
                    trend_score = 15  # Strong bullish trend
                elif recent_trend > 2:
                    trend_score = 10  # Moderate bullish trend
                else:
                    trend_score = 5   # Slight bullish trend

                details['trend_pct'] = round(recent_trend, 2)
                details['is_bullish'] = True
                details['trend_score'] = trend_score
            else:
                details['is_bullish'] = None
                details['trend_score'] = 0

            # === OPTIONAL TREND FILTERS (COMMENTED OUT - UNCOMMENT TO ENABLE) ===

            # FILTER 1: Moving Average Crossover
            # Requires price to be above N-day SMA (simple moving average)
            # Uncomment to enable:
            """
            if len(df) >= 10:
                sma_10 = df.tail(10)['close'].mean()
                if df.iloc[-1]['close'] < sma_10:
                    details['status'] = 'REJECTED_BELOW_SMA10'
                    return 0, details
            """

            # FILTER 2: Higher Lows Pattern
            # Requires recent lows to be getting higher (confirms uptrend)
            # Uncomment to enable:
            """
            if len(df) >= 5:
                recent_lows = df.tail(5)['low'].tolist()
                is_higher_lows = all(recent_lows[i] < recent_lows[i+1] for i in range(len(recent_lows)-1))
                if not is_higher_lows:
                    details['status'] = 'REJECTED_LOWER_LOWS'
                    return 0, details
            """

            # FILTER 3: Bullish Days Ratio
            # Requires X% of recent days to be up days (close > open)
            # Uncomment to enable:
            """
            if len(df) >= 5:
                recent_days = df.tail(5)
                up_days = (recent_days['close'] > recent_days['open']).sum()
                bullish_ratio = (up_days / len(recent_days)) * 100
                min_bullish_ratio = 60  # Require 60% of days to be up
                if bullish_ratio < min_bullish_ratio:
                    details['status'] = 'REJECTED_LOW_BULLISH_RATIO'
                    details['bullish_ratio'] = round(bullish_ratio, 1)
                    return 0, details
            """

            # FILTER 4: RSI > 50 (Momentum Indicator)
            # RSI > 50 suggests bullish momentum
            # Uncomment to enable:
            """
            if len(df) >= 14:
                delta = df['close'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss if loss.iloc[-1] != 0 else 0
                rsi = 100 - (100 / (1 + rs))
                if rsi.iloc[-1] < 50:
                    details['status'] = 'REJECTED_LOW_RSI'
                    details['rsi'] = round(rsi.iloc[-1], 1)
                    return 0, details
            """

            # FILTER 5: Consecutive Up Days
            # Requires N consecutive days where close > open
            # Uncomment to enable:
            """
            min_consecutive_up = 2  # Require 2+ consecutive green candles
            if len(df) >= min_consecutive_up:
                recent_days = df.tail(min_consecutive_up)
                consecutive_up = (recent_days['close'] > recent_days['open']).all()
                if not consecutive_up:
                    details['status'] = 'REJECTED_NO_CONSECUTIVE_UP'
                    return 0, details
            """

            # === GAP ANALYSIS ===
            # Gap = today's open vs yesterday's close
            # Only using data UP TO test_date
            yesterday_close = df.iloc[-2]['close']
            today_open = df.iloc[-1]['open']
            gap = abs((today_open - yesterday_close) / yesterday_close * 100)

            gap_score = min(25, gap * 5)
            score += gap_score
            details['gap_pct'] = round(gap, 2)
            details['gap_score'] = round(gap_score, 1)

            # === MOMENTUM (5-day return) ===
            # Calculate return from 5 days ago to test_date
            if len(df) >= 5:
                price_5d_ago = df.iloc[-5]['close']
                latest_price = df.iloc[-1]['close']
                momentum = ((latest_price - price_5d_ago) / price_5d_ago) * 100
            else:
                momentum = 0

            if momentum > 5:
                momentum_score = 25
            elif momentum > 2:
                momentum_score = 15
            elif momentum > 0:
                momentum_score = 10
            else:
                momentum_score = max(0, 10 + momentum * 2)

            score += momentum_score
            details['momentum_pct'] = round(momentum, 2)
            details['momentum_score'] = round(momentum_score, 1)

            # === VOLUME SURGE ===
            # Recent volume vs 20-day average
            recent_vol = df.iloc[-1]['volume']
            avg_vol = df.tail(20)['volume'].mean()
            vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

            if vol_ratio > 1.8:
                vol_score = 20
            elif vol_ratio > 1.5:
                vol_score = 15
            elif vol_ratio > 1.2:
                vol_score = 10
            else:
                vol_score = 0

            score += vol_score
            details['volume_ratio'] = round(vol_ratio, 2)
            details['volume_score'] = round(vol_score, 1)

            # === VOLATILITY (ATR) ===
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
            details['volatility_pct'] = round(atr_pct, 2)
            details['volatility_score'] = round(vol_rank_score, 1)

            # Price at test_date
            details['price'] = round(float(df.iloc[-1]['close']), 2)

            # Add trend score to total
            score += trend_score

            return score, details

        except Exception as e:
            logger.debug(f"Error scoring {symbol} for {test_date}: {e}")
            return 0, details

    def screen(self, test_date, top_n=15, min_score=35, min_momentum=0, trend_days=5):
        """
        Run screener for a specific date using only historical data UP TO that date.

        Args:
            min_momentum: Minimum required recent momentum (%) to qualify.
                         Default 0 = must be bullish (positive momentum)
                         Set to -999 to disable trend filter
            trend_days: Number of recent days to check for bullish trend
        """
        if not self.candidates:
            logger.error("No candidates loaded")
            return []

        scores = {}
        details_dict = {}

        for i, symbol in enumerate(self.candidates):
            try:
                score, details = self.score_stock(
                    symbol, test_date,
                    min_momentum=min_momentum,
                    trend_days=trend_days
                )
                scores[symbol] = score
                details_dict[symbol] = details
            except Exception as e:
                logger.debug(f"Error screening {symbol}: {e}")
                continue

        # Sort by score
        sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Filter by minimum score and take top N
        selected = [sym for sym, score in sorted_stocks if score >= min_score][:top_n]

        return selected, sorted_stocks, details_dict


def run_backtest(config_file="screener_test_config.yaml"):
    """Run backtest suite from config file"""

    # Load config
    with open(config_file) as f:
        config = yaml.safe_load(f)

    test_runs = config.get('test_runs', [])
    screener_params = config.get('screener_params', {})
    settings = config.get('settings', {})

    if not test_runs:
        logger.error("No test runs defined in config")
        return

    logger.info("=" * 80)
    logger.info(f"SCREENER BACKTEST SUITE - {len(test_runs)} tests")
    logger.info("=" * 80)

    # Initialize broker and screener
    broker = MockBroker()
    screener = StockScreener(
        broker,
        screener_params.get('candidates_file', 'candidates.txt')
    )
    logger.info(f"Loaded {len(screener.candidates)} candidate symbols\n")

    # Results storage
    all_results = []

    # Run each test
    for idx, test_run in enumerate(test_runs, 1):
        test_name = test_run.get('name', f'Test {idx}')
        test_date_str = test_run.get('date')
        description = test_run.get('description', '')

        try:
            # Parse date
            test_date = datetime.strptime(test_date_str, "%Y-%m-%d").date()

            logger.info(f"\n[{idx}/{len(test_runs)}] {test_name} - {test_date_str}")
            if description:
                logger.info(f"         {description}")
            logger.info("-" * 80)

            # Run screener for this date
            selected, sorted_stocks, details_dict = screener.screen(
                test_date,
                top_n=screener_params.get('num_stocks_to_trade', 15),
                min_score=screener_params.get('min_screener_score', 35),
                min_momentum=screener_params.get('min_momentum', 0),
                trend_days=screener_params.get('trend_days', 5)
            )

            # Display results
            if selected:
                logger.info(f"✓ Selected {len(selected)} stocks\n")

                if settings.get('show_details', True):
                    # Show top N
                    logger.info(f"{'RANK':<6} {'SYMBOL':<8} {'SCORE':<8} {'GAP':<8} {'5D RTN':<10} {'VOL':<8} {'PRICE':<10}")
                    logger.info("-" * 80)

                    for rank, symbol in enumerate(selected, 1):
                        details = details_dict[symbol]
                        score = sorted_stocks[[s[0] for s in sorted_stocks].index(symbol)][1]
                        logger.info(
                            f"{rank:<6} {symbol:<8} {score:<8.1f} "
                            f"{details['gap_pct']:<8.1f}% "
                            f"{details['momentum_pct']:<10.1f}% "
                            f"{details['volume_ratio']:<8.2f}x "
                            f"${details['price']:<9.2f}"
                        )

                    # Show bottom N stocks (lowest scores that didn't make it)
                    show_bottom = settings.get('show_bottom_n', 0)
                    if show_bottom > 0:
                        logger.info("\nBottom scorers (didn't qualify):")
                        logger.info("-" * 80)
                        bottom_stocks = sorted_stocks[-show_bottom:]
                        for sym, score in bottom_stocks:
                            if score > 0:
                                details = details_dict[sym]
                                logger.info(
                                    f"  {sym:<8} {score:<8.1f} "
                                    f"{details['gap_pct']:<8.1f}% "
                                    f"{details['momentum_pct']:<10.1f}%"
                                )

                # Store results
                test_result = {
                    'test_date': test_date_str,
                    'test_name': test_name,
                    'num_selected': len(selected),
                    'selected_stocks': ', '.join(selected),
                }
                all_results.append(test_result)

            else:
                logger.warning(f"✗ No stocks met minimum criteria (score >= {screener_params.get('min_screener_score', 35)})")
                all_results.append({
                    'test_date': test_date_str,
                    'test_name': test_name,
                    'num_selected': 0,
                    'selected_stocks': 'NONE',
                })

        except Exception as e:
            logger.error(f"Error running test for {test_date_str}: {e}", exc_info=True)
            all_results.append({
                'test_date': test_date_str,
                'test_name': test_name,
                'num_selected': 'ERROR',
                'selected_stocks': str(e),
            })

    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("BACKTEST SUMMARY")
    logger.info("=" * 80)

    for result in all_results:
        logger.info(
            f"{result['test_date']} | {result['test_name']:<25} | "
            f"Selected: {result['num_selected']}"
        )

    # Save results if requested
    if settings.get('save_results', False) and all_results:
        results_file = settings.get('results_file', 'screener_backtest_results.csv')
        df_results = pd.DataFrame(all_results)
        df_results.to_csv(results_file, index=False)
        logger.info(f"\n✓ Results saved to {results_file}")

    logger.info("=" * 80 + "\n")


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "screener_test_config.yaml"
    logger.info(f"Using config: {config_file}\n")

    try:
        run_backtest(config_file)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
