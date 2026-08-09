#!/usr/bin/env python3
"""
Standalone screener test script.
Run this to test the screener on any date without needing market hours.
"""

import sys
import os
import yaml
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# Load env variables
load_dotenv()

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from src.broker.alpaca_broker import AlpacaBroker
from src.screener.stock_screener import StockScreener
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def test_screener(test_date_str=None):
    """
    Test the screener on a specific date.

    Args:
        test_date_str: Date string in format "YYYY-MM-DD" (e.g., "2024-08-07")
                      If None, uses today's date
    """
    # Load config
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    # Parse date
    if test_date_str:
        test_date = datetime.strptime(test_date_str, "%Y-%m-%d").date()
    else:
        et = pytz.timezone("America/New_York")
        test_date = datetime.now(et).date()

    logger.info(f"=" * 70)
    logger.info(f"SCREENER TEST FOR: {test_date}")
    logger.info(f"=" * 70)

    try:
        # Initialize broker (paper trading)
        logger.info("Connecting to Alpaca (paper trading)...")
        broker = AlpacaBroker(paper=True)
        account = broker.get_account()
        logger.info(f"✓ Connected. Account cash: ${account.cash:.2f}")

        # Initialize screener
        logger.info(f"Initializing screener...")
        screener = StockScreener(broker, "candidates.txt")
        logger.info(f"✓ Loaded {len(screener.candidates)} candidates")

        # Run screener
        logger.info(f"\nRunning screener for {test_date}...")
        logger.info("-" * 70)

        selected_stocks = screener.screen(
            top_n=config["trading"]["num_stocks_to_trade"],
            min_score=config["trading"]["min_screener_score"]
        )

        logger.info("-" * 70)

        # Display results
        if selected_stocks:
            logger.info(f"\n✓ SCREENER PASSED - Selected {len(selected_stocks)} stocks")
            logger.info(f"\nStocks selected for trading:")
            for i, symbol in enumerate(selected_stocks, 1):
                logger.info(f"  {i:2d}. {symbol}")

            logger.info(f"\nThese stocks would be monitored at 9:35 AM ET for entry signals.")
        else:
            logger.warning(f"\n✗ SCREENER FAILED - No stocks met minimum criteria")

        logger.info(f"\n" + "=" * 70)

    except Exception as e:
        logger.error(f"Error running screener: {e}", exc_info=True)
        return False

    return True


if __name__ == "__main__":
    # Check for date argument
    if len(sys.argv) > 1:
        test_date = sys.argv[1]
        logger.info(f"Using provided date: {test_date}")
        success = test_screener(test_date)
    else:
        logger.info("No date provided, using today")
        success = test_screener()

    sys.exit(0 if success else 1)
