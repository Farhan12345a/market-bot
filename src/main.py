#!/usr/bin/env python3
"""
Market opening trading bot - Paper trading version

Watches for volume spike + price above open signals at 9:35 ET.
Uses trailing stop with scale-out exits.

Setup:
  1. export APCA_API_KEY_ID="your_key"
  2. export APCA_API_SECRET_KEY="your_secret"
  3. python main.py
"""

import os
import sys
import yaml
import logging
import time
from datetime import datetime
import pytz

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.broker.alpaca_broker import AlpacaBroker
from src.strategy.strategy import Strategy
from src.data.market_data import MarketDataManager
from src.executor.executor import Executor
from src.screener.stock_screener import StockScreener
from src.notifications.email_notifier import EmailNotifier

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/trading.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

def load_config(config_file="config.yaml"):
    """Load config from YAML"""
    with open(config_file, "r") as f:
        return yaml.safe_load(f)

def main():
    """Main trading loop"""
    try:
        # Load config
        config = load_config()
        logger.info("Config loaded")

        # Initialize broker
        broker = AlpacaBroker(paper=config["broker"]["paper_trading"])
        account = broker.get_account()
        logger.info(f"Connected to broker. Cash: ${account.cash}")

        # Initialize components
        market_data = MarketDataManager(broker)
        strategy = Strategy(config)
        executor = Executor(broker, config)
        email_notifier = EmailNotifier(config)

        logger.info("Paper trading bot started")
        logger.info(f"Trading hours: 9:30 AM - 4:00 PM ET")
        logger.info(f"Entry check at: 9:35 AM ET")

        et = pytz.timezone("America/New_York")

        # Use screener or static list
        if config["trading"].get("use_daily_screener", False):
            screener = StockScreener(broker, config["trading"]["candidates_file"])
        else:
            screener = None

        symbols = None  # Will be loaded at entry time

        # Main loop - wait for market to open, then run
        while True:
            now = datetime.now(et)

            # Check if market is open
            if not market_data.is_market_open():
                # Calculate sleep time until market open
                sleep_seconds = 60
                logger.debug(f"Market closed. Sleeping {sleep_seconds}s...")
                time.sleep(sleep_seconds)
                continue

            logger.info("Market is open, monitoring for signals...")

            # Entry phase: wait until 9:35 ET
            screener_run = False
            while not market_data.is_entry_time():
                time.sleep(10)
                now = datetime.now(et)

                # Run screener 5 minutes before entry (at 9:30)
                if (not screener_run and screener is not None and
                    now.hour == 9 and now.minute == 30):
                    logger.info("===== PRE-MARKET SCREENER (9:30 ET) =====")
                    symbols = screener.screen(
                        top_n=config["trading"]["num_stocks_to_trade"],
                        min_score=config["trading"]["min_screener_score"]
                    )
                    screener_run = True

                if now.hour >= 16:  # Market closed
                    logger.info("Market closed, exiting")
                    executor.flatten_all_positions()
                    executor.save_trades_log()
                    return

            # If no screener, use static list
            if symbols is None:
                symbols = config["trading"]["stock_universe"]

            logger.info("===== ENTRY TIME (9:35 ET) =====")

            # At 9:35, check all symbols for entry signals
            for symbol in symbols:
                try:
                    # Get avg volume first
                    avg_volume = market_data.get_20day_avg_volume(symbol)

                    # Get current bar
                    current_bar = market_data.get_open_bar(symbol)
                    if not current_bar:
                        logger.warning(f"No data for {symbol}")
                        continue

                    # Process through strategy
                    signal = strategy.process_bar(symbol, current_bar, avg_volume)

                    # Execute if we have a signal
                    if signal:
                        executor.execute_signal(signal)

                except Exception as e:
                    logger.error(f"Error processing {symbol}: {e}")
                    continue

            logger.info("Entry checks complete. Monitoring exits...")

            # Exit phase: monitor for exits throughout the day
            last_check = datetime.now(et)
            while True:
                now = datetime.now(et)

                # Check daily loss limit
                if executor.check_daily_loss_limit():
                    logger.warning("Daily loss limit hit, flattening all positions")
                    executor.flatten_all_positions()
                    executor.save_trades_log()
                    email_notifier.send_daily_summary()
                    return

                # Check for exits every minute
                if (now - last_check).seconds >= 60 or last_check == datetime.now(et):
                    last_check = now

                    for symbol in list(strategy.get_open_trades().keys()):
                        try:
                            current_bar = market_data.get_latest_bar(symbol, "1Min")
                            if not current_bar:
                                continue

                            current_price = current_bar.get("close", 0)
                            avg_volume = market_data.cache.get(
                                f"{symbol}_avg_volume", 1000000
                            )

                            # Check for exits
                            signal = strategy.process_bar(symbol, current_bar, avg_volume)
                            if signal:
                                executor.execute_signal(signal)

                        except Exception as e:
                            logger.error(f"Error checking exits for {symbol}: {e}")
                            continue

                    # Log current positions
                    positions = broker.get_positions()
                    if positions:
                        logger.info(f"Open positions: {len(positions)}")
                        for sym, pos in positions.items():
                            logger.info(
                                f"  {sym}: {pos.qty} @ {pos.current_price} "
                                f"(P&L: ${pos.unrealized_pl:.2f})"
                            )
                    else:
                        # All trades have closed - send summary and exit
                        logger.info("All trades closed. Sending daily summary...")
                        executor.save_trades_log()
                        email_notifier.send_daily_summary()
                        logger.info("Daily session complete")
                        break

                # Exit at 4:00 PM (if any positions still open)
                if now.hour >= 16:
                    logger.info("Market closing, flattening all positions...")
                    executor.flatten_all_positions()
                    executor.save_trades_log()
                    logger.info("Daily session complete")
                    email_notifier.send_daily_summary()
                    break

                # Sleep before next check
                time.sleep(30)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        executor.flatten_all_positions()
        executor.save_trades_log()
        email_notifier.send_daily_summary()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        try:
            executor.flatten_all_positions()
            executor.save_trades_log()
            email_notifier.send_daily_summary()
        except:
            pass
        raise

if __name__ == "__main__":
    main()
