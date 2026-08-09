#!/usr/bin/env python3
"""
Screener test with REAL Alpaca data using alpaca-trade-api
"""

import sys
import os
import pandas as pd
from datetime import datetime, timedelta
import pytz
import logging
from dotenv import load_dotenv

# Load env variables
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class RealAlpacaBroker:
    """Broker using alpaca-trade-api library (installed version)"""

    def __init__(self):
        from alpaca_trade_api import REST

        api_key = os.getenv("APCA_API_KEY_ID")
        api_secret = os.getenv("APCA_API_SECRET_KEY")

        if not api_key or not api_secret:
            raise ValueError("APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set")

        self.api = REST(api_key, api_secret, base_url="https://paper-api.alpaca.markets")
        logger.info("✓ Connected to Alpaca API (paper trading)")

    def get_historical_bars(self, symbols, start, end, timeframe="1Day"):
        """Fetch real historical bars from Alpaca"""
        if isinstance(symbols, str):
            symbols = [symbols]

        result = {}
        for symbol in symbols:
            try:
                logger.debug(f"Fetching {symbol} from {start} to {end}")

                # Alpaca REST API method for bars
                bars = self.api.get_barset(
                    symbols=symbol,
                    timeframe=timeframe,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    limit=1000
                )

                if symbol in bars and bars[symbol]:
                    df_data = []
                    for bar in bars[symbol]:
                        df_data.append({
                            'timestamp': bar.t,
                            'open': bar.o,
                            'high': bar.h,
                            'low': bar.l,
                            'close': bar.c,
                            'volume': bar.v,
                        })

                    df = pd.DataFrame(df_data)
                    if not df.empty:
                        result[symbol] = df
                    else:
                        logger.warning(f"No data for {symbol}")
                else:
                    logger.warning(f"No bars returned for {symbol}")

            except Exception as e:
                logger.error(f"Error fetching {symbol}: {e}")
                continue

        return result


class StockScreener:
    """Stock screener with real data"""

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

    def score_stock(self, symbol, test_date, min_momentum=0, trend_days=5):
        """Score a stock using real data up to test_date"""
        score = 0
        details = {"symbol": symbol}

        try:
            end_date = test_date
            start_date = test_date - timedelta(days=30)

            bars = self.broker.get_historical_bars(symbol, start_date, end_date, "1Day")

            if symbol not in bars or bars[symbol].empty:
                return 0, details

            df = bars[symbol].sort_values('timestamp')

            if len(df) < 2:
                return 0, details

            # === GAP ANALYSIS ===
            yesterday_close = df.iloc[-2]['close']
            today_open = df.iloc[-1]['open']
            gap = abs((today_open - yesterday_close) / yesterday_close * 100)

            gap_score = min(25, gap * 5)
            score += gap_score
            details['gap_pct'] = round(gap, 2)
            details['gap_score'] = round(gap_score, 1)

            # === MOMENTUM (5-day return) ===
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
            recent_vol = df.iloc[-1]['volume']
            avg_vol = df.tail(20)['volume'].mean() if len(df) >= 20 else df['volume'].mean()
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

            atr_pct = (df['tr'].tail(14).mean() / df.iloc[-1]['close']) * 100 if len(df) >= 14 else 0
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

            # === BULLISH TREND FILTER ===
            trend_score = 0
            if min_momentum > -999:
                lookback_idx = min(trend_days, len(df) - 1)
                price_lookback = df.iloc[-lookback_idx - 1]['close']
                latest_price = df.iloc[-1]['close']
                recent_trend = ((latest_price - price_lookback) / price_lookback) * 100

                if recent_trend < min_momentum:
                    details['trend_pct'] = round(recent_trend, 2)
                    details['status'] = 'REJECTED_BEARISH_TREND'
                    return 0, details

                if recent_trend > 5:
                    trend_score = 15
                elif recent_trend > 2:
                    trend_score = 10
                else:
                    trend_score = 5

                details['trend_pct'] = round(recent_trend, 2)
                details['trend_score'] = trend_score

            score += trend_score
            details['price'] = round(float(df.iloc[-1]['close']), 2)
            details['date'] = df.iloc[-1]['timestamp']

            return score, details

        except Exception as e:
            logger.error(f"Error scoring {symbol}: {e}")
            return 0, details

    def screen(self, test_date, top_n=15, min_score=35, min_momentum=0, trend_days=5):
        """Run screener for a specific date"""
        if not self.candidates:
            logger.error("No candidates loaded")
            return []

        logger.info(f"Screening {len(self.candidates)} stocks with REAL data...")
        scores = {}
        details_dict = {}

        for i, symbol in enumerate(self.candidates):
            if i % 10 == 0 and i > 0:
                logger.info(f"  Progress: {i}/{len(self.candidates)}")

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

        sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        selected = [sym for sym, score in sorted_stocks if score >= min_score][:top_n]

        return selected, sorted_stocks, details_dict


def run_real_data_test(test_date_str):
    """Run screener with real Alpaca data"""
    test_date = datetime.strptime(test_date_str, "%Y-%m-%d").date()

    logger.info("=" * 80)
    logger.info(f"REAL DATA SCREENER TEST - {test_date}")
    logger.info("=" * 80)

    try:
        # Connect to real Alpaca
        broker = RealAlpacaBroker()

        # Initialize screener
        screener = StockScreener(broker, "candidates.txt")
        logger.info(f"Loaded {len(screener.candidates)} candidates\n")

        # Run screener
        logger.info("Running screener with REAL market data...")
        selected, sorted_stocks, details_dict = screener.screen(test_date)

        if selected:
            logger.info(f"\n✓ Selected {len(selected)} stocks\n")
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
        else:
            logger.warning("No stocks met criteria")

        logger.info("=" * 80)
        return True

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_date = sys.argv[1]
        logger.info(f"Testing with date: {test_date}\n")
        success = run_real_data_test(test_date)
    else:
        logger.error("Please provide a date: python test_screener_real_data.py 2026-08-03")
        sys.exit(1)

    sys.exit(0 if success else 1)
