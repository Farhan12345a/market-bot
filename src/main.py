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
from src.data.stream import PriceStream
from src.executor.executor import Executor
from src.screener.stock_screener import StockScreener
from src.notifications.email_notifier import EmailNotifier
from src.analytics.signal_journal import SignalJournal

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
    later recomputation would be. Instead it's checked in run_trading_day as
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
        screener_started = time.monotonic()
        future = executor.submit(
            screener.screen,
            top_n=config["trading"]["num_stocks_to_trade"],
            min_score=config["trading"]["min_screener_score"],
        )
        try:
            symbols = future.result(timeout=timeout_seconds)
            logger.info(
                f"Screener finished in {time.monotonic() - screener_started:.1f}s "
                f"(timeout is {timeout_seconds}s)"
            )
        except FutureTimeoutError:
            screener_timed_out = True
            logger.warning(
                f"Screener did not finish within {timeout_seconds}s - "
                f"aborting and falling back to static stock_universe list"
            )
            symbols = []
        except Exception as e:
            logger.error(
                f"Screener failed after {time.monotonic() - screener_started:.1f}s: {e}"
            )
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

def _position_size(config, executor, price):
    """
    Shares to buy for one entry. Returns 0 when no position should be taken.

    Sizing used to be a flat `max_position_per_stock_usd / price` - a fixed
    $10,000 of notional into every name regardless of account size or how many
    positions were already open. With 10 concurrent slots that is $100,000
    committed against ~$95,000 of equity, i.e. the sizing rule itself assumed
    leverage and left max_total_exposure_fraction to arbitrarily reject
    whichever entry happened to arrive once the book filled up.

    The budget is now the SMALLEST of three independent ceilings, so whichever
    constraint binds first wins:

      1. Even slot share - equity * max_total_exposure_fraction
                           / max_concurrent_positions
         Splits deployable capital evenly across the book, so a full book
         lands at the exposure cap by construction rather than by rejection.
         Scales with the account automatically; no re-tuning as it grows.

      2. Risk budget - equity * max_risk_per_trade_fraction
                       / (final_exit_loss_pct / 100)
         The largest position whose hard stop costs at most
         max_risk_per_trade_fraction of equity. This is the one that ties
         size to the stop: widen final_exit_loss_pct and positions shrink on
         their own. With a 1% stop it sits well above ceiling 1 and stays
         dormant, which is intended - it exists to bind when the stop widens.

      3. Hard per-stock cap - max_position_per_stock_usd, unchanged, now a
         backstop rather than the sizing rule.

    Share COUNT is deliberately not a constraint. 780 shares of a $12 stock
    and 40 shares of a $240 stock are the same $10,000 of risk; only the
    dollar figure means anything, and dividing by price is what converts
    the dollar budget into shares.

    Returns 0 if equity isn't known yet (before the first snapshot refresh),
    so entries fail closed rather than sizing off a zero/stale account.
    """
    trading = config["trading"]
    if price <= 0:
        return 0

    equity = executor.equity
    if equity <= 0:
        return 0

    budgets = []

    hard_cap = trading.get("max_position_per_stock_usd")
    if hard_cap:
        budgets.append(float(hard_cap))

    exposure_fraction = trading.get("max_total_exposure_fraction")
    max_positions = trading.get("max_concurrent_positions")
    if exposure_fraction and max_positions:
        budgets.append(equity * exposure_fraction / max_positions)

    risk_fraction = trading.get("max_risk_per_trade_fraction")
    stop_pct = abs(trading.get("final_exit_loss_pct", 0)) / 100
    if risk_fraction and stop_pct > 0:
        budgets.append(equity * risk_fraction / stop_pct)

    if not budgets:
        return 0

    return int(min(budgets) / price)

def _summarise_burst_notes(config, notes):
    """
    One precise sentence describing what burst logic actually ran today, for
    the daily report. Reports the settings AND how often the throttle really
    engaged, so a day where it never triggered is distinguishable from a day
    where it shaped every entry.
    """
    trading = config["trading"]
    if not trading.get("use_burst_throttle", False):
        return "Burst throttle disabled - every qualifying signal sized normally."
    threshold = trading.get("burst_width_threshold", 5)
    max_entries = trading.get("burst_max_entries", 3)
    multiplier = trading.get("burst_size_multiplier", 0.5)
    throttled = sum(1 for n in notes if n and n.startswith("THROTTLED"))
    polls = len(notes)
    return (
        f"Burst throttle ON (>= {threshold} simultaneous signals -> take at most "
        f"{max_entries} at {multiplier:g}x size). Engaged on {throttled} of {polls} "
        f"entry-window polls."
    )


def _window_pct_change(history):
    """% change from the oldest to newest sample in a lookback deque, or None."""
    if not history or len(history) < 2:
        return None
    first, last = history[0][1], history[-1][1]
    if not first:
        return None
    return round((last - first) / first * 100, 3)


