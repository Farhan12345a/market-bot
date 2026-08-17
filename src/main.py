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
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
import pytz

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.broker.alpaca_broker import AlpacaBroker
from src.strategy.strategy import Strategy, TradeManager
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

def select_symbols(config, screener, market_data):
    """
    Run the daily screener (if enabled) and return the symbol list to trade,
    ALWAYS merged with the static stock_universe default list - the default
    list is watched every day no matter what the screener does (previously
    this was either/or: screener picks OR the fallback list, never both).
    The screener's picks (if it ran and found any) are added on top of the
    defaults, deduplicated.

    Also computes RSI(rsi_period) once per symbol here, at market-open, and
    returns it alongside the symbol list. This is NOT used to filter which
    symbols get watched - a daily-bar RSI barely moves within a single
    trading day, so a once-at-open computation is exactly as fresh as any
    later recomputation would be. Instead it's checked in run_entry_window as
    one half of the combined entry signal: a rapid price increase AND
    RSI < rsi_max_for_entry at the same time is what actually triggers a buy,
    so every symbol still gets watched for the price signal regardless of its
    RSI, but only enters when both conditions line up together.

    The timeout runs the screener in a background thread and simply stops
    waiting after screener_timeout_seconds, rather than trying to interrupt it
    with a signal. A signal-based interrupt was tried first and doesn't work
    here: get_historical_bars() (and every scoring helper in stock_screener.py)
    wraps its body in a broad `except Exception`, which would silently catch
    and swallow a signal-raised TimeoutError before it could ever reach this
    function - and since signal.alarm() only fires once, that single swallowed
    exception would fully disable the timeout for the rest of the run.
    Abandoning a background thread instead sidesteps that entirely: this
    function just stops waiting, regardless of what the thread does internally.
    """
    screener_ran = False
    screener_timed_out = False
    symbols = []

    if config["trading"].get("use_daily_screener", False) and screener is not None:
        logger.info("===== PRE-MARKET SCREENER =====")
        screener_ran = True
        timeout_seconds = config["trading"].get("screener_timeout_seconds", 420)

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            screener.screen,
            top_n=config["trading"]["num_stocks_to_trade"],
            min_score=config["trading"]["min_screener_score"],
        )
        try:
            symbols = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
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
            # Don't block waiting for an abandoned/still-running screener thread -
            # we've already moved on; let it finish (or not) in the background.
            executor.shutdown(wait=False)

    candidates_evaluated = len(screener.candidates) if screener is not None else 0

    if not symbols and screener_ran and not screener_timed_out:
        logger.warning("Screener produced no candidates")

    default_list = config["trading"]["stock_universe"]
    merged = list(dict.fromkeys(symbols + default_list))  # screener picks first, then defaults, deduped
    if len(merged) != len(symbols):
        logger.info(
            f"Merged screener picks with the default stock_universe list: "
            f"{len(symbols)} screener + {len(default_list)} default -> {len(merged)} total watched"
        )
    symbols = merged

    logger.info(
        f"Symbol selection: screener_ran={screener_ran}, "
        f"candidates_evaluated={candidates_evaluated}, "
        f"symbols_selected={len(symbols)} ({', '.join(symbols)})"
    )

    rsi_values = {}
    if config["trading"].get("use_rsi_filter", False):
        rsi_period = config["trading"].get("rsi_period", 14)
        rsi_values = {symbol: market_data.get_rsi(symbol, period=rsi_period) for symbol in symbols}
        logger.info(
            "RSI(%d) at market open: %s",
            rsi_period,
            ", ".join(
                f"{s}={rsi_values[s]:.1f}" if rsi_values[s] is not None else f"{s}=N/A"
                for s in symbols
            ),
        )

    return symbols, rsi_values

