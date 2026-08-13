#!/usr/bin/env python3
"""
Market opening trading bot - Paper trading version

Watches screened candidates for a rapid price rise during the entry window,
then manages exits with trailing stop + scale-out logic for the rest of the day.

Setup:
  1. export APCA_API_KEY_ID="your_key"
  2. export APCA_API_SECRET_KEY="your_secret"
  3. python main.py
"""

import os
import sys
import yaml
import logging
import signal
import time
from collections import deque
from datetime import datetime, timedelta
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

def parse_hhmm_today(hhmm_str, et_tz):
    """Turn a config 'HH:MM' string into today's datetime in the given tz"""
    hour, minute = map(int, hhmm_str.split(":"))
    return datetime.now(et_tz).replace(hour=hour, minute=minute, second=0, microsecond=0)

def _alarm_handler(signum, frame):
    raise TimeoutError("Screener timed out")

def select_symbols(config, screener):
    """
    Run the daily screener (if enabled) and return the symbol list to trade.
    ALWAYS falls back to the static stock_universe list if the screener is
    disabled, errors, returns zero candidates, or doesn't finish within
    screener_timeout_seconds - the bot never runs with an empty symbol list,
    and never lets a slow screener eat into the entry window.
    """
    screener_ran = False
    screener_timed_out = False
    symbols = []

    if config["trading"].get("use_daily_screener", False) and screener is not None:
        logger.info("===== PRE-MARKET SCREENER =====")
        screener_ran = True
        timeout_seconds = config["trading"].get("screener_timeout_seconds", 420)

        previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(timeout_seconds)
        try:
            symbols = screener.screen(
                top_n=config["trading"]["num_stocks_to_trade"],
                min_score=config["trading"]["min_screener_score"],
            )
        except TimeoutError:
            screener_timed_out = True
            logger.warning(
                f"Screener did not finish within {timeout_seconds}s - "
                f"aborting and falling back to static stock_universe list"
            )
            symbols = []
        except Exception as e:
            logger.error(f"Screener failed: {e}")
            symbols = []
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

    candidates_evaluated = len(screener.candidates) if screener is not None else 0

    if not symbols:
        if screener_ran and not screener_timed_out:
            logger.warning(
                "Screener produced no candidates - falling back to static stock_universe list"
            )
        symbols = config["trading"]["stock_universe"]

    logger.info(
        f"Symbol selection: screener_ran={screener_ran}, "
        f"candidates_evaluated={candidates_evaluated}, "
        f"symbols_selected={len(symbols)} ({', '.join(symbols)})"
    )

    return symbols

def run_entry_window(config, market_data, strategy, executor, symbols, et):
    """
    Watch `symbols` from entry_window_start to entry_window_end for a rapid
    price rise (>= rapid_increase_pct within a trailing rapid_increase_lookback_minutes
    window) and buy on the first qualifying sample per symbol.
    """
    entry_start = parse_hhmm_today(config["trading"]["entry_window_start"], et)
    entry_end = parse_hhmm_today(config["trading"]["entry_window_end"], et)
    check_interval = config["trading"]["entry_check_interval_seconds"]
    lookback = timedelta(minutes=config["trading"]["rapid_increase_lookback_minutes"])

    now = datetime.now(et)
    while now < entry_start:
        time.sleep(min(5, (entry_start - now).total_seconds()))
        now = datetime.now(et)

    logger.info(
        f"===== ENTRY WINDOW: {entry_start.strftime('%H:%M')} - "
        f"{entry_end.strftime('%H:%M')} ET ====="
    )

    price_history = {symbol: deque() for symbol in symbols}
    entries_triggered = 0

    while datetime.now(et) < entry_end:
        now = datetime.now(et)

        for symbol in symbols:
            if symbol in strategy.get_open_trades():
                continue

            try:
                bar = market_data.get_latest_bar(symbol, "1Min")
                if not bar:
                    continue

                price = bar.get("close", 0)
                ts = bar.get("timestamp", now)
                history = price_history[symbol]
                history.append((ts, price))

                # Drop samples older than the lookback window
                cutoff = now - lookback
                while history and history[0][0] < cutoff:
                    history.popleft()

                if len(history) < 2:
                    continue

                price_then = history[0][1]
                qty, pct_change = strategy.check_rapid_increase_entry(symbol, price, price_then)

                if qty > 0:
                    signal = strategy.enter_trade(symbol, price, qty)
                    if signal:
                        logger.info(
                            f"{symbol}: RAPID INCREASE entry triggered - "
                            f"+{pct_change:.2f}% over {lookback.total_seconds()/60:.0f}min "
                            f"(threshold {config['trading']['rapid_increase_pct']}%)"
                        )
                        executor.execute_signal(signal)
                        entries_triggered += 1

            except Exception as e:
                logger.error(f"Error checking entry for {symbol}: {e}")
                continue

        time.sleep(check_interval)

    logger.info(
        f"Entry window closed. symbols_monitored={len(symbols)}, "
        f"entries_triggered={entries_triggered}"
    )
    return entries_triggered