def _spread_pct(market_data, symbol, price):
    """
    Bid-ask spread as a % of price, for the signal journal only.

    This is the quantity min_stock_price only approximates: a one-cent spread
    is a fixed dollar cost, so as a percentage it explodes at low prices -
    PLUG at $2.19 quotes ~0.45%, against a first exit that triggers at -0.5%.
    Measuring it directly is what would eventually allow keeping a cheap stock
    that happens to be tight while rejecting an expensive one that is wide.

    Fully guarded: journal-only, called after the entry decision, never gates
    a trade, and returns None on any failure.
    """
    try:
        quote = market_data.broker.get_latest_quote(symbol)
        if not quote or not price:
            return None
        return round(quote["spread"] / price * 100, 4)
    except Exception:
        return None


def _burst_policy(config, burst_width):
    """
    Decide how many of this poll's simultaneous signals to act on, and at what
    size. Returns (max_entries, size_multiplier, description).

    When many symbols fire in the same poll they are almost never independent
    ideas - they are one market move showing up in many tickers at once. On
    2026-08-19 twenty names triggered inside nine seconds; the book was not
    twenty bets, it was one bet held twenty times, and when the move went the
    wrong way every position lost together (1 winner out of 23, -$1,307).

    Note what this does and does not fix. Taking the first N of a burst is NOT
    diversification - it is the same bet held N times instead of twenty, which
    reduces the size of the loss without changing its nature. Ranking (buying
    the "best" of a burst) is a separate improvement and needs the evidence
    the signal journal is being built to collect; this only controls how much
    is committed to a single market move.

    Below burst_width_threshold nothing changes: a couple of signals in one
    poll is normal, uncorrelated behavior and gets full size.
    """
    trading = config["trading"]
    if not trading.get("use_burst_throttle", False):
        return None, 1.0, f"disabled (burst={burst_width})"

    threshold = trading.get("burst_width_threshold", 5)
    if burst_width < threshold:
        return None, 1.0, f"normal: burst={burst_width} < threshold {threshold}, full size"

    max_entries = trading.get("burst_max_entries", 3)
    size_multiplier = trading.get("burst_size_multiplier", 0.5)
    return (
        max_entries,
        size_multiplier,
        f"THROTTLED: burst={burst_width} >= {threshold}, took <= {max_entries} at {size_multiplier:g}x size",
    )


def _compute_rvol(bar, volume_history):
    """
    This bar's volume against the average of this symbol's own recent bars.
    Returns None until there is enough history to compare against. Journal
    only - never gates an entry.
    """
    try:
        vol = float(bar.get("volume") or 0)
        prior = [v for v in volume_history if v > 0]
        if vol <= 0 or len(prior) < 3:
            return None
        avg = sum(prior) / len(prior)
        return round(vol / avg, 3) if avg > 0 else None
    except Exception:
        return None


def _attempt_entry(config, strategy, executor, symbol, price, entry_method, symbol_rsi,
                   size_multiplier=1.0, burst_note=None):
    """
    Shared entry path for all three entry signals (three-bar momentum, rapid
    increase immediate, pullback resumption). Returns True if a position was
    actually opened.

    Order matters, and is the fix for the 2026-08-18 phantom-entry bug:
      1. strategy.can_enter - pure check, no state mutated.
      2. executor.pre_entry_check - funds/exposure check, no broker call made.
      3. executor.submit_entry_order - the ONLY step that touches the broker.
      4. strategy.confirm_entry - commits to strategy.trades, and ONLY runs
         if step 3 actually succeeded.
    Previously, step 4's equivalent (the old enter_trade()) ran BEFORE step 3,
    so a failed broker order (e.g. insufficient buying power) left a phantom
    position in the bot's memory. When exit logic later fired against that
    phantom long, the resulting sell - for shares never actually bought -
    was accepted by Alpaca's margin account as opening a real, completely
    untracked short position. 16 of them happened this way in one session.
    """
    qty = int(_position_size(config, executor, price) * size_multiplier)
    if qty <= 0:
        logger.info(f"{symbol}: entry skipped - position size worked out to 0 shares at {price:.2f}")
        return False
    if not strategy.can_enter(symbol, qty):
        return False

    # min_stock_price / max_stock_price were dead config until 2026-08-20 -
    # declared, documented, and read by nothing. stock_screener.score_stock even
    # carries a "# Price check (filter only)" comment above code that records
    # the price and never filters on it. So the whole cheap-stock cohort stayed
    # tradeable: PLUG, BMBL, OPEN, GRAB, NIO, LCID and SOUN all sit in the
    # static stock_universe and were watched every day regardless of the setting.
    #
    # That matters because a one-cent spread is a fixed dollar cost, so as a
    # PERCENT it explodes at low prices while every stop here is a percent. PLUG
    # at $2.19 quotes ~0.45% (measured live), against a first exit that fires at
    # -0.5% - the position is at its stop the moment it fills. Sub-$10 names went
    # 16 trades / 1 winner / -$623 on 2026-08-19, 36% of the day's loss from 23%
    # of its trades.
    #
    # Enforced here rather than by pruning the watchlist at selection time: this
    # is the price actually being paid at the moment of entry, and it costs no
    # extra API calls.
    min_price = config["trading"].get("min_stock_price")
    max_price = config["trading"].get("max_stock_price")
    if min_price and price < min_price:
        logger.info(
            f"{symbol}: entry skipped - price ${price:.2f} below min_stock_price ${min_price}"
        )
        return False
    if max_price and price > max_price:
        logger.info(
            f"{symbol}: entry skipped - price ${price:.2f} above max_stock_price ${max_price}"
        )
        return False

    cooldown_left = executor.reentry_cooldown_remaining(symbol)
    if cooldown_left > 0:
        logger.info(
            f"{symbol}: entry skipped - re-entry cooldown, {cooldown_left / 60:.1f} min left "
            f"since it was last stopped out"
        )
        return False

    ok, reason = executor.pre_entry_check(qty, price)
    if not ok:
        logger.info(f"{symbol}: entry skipped - {reason}")
        return False

    order = executor.submit_entry_order(symbol, qty, price, entry_method=entry_method, entry_rsi=symbol_rsi)
    if order is None:
        return False  # broker rejected/failed - already logged by submit_entry_order, nothing committed

    strategy.confirm_entry(symbol, price, qty)
    if burst_note:
        executor.entry_meta.setdefault(symbol, {})["burst_logic"] = burst_note
    rsi_note = f", RSI={symbol_rsi:.1f}" if symbol_rsi is not None else ""
    size_note = f", {size_multiplier:g}x size" if size_multiplier != 1.0 else ""
    logger.info(
        f"{symbol}: {entry_method} entry confirmed - {qty} shares @ {price:.2f}{rsi_note}{size_note}"
    )
    return True

