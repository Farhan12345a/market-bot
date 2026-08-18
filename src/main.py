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
import csv
from pathlib import Path
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

    If use_three_bar_momentum is on: a separate, faster signal checked ahead of
    everything else above. The instant 3 consecutive 1-minute bars are all
    green (close > open) with each bar's close higher than the previous bar's
    close, it buys immediately - no waiting for rapid_increase_pct's %
    threshold to accumulate, and no pullback-wait either. This exists because
    the %-over-lookback-window and pullback-wait signals are both deliberately
    patient, and on a stock opening with a hard, clean thrust that patience
    costs a meaningfully worse entry price. Same exit/stop-loss handling
    applies once entered - this only changes how fast the buy fires.
    """
    entry_start = parse_hhmm_today(config["trading"]["entry_window_start"], et)
    entry_end = parse_hhmm_today(config["trading"]["entry_window_end"], et)
    check_interval = config["trading"]["entry_check_interval_seconds"]
    lookback = timedelta(minutes=config["trading"]["rapid_increase_lookback_minutes"])
    use_rsi_filter = config["trading"].get("use_rsi_filter", False)
    rsi_max = config["trading"].get("rsi_max_for_entry", 50)
    use_pullback_entry = config["trading"].get("use_pullback_entry", False)
    use_three_bar_momentum = config["trading"].get("use_three_bar_momentum", False)

    now = datetime.now(et)
    while now < entry_start:
        time.sleep(min(5, (entry_start - now).total_seconds()))
        now = datetime.now(et)

    logger.info(
        f"===== ENTRY WINDOW: {entry_start.strftime('%H:%M')} - "
        f"{entry_end.strftime('%H:%M')} ET ====="
    )

    price_history = {symbol: deque() for symbol in symbols}
    bar_history = {symbol: deque(maxlen=3) for symbol in symbols}  # last 3 1min bars, for use_three_bar_momentum
    pending_pullbacks = {}  # symbol -> state dict, only used when use_pullback_entry is on
    symbol_price_log = {symbol: [] for symbol in symbols}  # full (untrimmed) history, for _write_price_log
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
                bar_history[symbol].append(bar)
                symbol_price_log[symbol].append((ts, price))

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

                if use_three_bar_momentum and _check_three_bar_momentum(bar_history[symbol]):
                    qty = int(config["trading"]["max_position_per_stock_usd"] / price)
                    signal = strategy.enter_trade(symbol, price, qty)
                    if signal:
                        rsi_note = f", RSI={symbol_rsi:.1f}" if symbol_rsi is not None else ""
                        closes = [b.get("close", 0) for b in bar_history[symbol]]
                        logger.info(
                            f"{symbol}: THREE-BAR MOMENTUM entry triggered - 3 consecutive "
                            f"green 1min bars, closes {closes[0]:.2f} -> {closes[1]:.2f} -> "
                            f"{closes[2]:.2f}{rsi_note}"
                        )
                        signal["entry_method"] = "THREE_BAR_MOMENTUM"
                        signal["entry_rsi"] = symbol_rsi
                        executor.execute_signal(signal)
                        entries_triggered += 1
                        pending_pullbacks.pop(symbol, None)
                    continue

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
                        signal["entry_method"] = "RAPID_INCREASE_IMMEDIATE"
                        signal["entry_rsi"] = symbol_rsi
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
    _write_price_log(symbol_price_log, et)
    return entries_triggered

def _write_price_log(symbol_price_log, et):
    """
    Dump this entry window's minute-by-minute price samples for every
    watched symbol (screener picks + default watchlist) to a dedicated,
    per-day log file - one block per symbol, chronological within each -
    so the actual price action behind a day's entries (or non-entries) can
    be eyeballed directly instead of dug out of trading.log. Separate file,
    doesn't touch trading.log.
    """
    date_str = datetime.now(et).strftime("%Y-%m-%d")
    path = os.path.join("logs", f"price_log_{date_str}.txt")
    try:
        os.makedirs("logs", exist_ok=True)
        with open(path, "a") as f:
            f.write(f"\n===== entry window price log - written {datetime.now(et)} =====\n")
            for symbol, samples in symbol_price_log.items():
                for ts, price in samples:
                    f.write(f"{symbol} {ts} {price}\n")
        logger.info(f"Wrote entry-window price log to {path}")
    except Exception as e:
        logger.error(f"Error writing price log: {e}")

def _check_three_bar_momentum(bars):
    """
    True if `bars` holds exactly 3 1-minute bars, all green (close > open),
    with each bar's close strictly higher than the previous bar's close -
    i.e. a clean 3-bar upward thrust, not just 3 green bars chopping sideways.
    """
    if len(bars) < 3:
        return False
    bars = list(bars)
    if any(b.get("close", 0) <= b.get("open", 0) for b in bars):
        return False
    return all(curr.get("close", 0) > prev.get("close", 0) for prev, curr in zip(bars, bars[1:]))

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
                    signal["entry_method"] = "PULLBACK_RESUMPTION"
                    signal["entry_rsi"] = symbol_rsi
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

def run_exit_monitoring(config, market_data, strategy, executor, email_notifier, entries_triggered, et, symbols=None):
    """
    Monitor open positions for exits until either all positions have closed
    (only meaningful if we actually entered something today) or the 4 PM
    time stop is hit.

    "Did this session ever have trades" is entries_triggered (bought fresh
    this run) OR there being open trades already at the moment this function
    starts (adopted via reconcile_existing_positions on a restart mid-day,
    after the entry window already closed - entries_triggered would be 0 for
    that run even though real positions are open and being managed). Without
    the second half, a restart-recovered position closing out would never be
    recognized as "all trades closed" and the trades.json/email summary
    would only ever fire off the blunt 4pm time-stop instead.

    `symbols` (the day's full watched list, screener + defaults merged) is
    optional only for callers that don't have it handy - it's used purely to
    fill in the "symbols watched" column of the daily summary CSV.
    """
    time_stop_hour = config["trading"]["time_stop_hour"]
    had_any_trades = entries_triggered > 0 or bool(strategy.get_open_trades())
    starting_cash = None
    try:
        starting_cash = float(market_data.broker.get_account().cash)
    except Exception as e:
        logger.debug(f"Could not read starting cash for daily summary: {e}")
    last_check = datetime.now(et)

    while True:
        now = datetime.now(et)

        if executor.check_daily_loss_limit():
            logger.warning("Daily loss limit hit, flattening all positions")
            executor.flatten_all_positions()
            executor.save_trades_log()
            email_notifier.send_daily_summary()
            _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et)
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
                        # RSI is purely for the daily report - a failure here (API
                        # hiccup, insufficient history) must never block the actual
                        # exit order, so it's isolated in its own try/except rather
                        # than sharing the outer one that guards execute_signal.
                        try:
                            signal["exit_rsi"] = market_data.get_rsi(
                                symbol, period=config["trading"].get("rsi_period", 14)
                            )
                        except Exception as rsi_err:
                            logger.debug(f"Could not fetch exit RSI for {symbol}: {rsi_err}")
                            signal["exit_rsi"] = None
                        executor.execute_signal(signal)

                except Exception as e:
                    logger.error(f"Error checking exits for {symbol}: {e}")
                    continue

            open_trades = strategy.get_open_trades()
            if open_trades:
                logger.info(f"Open positions: {len(open_trades)}")
            elif had_any_trades:
                # We entered (or adopted via reconciliation) at least one trade today, and everything is now closed.
                logger.info("All trades closed. Sending daily summary...")
                executor.save_trades_log()
                email_notifier.send_daily_summary()
                _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et)
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
            _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et)
            logger.info(
                f"Daily session complete: entries_triggered={entries_triggered}, "
                f"reason=time_stop"
            )
            return

        time.sleep(30)

def _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et, filepath="logs/daily_summary.csv"):
    """
    Append one row to a running, never-overwritten master CSV - one row per
    trading DAY (as opposed to trade_history.csv's one row per trade) - for
    the "how did each day's settings/results compare" view: date, P&L,
    win rate, which entry method(s) were active, symbols watched, etc.
    """
    try:
        today_trades = [t for t in executor.trades_log if (t.get("exit_time") or "").startswith(datetime.now(et).strftime("%Y-%m-%d"))]
        total_pl = sum(t.get("pl", 0) for t in today_trades)
        wins = [t for t in today_trades if t.get("pl", 0) > 0]
        losses = [t for t in today_trades if t.get("pl", 0) < 0]
        win_rate = (len(wins) / len(today_trades) * 100) if today_trades else 0.0

        ending_cash = None
        try:
            ending_cash = float(market_data.broker.get_account().cash)
        except Exception as e:
            logger.debug(f"Could not read ending cash for daily summary: {e}")

        row = {
            "date": datetime.now(et).strftime("%Y-%m-%d"),
            "total_pl": round(total_pl, 2),
            "starting_cash": starting_cash,
            "ending_cash": ending_cash,
            "trades_count": len(today_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "entries_triggered": entries_triggered,
            "symbols_watched_count": len(symbols) if symbols else None,
            "symbols_watched": "|".join(symbols) if symbols else "",
            "symbols_traded": "|".join(sorted({t["symbol"] for t in today_trades})),
            "use_pullback_entry": config["trading"].get("use_pullback_entry"),
            "use_three_bar_momentum": config["trading"].get("use_three_bar_momentum"),
            "use_rsi_filter": config["trading"].get("use_rsi_filter"),
            "rapid_increase_pct": config["trading"].get("rapid_increase_pct"),
            "entry_window": f"{config['trading'].get('entry_window_start')}-{config['trading'].get('entry_window_end')}",
        }

        fieldnames = list(row.keys())
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        logger.info(f"Wrote daily summary row to {filepath}")
    except Exception as e:
        logger.error(f"Error writing daily summary CSV: {e}")

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

    Also checks the broker's own order history for a filled SELL on this
    symbol since midnight ET today, and marks first_exit_done accordingly -
    a fresh TradeManager always starts with first_exit_done=False, so
    without this check, a position that already had its -0.5% scale-out
    fire before a restart would be eligible to fire ANOTHER first-exit
    tranche after reconciliation, over-trimming the position.
    """
    try:
        positions = broker.get_positions()
    except Exception as e:
        logger.error(f"Error reconciling existing positions: {e}")
        return

    et = pytz.timezone("America/New_York")
    today_start = datetime.now(et).replace(hour=0, minute=0, second=0, microsecond=0)

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

            prior_sells = broker.get_filled_sell_orders_since(symbol, today_start)
            if prior_sells:
                trade.first_exit_done = True

            strategy.trades[symbol] = trade
            executor.open_entries[symbol] = entry_price
            executor.record_entry_meta(symbol, method="RECONCILED", rsi=None, entry_time=None)
            logger.info(
                f"{symbol}: adopted pre-existing position on startup - {qty} shares "
                f"@ {entry_price:.2f} (broker avg_entry_price) - resuming stop-loss/"
                f"trailing-stop management"
                + (f" (first_exit already fired today, {len(prior_sells)} prior sell(s) - won't re-arm it)" if prior_sells else "")
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
                config, market_data, strategy, executor, email_notifier, entries_triggered, et, symbols
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