def run_entry_window(config, market_data, strategy, executor, symbols, rsi_values, et):
    """
    Watch `symbols` from entry_window_start to entry_window_end for a rapid
    price rise (>= rapid_increase_pct within a trailing rapid_increase_lookback_minutes
    window). If use_rsi_filter is on, also requires RSI (from rsi_values,
    computed once at market open by select_symbols) below rsi_max_for_entry -
    a rapid increase on an already-overbought symbol is logged but not bought.

    If use_pullback_entry is on (the default): a detected rapid increase does
    NOT buy immediately. Instead it starts tracking that symbol for a pullback
    off the post-thrust peak followed by a resumption higher, and only buys on
    the resumption - aiming for a better average entry than buying directly
    into the top of the initial spike. See _advance_pullback_state for the
    state machine. If off, falls back to the original buy-on-detection behavior.
    """
    entry_start = parse_hhmm_today(config["trading"]["entry_window_start"], et)
    entry_end = parse_hhmm_today(config["trading"]["entry_window_end"], et)
    check_interval = config["trading"]["entry_check_interval_seconds"]
    lookback = timedelta(minutes=config["trading"]["rapid_increase_lookback_minutes"])
    use_rsi_filter = config["trading"].get("use_rsi_filter", False)
    rsi_max = config["trading"].get("rsi_max_for_entry", 50)
    use_pullback_entry = config["trading"].get("use_pullback_entry", False)

    now = datetime.now(et)
    while now < entry_start:
        time.sleep(min(5, (entry_start - now).total_seconds()))
        now = datetime.now(et)

    logger.info(
        f"===== ENTRY WINDOW: {entry_start.strftime('%H:%M')} - "
        f"{entry_end.strftime('%H:%M')} ET ====="
    )

    price_history = {symbol: deque() for symbol in symbols}
    pending_pullbacks = {}  # symbol -> state dict, only used when use_pullback_entry is on
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

                # Drop samples older than the lookback window, measured from the latest
                # BAR's own timestamp (not wall-clock `now`) - the feed can lag wall-clock
                # by a few minutes, and anchoring to `now` would evict every sample before
                # two ever accumulate whenever that lag exceeds `lookback`.
                cutoff = ts - lookback
                while history and history[0][0] < cutoff:
                    history.popleft()

                symbol_rsi = rsi_values.get(symbol)
                if use_rsi_filter and (symbol_rsi is None or symbol_rsi >= rsi_max):
                    continue  # doesn't qualify on RSI at all - don't bother tracking it

                if use_pullback_entry and symbol in pending_pullbacks:
                    _advance_pullback_state(
                        config, strategy, executor, symbol, price,
                        pending_pullbacks, symbol_rsi,
                    )
                    if symbol in strategy.get_open_trades():
                        entries_triggered += 1
                        del pending_pullbacks[symbol]
                    continue

                if len(history) < 2:
                    continue

                price_then = history[0][1]
                qty, pct_change = strategy.check_rapid_increase_entry(symbol, price, price_then)

                if qty > 0:
                    if use_pullback_entry:
                        pending_pullbacks[symbol] = {
                            "qty": qty,
                            "base_price": price_then,
                            "thrust_price": price,
                            "peak": price,
                            "pullback_low": None,
                            "pct_change": pct_change,
                        }
                        logger.info(
                            f"{symbol}: RAPID INCREASE detected (+{pct_change:.2f}% over "
                            f"{lookback.total_seconds()/60:.0f}min) - watching for a pullback "
                            f"and resumption before entering"
                        )
                        continue

                    signal = strategy.enter_trade(symbol, price, qty)
                    if signal:
                        rsi_note = f", RSI={symbol_rsi:.1f}" if symbol_rsi is not None else ""
                        logger.info(
                            f"{symbol}: RAPID INCREASE entry triggered - "
                            f"+{pct_change:.2f}% over {lookback.total_seconds()/60:.0f}min "
                            f"(threshold {config['trading']['rapid_increase_pct']}%){rsi_note}"
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

def _advance_pullback_state(config, strategy, executor, symbol, price, pending_pullbacks, symbol_rsi):
    """
    Advance one symbol's pullback-entry state machine by one price sample.
    Only called (from run_entry_window) once a rapid-increase thrust has
    already been detected for `symbol` and it hasn't been bought yet.

    Resumption is checked BEFORE peak-tracking/invalidation: if price has
    bounced far enough off a tracked pullback low, that's a buy regardless of
    whether it also happens to exceed the prior peak - checking peak-update
    first would mean a resumption strong enough to blow straight past the old
    peak in one tick could skip the buy entirely, which defeats the point.
    """
    setup = pending_pullbacks[symbol]
    min_pullback_pct = config["trading"].get("pullback_min_pct", 0.1) / 100
    max_giveback_fraction = config["trading"].get("pullback_max_giveback_fraction", 0.75)
    resumption_confirm_pct = config["trading"].get("resumption_confirm_pct", 0.05) / 100

    if setup["pullback_low"] is not None:
        if price < setup["pullback_low"]:
            setup["pullback_low"] = price
        else:
            bounce_pct = (price - setup["pullback_low"]) / setup["pullback_low"]
            if bounce_pct >= resumption_confirm_pct:
                qty = int(config["trading"]["max_position_per_stock_usd"] / price)
                if qty <= 0:
                    del pending_pullbacks[symbol]
                    return
                signal = strategy.enter_trade(symbol, price, qty)
                if signal:
                    rsi_note = f", RSI={symbol_rsi:.1f}" if symbol_rsi is not None else ""
                    logger.info(
                        f"{symbol}: PULLBACK RESUMPTION entry triggered - thrust "
                        f"+{setup['pct_change']:.2f}%, peak {setup['peak']:.2f}, pulled back to "
                        f"{setup['pullback_low']:.2f}, resumed to {price:.2f}{rsi_note}"
                    )
                    executor.execute_signal(signal)
                return

    if price > setup["peak"]:
        setup["peak"] = price
        setup["pullback_low"] = None
        return

    thrust_gain = setup["peak"] - setup["base_price"]
    if thrust_gain <= 0:
        del pending_pullbacks[symbol]
        return

    giveback = setup["peak"] - price
    if giveback / thrust_gain >= max_giveback_fraction:
        logger.info(
            f"{symbol}: pullback gave back {giveback / thrust_gain * 100:.0f}% of the thrust's "
            f"gain (peak {setup['peak']:.2f} -> {price:.2f}) - setup invalidated"
        )
        del pending_pullbacks[symbol]
        return

    if setup["pullback_low"] is None:
        retracement_pct = giveback / setup["peak"]
        if retracement_pct >= min_pullback_pct:
            setup["pullback_low"] = price

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

def reconcile_existing_positions(broker, strategy, executor):
    """
    Adopt any positions that already exist in the broker account but aren't
    tracked in strategy.trades. This happens whenever the process restarts
    (e.g. after the hosting environment reclaims the container) while
    positions are still open - a fresh Strategy() starts with empty in-memory
    tracking, so without this, the tiered stop-loss/trailing-stop logic in
    Strategy.process_bar never runs again for those positions until they're
    closed or the 4pm time-stop flatten sweeps everything.

    Uses the broker's own avg_entry_price, mirroring the same fallback
    executor.flatten_all_positions() already uses for positions it didn't
    itself open. The trailing-stop high-water mark is seeded with
    max(entry_price, current_price) rather than entry_price alone - a partial
    correction for not knowing the position's true peak since its original
    entry, since the broker doesn't expose intraday high-since-entry.

    Also seeds executor.open_entries[symbol] with the same entry price -
    Executor.submit_exit_order() reads P&L from that dict (populated
    separately from strategy.trades, only on a normal submit_entry_order()
    call), so without this a reconciled position's eventual exit would log
    an accurate STOP DECISION but a wrong "entry was None, P&L: $0.00" record.
    """
    try:
        positions = broker.get_positions()
    except Exception as e:
        logger.error(f"Error reconciling existing positions: {e}")
        return

    for symbol, position in positions.items():
        if symbol in strategy.trades:
            continue
        try:
            qty = int(abs(float(position.qty)))
            if qty <= 0:
                continue

            avg_entry = getattr(position, "avg_entry_price", None)
            current_price_attr = getattr(position, "current_price", None)
            current_price = float(current_price_attr) if current_price_attr else None
            entry_price = float(avg_entry) if avg_entry else current_price

            if not entry_price or entry_price <= 0:
                logger.warning(
                    f"{symbol}: found an open position on startup but couldn't "
                    f"determine an entry price - leaving unmanaged, will still be "
                    f"caught by the time-stop/daily-loss-limit safety nets"
                )
                continue

            trade = TradeManager(symbol, entry_price, qty, strategy.config)
            if current_price and current_price > trade.highest_price:
                trade.highest_price = current_price
                trade.highest_since_entry = current_price
                trade.price_history = [entry_price, current_price]

            strategy.trades[symbol] = trade
            executor.open_entries[symbol] = entry_price
            logger.info(
                f"{symbol}: adopted pre-existing position on startup - {qty} shares "
                f"@ {entry_price:.2f} (broker avg_entry_price) - resuming stop-loss/"
                f"trailing-stop management"
            )
        except Exception as e:
            logger.error(f"Error reconciling position for {symbol}: {e}")

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
        reconcile_existing_positions(broker, strategy, executor)
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

            symbols, rsi_values = select_symbols(config, screener, market_data)
            entries_triggered = run_entry_window(
                config, market_data, strategy, executor, symbols, rsi_values, et
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