def run_trading_day(config, market_data, strategy, executor, symbols, rsi_values, email_notifier, et, signal_journal=None):
    """
    Runs the entire trading day as ONE continuous loop, from entry_window_start
    until either all positions have closed after the entry window ends, or the
    4 PM time stop is hit.

    This replaces the old run_entry_window -> run_exit_monitoring design,
    which ran the full 9:30-9:55 entry window as one blocking phase before
    exit-checking ever started - meaning a position opened at, say, 9:31 got
    ZERO stop-loss protection until 9:55. On 2026-08-18, FCEL ran to -7.42%
    (well past both the -0.5% and -1.0% stop thresholds) before the bot ever
    checked it, purely because exit monitoring hadn't started yet. Now, every
    poll cycle checks exits for everything already open AND checks entries
    for everything not yet open (while still inside the entry window) in the
    same pass - stop-losses are live from the moment a position opens.

    If use_pullback_entry is on: a detected rapid increase does NOT buy
    immediately. Instead it starts tracking that symbol for a pullback off
    the post-thrust peak followed by a resumption higher, and only buys on
    the resumption - see _advance_pullback_state for the state machine. If
    off, a rapid increase buys immediately via _attempt_entry.

    If use_three_bar_momentum is on: a separate, faster signal checked ahead
    of everything else. The instant 3 consecutive 1-minute bars are all green
    with each bar's close higher than the last, it buys immediately - no
    waiting for rapid_increase_pct's % threshold, no pullback-wait either.
    """
    entry_start = parse_hhmm_today(config["trading"]["entry_window_start"], et)
    entry_end = parse_hhmm_today(config["trading"]["entry_window_end"], et)
    time_stop_hour = config["trading"]["time_stop_hour"]
    check_interval = config["trading"]["entry_check_interval_seconds"]
    lookback = timedelta(minutes=config["trading"]["rapid_increase_lookback_minutes"])
    use_rsi_filter = config["trading"].get("use_rsi_filter", False)
    rsi_max = config["trading"].get("rsi_max_for_entry", 50)
    use_pullback_entry = config["trading"].get("use_pullback_entry", False)
    use_three_bar_momentum = config["trading"].get("use_three_bar_momentum", False)
    three_bar_require_acceleration = config["trading"].get("three_bar_require_acceleration", True)
    max_daily_entries = config["trading"].get("max_daily_entries")  # None/0 = unlimited
    daily_entry_cap_logged = False  # so the "cap reached" line is logged once, not every poll
    use_opening_reversal_entry = config["trading"].get("use_opening_reversal_entry", False)
    opening_reversal_window = timedelta(minutes=config["trading"].get("opening_reversal_window_minutes", 5))
    opening_reversal_drop_bars = config["trading"].get("opening_reversal_drop_bars", 5)
    opening_reversal_confirm_bars = config["trading"].get("opening_reversal_confirm_bars", 5)
    rsi_period = config["trading"].get("rsi_period", 14)

    now = datetime.now(et)
    while now < entry_start:
        time.sleep(min(5, (entry_start - now).total_seconds()))
        now = datetime.now(et)

    logger.info(
        f"===== TRADING DAY START: entries watched {entry_start.strftime('%H:%M')}-"
        f"{entry_end.strftime('%H:%M')} ET, exits watched continuously from the moment "
        f"a position opens ====="
    )

    price_history = {symbol: deque() for symbol in symbols}
    bar_history = {symbol: deque(maxlen=3) for symbol in symbols}  # last 3 1min bars, for use_three_bar_momentum
    pending_pullbacks = {}  # symbol -> state dict, only used when use_pullback_entry is on
    pending_reversals = {}  # symbol -> state dict, only used when use_opening_reversal_entry is on
    reversal_drop_bar_history = {symbol: deque(maxlen=opening_reversal_drop_bars) for symbol in symbols}  # only used when use_opening_reversal_entry is on
    symbol_open_price = {}  # symbol -> first observed price this window, purely for the log message - only tracked when use_opening_reversal_entry is on
    symbol_price_log = {symbol: [] for symbol in symbols}  # full (untrimmed) history, for _write_price_log
    entries_triggered = 0
    had_any_trades = bool(strategy.get_open_trades())  # true if reconciliation adopted positions on startup
    starting_cash = None
    try:
        starting_cash = float(market_data.broker.get_account().cash)
    except Exception as e:
        logger.debug(f"Could not read starting cash for daily summary: {e}")

    def finish_day(reason):
        signal_journal.flush()
        executor.save_trades_log()
        email_notifier.send_daily_summary(burst_summary=executor.day_burst_summary)
        _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et)
        _write_price_log(symbol_price_log, et)
        executor.day_burst_summary = _summarise_burst_notes(config, day_burst_notes)
        logger.info(f"Burst logic for the day: {executor.day_burst_summary}")
        logger.info(f"Signal journal: {signal_journal.stats()}")
        logger.info(f"Daily session complete: entries_triggered={entries_triggered}, reason={reason}")

    if signal_journal is None:
        signal_journal = SignalJournal(config)
    stream_warned = False
    volume_history = {symbol: deque(maxlen=20) for symbol in symbols}  # for intraday RVOL
    spy_history = deque()          # SPY samples, for excess-return-vs-market
    day_burst_notes = []           # one note per poll, summarised into the daily report

    while True:
        now = datetime.now(et)
        executor.refresh_account_snapshot()

        # A stream that is nominally connected but delivering no bars is the
        # dangerous case: every read silently falls back to REST, so the bot
        # keeps trading on ~15-minute-delayed prices while the logs show a
        # connected stream. Warn once per session rather than let that pass
        # unnoticed. Trading is unaffected either way - the fallback is what
        # keeps prices flowing - so this is a heads-up, not a halt.
        # Fill in forward returns for journalled signals whose horizon has
        # elapsed. Uses the price source already in use, so no extra API cost
        # for symbols already being watched.
        signal_journal.update_forward_returns(
            lambda sym: (market_data.get_latest_bar(sym, "1Min") or {}).get("close")
        )

        stream = getattr(market_data, "stream", None)
        if stream is not None and not stream_warned:
            stats = stream.stats()
            if stats["connected"] and not stats["healthy"] and stats["bars_received"] == 0:
                logger.warning(
                    "Price stream is connected but has delivered ZERO bars - "
                    "every price read is falling back to REST (~15 min delayed). "
                    "Check for an Alpaca stream connection limit (only one data "
                    "websocket per account) or a network the stream endpoint refuses."
                )
                stream_warned = True

        if executor.check_daily_loss_limit():
            logger.warning("Daily loss limit hit, flattening all positions")
            flattened = executor.flatten_all_positions()
            for symbol in flattened:
                strategy.trades.pop(symbol, None)
            finish_day("daily_loss_limit")
            return entries_triggered

        # ---- EXIT CHECKS: every open position, every cycle, from the moment it opens ----
        for symbol in list(strategy.get_open_trades().keys()):
            try:
                current_bar = market_data.get_latest_bar(symbol, "1Min")
                if not current_bar:
                    continue

                exit_info = strategy.check_exit(symbol, current_bar)
                if exit_info:
                    # RSI is purely for the daily report - a failure here (API
                    # hiccup, insufficient history) must never block the
                    # actual exit order.
                    try:
                        exit_rsi = market_data.get_rsi(symbol, period=rsi_period)
                    except Exception as rsi_err:
                        logger.debug(f"Could not fetch exit RSI for {symbol}: {rsi_err}")
                        exit_rsi = None

                    # Read excursions BEFORE confirm_exit - a full exit deletes
                    # the TradeManager, taking the peak/trough with it.
                    trade = strategy.trades.get(symbol)
                    mfe_pct, mae_pct = trade.excursions() if trade else (None, None)

                    order = executor.submit_exit_order(
                        symbol, exit_info["qty"], exit_info["reason"], exit_info["price"],
                        exit_rsi=exit_rsi, mfe_pct=mfe_pct, mae_pct=mae_pct,
                    )
                    if order is not None:
                        strategy.confirm_exit(symbol, exit_info["qty"], exit_info["reason"], exit_info["price"])
                        had_any_trades = True
                    else:
                        logger.error(
                            f"{symbol}: exit order FAILED to submit ({exit_info['reason']}) - "
                            f"position remains tracked, will retry next check"
                        )

            except Exception as e:
                logger.error(f"Error checking exits for {symbol}: {e}")
                continue

        # ---- ENTRY CHECKS: only within the window, only for symbols not already open ----
        # max_daily_entries is a separate limit from max_concurrent_positions
        # and caps a different thing: the TOTAL number of positions opened over
        # the whole day, however many are held at once. On 2026-08-19 the
        # concurrent cap of 10 was respected, yet 54 buys still went through in
        # ~21 minutes, because positions kept closing and new ones opening in
        # their place - the concurrent cap has nothing to say about that churn.
        #
        # Deliberately gates ONLY this entry block. Exit checks above run every
        # cycle regardless, so hitting the cap stops the bot opening anything
        # new but never leaves an already-open position unprotected.
        #
        # entries_triggered is local to run_trading_day, so it resets on its own
        # each trading day rather than accumulating across days in this
        # long-running process.
        if max_daily_entries and entries_triggered >= max_daily_entries:
            if not daily_entry_cap_logged:
                logger.info(
                    f"Reached max_daily_entries ({entries_triggered}/{max_daily_entries}) - "
                    f"no new entries for the rest of the day; exits continue as normal"
                )
                daily_entry_cap_logged = True
        elif now < entry_end:
            # SPY is tracked purely as a market benchmark - never traded. It is
            # what separates "this stock is strong" from "the market went up":
            # during a burst every name's raw move looks alike, but excess
            # return over the index collapses toward zero for the ones that are
            # only beta.
            try:
                spy_bar = market_data.get_latest_bar("SPY", "1Min")
                if spy_bar:
                    spy_ts = spy_bar.get("timestamp", now)
                    spy_history.append((spy_ts, spy_bar.get("close", 0)))
                    spy_cutoff = spy_ts - lookback
                    while spy_history and spy_history[0][0] < spy_cutoff:
                        spy_history.popleft()
            except Exception as e:
                logger.debug(f"SPY benchmark unavailable this poll: {e}")

            # PASS 1 - detect only. Signals are collected, not acted on, so
            # the number firing in THIS poll is known before any order is
            # placed. That count (the burst width) is what _burst_policy needs
            # in order to distinguish a couple of independent ideas from one
            # market move showing up in twenty tickers at once.
            #
            # Only the two live entry paths are deferred this way. The
            # pullback and opening-reversal state machines are both toggled
            # off and keep their existing immediate behavior rather than being
            # restructured while unproven.
            burst_candidates = []
            for symbol in symbols:
                # Re-checked per SYMBOL, not just once per poll. The outer
                # check above only runs at the top of a cycle, and this inner
                # loop can open many positions within that one cycle - on
                # 2026-08-19 twenty entries landed inside a single 9-second
                # pass - so a per-poll-only check would let an entire burst
                # through before the cap was ever consulted again. Same
                # failure shape as the exposure-cache race.
                if max_daily_entries and entries_triggered >= max_daily_entries:
                    if not daily_entry_cap_logged:
                        logger.info(
                            f"Reached max_daily_entries ({entries_triggered}/{max_daily_entries}) - "
                            f"no new entries for the rest of the day; exits continue as normal"
                        )
                        daily_entry_cap_logged = True
                    break

                if symbol in strategy.get_open_trades():
                    continue

                try:
                    bar = market_data.get_latest_bar(symbol, "1Min")
                    if not bar:
                        continue

                    # ENTRY price only. The exit loop above keeps using the
                    # bar close it already reads - see get_entry_price.
                    price = market_data.get_entry_price(symbol, bar)
                    ts = bar.get("timestamp", now)
                    history = price_history[symbol]
                    history.append((ts, price))
                    bar_history[symbol].append(bar)
                    symbol_price_log[symbol].append((ts, price))
                    volume_history[symbol].append(float(bar.get("volume") or 0))

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

                    if use_three_bar_momentum and _check_three_bar_momentum(
                        bar_history[symbol], require_acceleration=three_bar_require_acceleration
                    ):
                        closes = [b.get("close", 0) for b in bar_history[symbol]]
                        gaps = [closes[1] - closes[0], closes[2] - closes[1]]
                        logger.info(
                            f"{symbol}: THREE-BAR MOMENTUM signal - 3 consecutive green 1min "
                            f"bars, closes {closes[0]:.2f} -> {closes[1]:.2f} -> {closes[2]:.2f} "
                            f"(gaps +{gaps[0]:.2f}, +{gaps[1]:.2f}"
                            f"{', accelerating' if three_bar_require_acceleration else ''})"
                        )
                        burst_candidates.append({
                            "symbol": symbol, "price": price, "method": "THREE_BAR_MOMENTUM",
                            "rsi": symbol_rsi, "signal_pct": None, "bar": bar,
                        })
                        continue

                    if use_pullback_entry and symbol in pending_pullbacks:
                        entered = _advance_pullback_state(
                            config, strategy, executor, symbol, price, pending_pullbacks, symbol_rsi,
                        )
                        if entered:
                            entries_triggered += 1
                            had_any_trades = True
                            pending_pullbacks.pop(symbol, None)
                        continue

                    if use_opening_reversal_entry:
                        if symbol not in symbol_open_price:
                            symbol_open_price[symbol] = price

                        if symbol in pending_reversals:
                            entered = _advance_reversal_state(
                                config, strategy, executor, symbol, bar, pending_reversals, symbol_rsi,
                            )
                            if entered:
                                entries_triggered += 1
                                had_any_trades = True
                                pending_reversals.pop(symbol, None)
                            continue

                        elif now - entry_start <= opening_reversal_window:
                            reversal_drop_bar_history[symbol].append(bar)
                            if _check_reversal_bar_pattern(
                                reversal_drop_bar_history[symbol], opening_reversal_drop_bars, direction="down"
                            ):
                                pending_reversals[symbol] = {
                                    "base_price": symbol_open_price[symbol],
                                    "low": price,
                                    "bounce_bar_history": deque(maxlen=opening_reversal_confirm_bars),
                                }
                                logger.info(
                                    f"{symbol}: OPENING DECLINE detected - {opening_reversal_drop_bars} "
                                    f"consecutive red bars from open {symbol_open_price[symbol]:.2f}, now "
                                    f"{price:.2f} - watching for a reversal bounce"
                                )
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

                        logger.info(
                            f"{symbol}: RAPID INCREASE signal - +{pct_change:.2f}% over "
                            f"{lookback.total_seconds()/60:.0f}min (threshold "
                            f"{config['trading']['rapid_increase_pct']}%)"
                        )
                        burst_candidates.append({
                            "symbol": symbol, "price": price, "method": "RAPID_INCREASE_IMMEDIATE",
                            "rsi": symbol_rsi, "signal_pct": round(pct_change, 3), "bar": bar,
                        })

                except Exception as e:
                    logger.error(f"Error checking entry for {symbol}: {e}")
                    continue

            # PASS 2 - act. Burst width is now known for the whole poll.
            burst_width = len(burst_candidates)
            burst_max, burst_size, burst_note = _burst_policy(config, burst_width)
            if burst_max is not None and burst_width >= config["trading"].get("burst_width_threshold", 5):
                logger.warning(
                    f"BURST DETECTED: {burst_width} symbols signalled in one poll "
                    f"({', '.join(c['symbol'] for c in burst_candidates)}) - {burst_note}"
                )
            day_burst_notes.append(burst_note)

            spy_pct = _window_pct_change(spy_history)

            for index, cand in enumerate(burst_candidates):
                symbol, price = cand["symbol"], cand["price"]
                taken, skip_reason = False, None

                if max_daily_entries and entries_triggered >= max_daily_entries:
                    skip_reason = "max_daily_entries"
                elif burst_max is not None and index >= burst_max:
                    skip_reason = "burst_throttle"
                else:
                    taken = _attempt_entry(
                        config, strategy, executor, symbol, price, cand["method"], cand["rsi"],
                        size_multiplier=burst_size, burst_note=burst_note,
                    )
                    if taken:
                        entries_triggered += 1
                        had_any_trades = True
                        pending_pullbacks.pop(symbol, None)
                    else:
                        skip_reason = "rejected_by_pre_entry_checks"

                # Journal EVERY signal, taken or not - the refused ones are
                # the control group any future ranking has to be judged
                # against. Recorded after the decision so it cannot affect it.
                sig_pct = cand["signal_pct"]
                signal_journal.record(
                    symbol=symbol, entry_method=cand["method"], price=price,
                    signal_pct=sig_pct,
                    spy_pct=spy_pct,
                    excess_vs_spy_pct=(round(sig_pct - spy_pct, 3)
                                       if sig_pct is not None and spy_pct is not None else None),
                    rvol=_compute_rvol(cand["bar"], volume_history[symbol]),
                    spread_pct=_spread_pct(market_data, symbol, price),
                    burst_width=burst_width,
                    taken=taken, skip_reason=skip_reason,
                    qty=None, size_multiplier=burst_size,
                )

        # ---- day-completion checks ----
        open_trades = strategy.get_open_trades()
        if not open_trades and now >= entry_end and had_any_trades:
            logger.info("All trades closed. Sending daily summary...")
            finish_day("all_closed")
            return entries_triggered

        if now.hour >= time_stop_hour:
            logger.info("Market closing, flattening all positions...")
            flattened = executor.flatten_all_positions()
            for symbol in flattened:
                strategy.trades.pop(symbol, None)
            finish_day("time_stop")
            return entries_triggered

        time.sleep(check_interval)

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