def run_exit_monitoring(config, market_data, strategy, executor, email_notifier, entries_triggered, et):
    """
    Monitor open positions for exits until either all positions have closed
    (only meaningful if we actually entered something today) or the 4 PM
    time stop is hit.
    """
    time_stop_hour = config["trading"]["time_stop_hour"]
    last_check = datetime.now(et)

    while True:
        now = datetime.now(et)

        if executor.check_daily_loss_limit():
            logger.warning("Daily loss limit hit, flattening all positions")
            executor.flatten_all_positions()
            executor.save_trades_log()
            email_notifier.send_daily_summary()
            return

        if (now - last_check).seconds >= 60 or last_check == now:
            last_check = now

            for symbol in list(strategy.get_open_trades().keys()):
                try:
                    current_bar = market_data.get_latest_bar(symbol, "1Min")
                    if not current_bar:
                        continue

                    signal = strategy.process_bar(symbol, current_bar)
                    if signal:
                        executor.execute_signal(signal)

                except Exception as e:
                    logger.error(f"Error checking exits for {symbol}: {e}")
                    continue

            open_trades = strategy.get_open_trades()
            if open_trades:
                logger.info(f"Open positions: {len(open_trades)}")
            elif entries_triggered > 0:
                # We entered at least one trade today and everything is now closed.
                logger.info("All trades closed. Sending daily summary...")
                executor.save_trades_log()
                email_notifier.send_daily_summary()
                logger.info(
                    f"Daily session complete: entries_triggered={entries_triggered}, "
                    f"positions_open=0"
                )
                return
            # else: zero trades were ever entered today - keep idling until the time stop,
            # this is NOT "all trades closed", there was simply nothing to close.

        if now.hour >= time_stop_hour:
            logger.info("Market closing, flattening all positions...")
            executor.flatten_all_positions()
            executor.save_trades_log()
            email_notifier.send_daily_summary()
            logger.info(
                f"Daily session complete: entries_triggered={entries_triggered}, "
                f"reason=time_stop"
            )
            return

        time.sleep(30)

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
        logger.info(f"Trading hours: 9:30 AM - {config['trading']['time_stop_hour']}:00 ET")
        logger.info(
            f"Entry window: {config['trading']['entry_window_start']} - "
            f"{config['trading']['entry_window_end']} ET"
        )

        et = pytz.timezone("America/New_York")

        if config["trading"].get("use_daily_screener", False):
            screener = StockScreener(broker, config["trading"]["candidates_file"])
        else:
            screener = None

        # Main loop - wait for market to open, run one full session, repeat next day
        while True:
            now = datetime.now(et)

            if not market_data.is_market_open():
                time.sleep(60)
                continue

            logger.info("Market is open, monitoring for signals...")

            symbols = select_symbols(config, screener)
            entries_triggered = run_entry_window(
                config, market_data, strategy, executor, symbols, et
            )
            run_exit_monitoring(
                config, market_data, strategy, executor, email_notifier, entries_triggered, et
            )

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