def _check_three_bar_momentum(bars, require_acceleration=True):
    """
    True if `bars` holds 3 1-minute bars forming a clean upward thrust:
    all green (close > open), each close strictly higher than the last, and
    - when require_acceleration is on - each close-to-close gap strictly
    LARGER than the one before it.

    The acceleration requirement is the whole point of the signal. Without
    it, "3 rising closes" happily matches a move that is running out of
    steam, because a decelerating series still rises:

        9.51 -> 9.55 -> 9.56    gaps +0.04, +0.01   rising, but stalling
        9.51 -> 9.53 -> 9.58    gaps +0.02, +0.05   rising AND accelerating

    Both pass a plain "each close higher" test; only the second is a thrust
    still gaining speed. This signal exists to buy the second shape, and
    buying the first means buying into the top of a move that has already
    largely played out.

    Gaps are compared in absolute dollars rather than percent. Over three
    consecutive 1-minute bars of a single symbol the denominator barely
    moves, so the two orderings are equivalent in practice, and absolute
    keeps the log line directly comparable to the printed closes.
    """
    if len(bars) < 3:
        return False
    bars = list(bars)
    if any(b.get("close", 0) <= b.get("open", 0) for b in bars):
        return False

    closes = [b.get("close", 0) for b in bars]
    gaps = [curr - prev for prev, curr in zip(closes, closes[1:])]

    if any(gap <= 0 for gap in gaps):
        return False

    if require_acceleration:
        return all(nxt > cur for cur, nxt in zip(gaps, gaps[1:]))

    return True

def _check_reversal_bar_pattern(bars, count, direction):
    """
    Standalone bar-run check used only by the opening-reversal (U-shape)
    feature - deliberately NOT shared with _check_three_bar_momentum above,
    even though the underlying logic is similar, so this new/unproven
    feature can never touch or risk that already-live production check.

    True if the most recent `count` bars in `bars` are all the same color in
    `direction`, with each bar's close strictly further in that direction
    than the previous bar's close - i.e. a clean N-bar run, not just N
    same-colored bars chopping sideways.

    direction="down": all red (close < open), closes strictly falling - used
    to detect a "drastic drop" worth watching for use_opening_reversal_entry.
    direction="up": all green (close > open), closes strictly rising - used
    to confirm the reversal bounce off the tracked low is real.
    """
    if len(bars) < count:
        return False
    recent = list(bars)[-count:]
    if direction == "up":
        if any(b.get("close", 0) <= b.get("open", 0) for b in recent):
            return False
        return all(curr.get("close", 0) > prev.get("close", 0) for prev, curr in zip(recent, recent[1:]))
    else:
        if any(b.get("close", 0) >= b.get("open", 0) for b in recent):
            return False
        return all(curr.get("close", 0) < prev.get("close", 0) for prev, curr in zip(recent, recent[1:]))

def _advance_pullback_state(config, strategy, executor, symbol, price, pending_pullbacks, symbol_rsi):
    """
    Advance one symbol's pullback-entry state machine by one price sample.
    Only called (from run_trading_day) once a rapid-increase thrust has
    already been detected for `symbol` and it hasn't been bought yet.
    Returns True if a position was opened this call (caller should then pop
    pending_pullbacks[symbol] - this function does NOT pop it on a successful
    entry, only on invalidation, so the caller has one consistent place to
    decide when tracking for this symbol is done).

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
                logger.info(
                    f"{symbol}: PULLBACK RESUMPTION signal - thrust +{setup['pct_change']:.2f}%, "
                    f"peak {setup['peak']:.2f}, pulled back to {setup['pullback_low']:.2f}, "
                    f"resumed to {price:.2f}"
                )
                return _attempt_entry(config, strategy, executor, symbol, price, "PULLBACK_RESUMPTION", symbol_rsi)

    if price > setup["peak"]:
        setup["peak"] = price
        setup["pullback_low"] = None
        return False

    thrust_gain = setup["peak"] - setup["base_price"]
    if thrust_gain <= 0:
        del pending_pullbacks[symbol]
        return False

    giveback = setup["peak"] - price
    if giveback / thrust_gain >= max_giveback_fraction:
        logger.info(
            f"{symbol}: pullback gave back {giveback / thrust_gain * 100:.0f}% of the thrust's "
            f"gain (peak {setup['peak']:.2f} -> {price:.2f}) - setup invalidated"
        )
        del pending_pullbacks[symbol]
        return False

    if setup["pullback_low"] is None:
        retracement_pct = giveback / setup["peak"]
        if retracement_pct >= min_pullback_pct:
            setup["pullback_low"] = price
    return False

def _advance_reversal_state(config, strategy, executor, symbol, bar, pending_reversals, symbol_rsi):
    """
    Advance one symbol's opening-reversal (U-shape) state machine by one bar.
    Only called once a "drastic drop" (opening_reversal_drop_bars consecutive
    red bars) has already been detected for `symbol` within
    opening_reversal_window_minutes of market open, and it hasn't been
    bought yet. Returns True if a position was opened this call.

    Confirmation mirrors use_three_bar_momentum's own logic (via the
    separate _check_reversal_bar_pattern helper) rather than a simple
    %-bounce threshold: requires opening_reversal_confirm_bars consecutive
    GREEN bars off the tracked low before buying - the same "don't chase a
    single tick, wait for a real run" discipline already used for the
    up-momentum signal,
    applied to confirming the reversal itself is real rather than a
    single-bar fakeout.

    Structurally mirrors _advance_pullback_state - tracking a trough instead
    of a peak - but deliberately simpler: pullback exists to avoid buying
    the TOP of an ALREADY-ESTABLISHED up-thrust, so it needs the extra
    giveback/max_giveback_fraction invalidation logic to tell "a normal
    pullback within a real breakout" apart from "a failed breakout that
    shouldn't be bought at all." That distinction doesn't have a clean
    equivalent here: catching the bounce off the trough IS the entire
    signal - there's no pre-existing trend being resumed to protect against
    overpaying for, so there's nothing analogous to invalidate against. A
    red bar breaking a forming bounce just falls out of the rolling
    confirm-bar window on its own - no separate invalidation branch needed.
    """
    setup = pending_reversals[symbol]
    price = bar.get("close", 0)
    confirm_bars = config["trading"].get("opening_reversal_confirm_bars", 5)

    if price < setup["low"]:
        setup["low"] = price

    setup["bounce_bar_history"].append(bar)

    if _check_reversal_bar_pattern(setup["bounce_bar_history"], confirm_bars, direction="up"):
        logger.info(
            f"{symbol}: OPENING REVERSAL signal - {confirm_bars} consecutive green bars off a "
            f"low of {setup['low']:.2f} (dropped from open {setup['base_price']:.2f}), now {price:.2f}"
        )
        return _attempt_entry(config, strategy, executor, symbol, price, "OPENING_REVERSAL", symbol_rsi)

    return False

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
            "rapid_increase_config": f"{config['trading'].get('rapid_increase_pct')}% / {config['trading'].get('rapid_increase_lookback_minutes')}min",
            "final_stop_loss_pct": config["trading"].get("final_exit_loss_pct"),
            "first_scale_out_config": f"{config['trading'].get('first_exit_loss_pct')}% / {config['trading'].get('first_exit_pct', 0) * 100:.0f}%",
            "trailing_stop_pct": config["trading"].get("trailing_stop_pct"),
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

        # Real-time price stream. Alpaca's free tier delays the REST
        # historical endpoint ~15 minutes but carries live IEX data over the
        # WebSocket, so this is purely a delivery-mechanism change - no plan
        # upgrade involved. MarketDataManager falls back to REST per-symbol
        # whenever the stream has no fresh bar, so a failure here costs
        # freshness, never availability.
        price_stream = None
        if config["trading"].get("use_websocket_stream", True):
            price_stream = PriceStream(
                broker.api_key,
                broker.api_secret,
                feed=config["trading"].get("websocket_feed", "iex"),
                subscribe_trades=config["trading"].get("use_trade_ticks_for_entry", True),
            )

        # Initialize components
        market_data = MarketDataManager(broker, stream=price_stream)
        strategy = Strategy(config)
        executor = Executor(broker, config)
        reconcile_existing_positions(broker, strategy, executor)
        email_notifier = EmailNotifier(config)
        signal_journal = SignalJournal(config)

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

        # Screener runs BEFORE the open, so the entry window starts with the
        # symbol list already in hand. Previously the loop waited on
        # is_market_open() and only then screened, which on 2026-08-19 meant
        # the screener started at 09:30:51 and finished at 09:32:15 - burning
        # the first 2.5 minutes of a 25-minute entry window before the bot
        # could take a single trade.
        screener_start = config["trading"].get("screener_start_time", "09:05")
        screener_hour, screener_minute = (int(x) for x in screener_start.split(":"))

        # Holds the pre-market screener result until the open consumes it.
        pending_selection = None
        # Date of the last completed session. run_trading_day RETURNS once every
        # position is closed ("all_closed"), which on 2026-08-20 happened at
        # 10:14 ET - and the loop immediately re-screened and started a second
        # trading day for the same date. Harmless that day only because the
        # entry window had already passed, but it burned a screener run and
        # would re-arm entries entirely if a session ever finished early enough.
        last_session_date = None

        while True:
            now = datetime.now(et)

            if market_data.is_market_open():
                if last_session_date == now.date():
                    # Already traded today - wait for tomorrow rather than
                    # starting a second session on the same date.
                    time.sleep(60)
                    continue

                if pending_selection is None:
                    # No pre-market run happened - the process started late,
                    # or was restarted mid-session. Screen now rather than
                    # trade a stale/empty list; this is the old behavior and
                    # costs entry-window time, hence the warning.
                    logger.warning(
                        "Market already open with no pre-market screener result "
                        "(late start or mid-session restart) - screening now, "
                        "which eats into the entry window"
                    )
                    pending_selection = select_symbols(config, screener, market_data)

                symbols, rsi_values = pending_selection
                pending_selection = None

                # Subscribe only once the day's symbol list is known. Started
                # here rather than at construction because the watchlist isn't
                # decided until the screener has run.
                if price_stream is not None:
                    price_stream.start(symbols)

                logger.info("Market is open, monitoring for signals...")
                try:
                    run_trading_day(
                        config, market_data, strategy, executor, symbols, rsi_values,
                        email_notifier, et, signal_journal,
                    )
                    last_session_date = datetime.now(et).date()
                finally:
                    if price_stream is not None:
                        logger.info(f"Price stream for the session: {price_stream.stats()}")
                        logger.info(f"Bar reads by source: {market_data.data_source_stats()}")
                        price_stream.stop()
                continue

            # Market closed. Run the screener once, inside the pre-market
            # window that starts at screener_start_time and ends at the open.
            market_open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
            screener_time_today = now.replace(
                hour=screener_hour, minute=screener_minute, second=0, microsecond=0
            )

            if (
                pending_selection is None
                and market_data.is_trading_day(now)
                and screener_time_today <= now < market_open_today
            ):
                logger.info(
                    f"===== PRE-MARKET: screening at {now:%H:%M:%S} ET, "
                    f"{(market_open_today - now).total_seconds() / 60:.0f} min ahead of the open ====="
                )
                pending_selection = select_symbols(config, screener, market_data)
                finished = datetime.now(et)
                if finished >= market_open_today:
                    logger.warning(
                        f"Screener ran past the open (finished {finished:%H:%M:%S} ET) - "
                        f"move screener_start_time earlier than {screener_start}"
                    )
                continue

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
