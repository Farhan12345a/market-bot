import csv
import logging
import json
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# How long a just-opened position is trusted as open even while the broker's
# position list still doesn't show it. Alpaca's get_all_positions() lags a
# fill by a second or two, so a refresh landing in that gap would otherwise
# report the position as not existing and undercount live exposure. Two poll
# cycles (poll is 60s) is comfortably longer than any observed lag, while
# still being short enough that a position closed outside the bot's own exit
# path can't linger in the count indefinitely.
ENTRY_CONFIRM_GRACE_SECONDS = 120

# Exit reasons that represent a protective stop firing (vs. a discretionary/
# time-based exit) - used to fill the "was a stop-loss used" column in the
# daily trade report.
STOP_LOSS_EXIT_REASONS = {"FINAL_EXIT_-1.0%", "FIRST_EXIT_-0.5%", "TRAILING_STOP", "FLATTEN_ALL"}

# FIRST_EXIT_-0.5% is the only exit reason that ever sells a PARTIAL position
# (first_exit_pct, e.g. 33%) - every other reason always sells the entire
# qty_remaining (see TradeManager/Strategy.check_exit). Used to color-code
# entry/exit log lines for live journalctl monitoring - green (buy), red
# (100% sold), yellow (partial sold) - so a scan of the live log makes the
# nature of each fill obvious at a glance.
PARTIAL_EXIT_REASONS = {"FIRST_EXIT_-0.5%"}

ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_RESET = "\033[0m"

class Executor:
    """Handles order submission and trade tracking"""

    def __init__(self, broker, config):
        self.broker = broker
        self.config = config
        # trades_log holds one row PER COMPLETED EXIT - self-contained with
        # entry_price, exit_price, qty, pl, pl_pct, exit_reason - this is what
        # gets saved to trades.json and read by the email/push-notification
        # summaries. Raw order submissions (entries + exits, with order IDs)
        # are tracked separately in order_history for auditability.
        self.trades_log = []
        self.daily_pnl = 0.0
        self.order_history = []
        self.open_entries = {}  # symbol -> entry price, used to compute P&L when the matching exit(s) happen
        self.entry_meta = {}  # symbol -> {method, rsi, entry_time}, for the daily trade report
        self._csv_appended_count = 0  # how many trades_log rows have already been written to trade_history.csv -
        # trades_log is never cleared across days in a long-running process, so without this cursor,
        # calling save_trades_log() on day 2 would re-append day 1's trades to the CSV a second time.

        # Cached account/exposure snapshot, refreshed once per poll cycle (see
        # refresh_account_snapshot) rather than once per symbol - used by
        # pre_entry_check() to gate entries on real available capital/exposure
        # BEFORE ever attempting a broker order, instead of relying on the
        # broker's margin engine to eventually reject an order once out of
        # room (which is what let the account run to ~2.8x leverage across 28
        # simultaneous positions on 2026-08-18 before anything stopped it).
        # Starts fail-closed (0 buying power) so nothing can enter before the
        # first real refresh happens.
        self._equity = 0.0
        self._buying_power = 0.0
        self._total_exposure_usd = 0.0

        # Open positions are tracked as a SET OF SYMBOLS, not a bare count.
        # A count was maintained by two mechanisms that could disagree -
        # refresh_account_snapshot() assigning len(broker_positions), and
        # +1/-1 per confirmed order - and every poll the assignment clobbered
        # the increments. When the broker's position list lagged the fills
        # from the previous poll (it lags by a second or two), the count
        # reset BELOW the true number of open positions, and the cap let
        # extra entries through. That is how a buy was approved at 10/10 on
        # 2026-08-19 even after the same-poll race had been fixed. A set can
        # be reconciled against the broker element-by-element instead of
        # being overwritten wholesale, so a lagging broker response can no
        # longer erase a position the bot knows it just opened.
        self._open_symbols = set()
        self._entry_recorded_at = {}  # symbol -> time.monotonic() when we recorded the entry
        self._pending_cost = {}  # symbol -> cost basis, for exposure while the broker lags

    def refresh_account_snapshot(self):
        """
        Pull current equity/buying-power/position-count/total-exposure from
        the broker (source of truth - not strategy.trades, which could in
        principle drift from what the broker actually holds) and cache it.
        Call this once per poll cycle, not once per symbol - it's two API
        calls, and doing it per-symbol across 65+ watched symbols every cycle
        would be wasteful and slow.
        """
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            self._equity = float(account.equity)
            self._buying_power = float(account.buying_power)

            # Reconcile rather than overwrite. The broker's list is the source
            # of truth for anything it reports, but it LAGS recent fills, so a
            # position we opened moments ago can legitimately be missing from
            # it. Those are carried over for a bounded grace period instead of
            # being dropped (see ENTRY_CONFIRM_GRACE_SECONDS). Symbols we've
            # exited were already discarded from _open_symbols, so this can
            # only preserve genuinely-open positions, never resurrect closed
            # ones - and the grace window means a position closed outside the
            # bot's own exit path still ages out on its own.
            broker_symbols = set(positions.keys())
            now = time.monotonic()
            unconfirmed = {
                symbol
                for symbol in self._open_symbols - broker_symbols
                if now - self._entry_recorded_at.get(symbol, 0.0) < ENTRY_CONFIRM_GRACE_SECONDS
            }
            if unconfirmed:
                logger.debug(
                    f"Broker position list lags {len(unconfirmed)} recent entry/entries "
                    f"({', '.join(sorted(unconfirmed))}) - counting them as open"
                )

            self._open_symbols = broker_symbols | unconfirmed
            self._entry_recorded_at = {
                s: t for s, t in self._entry_recorded_at.items() if s in self._open_symbols
            }
            self._pending_cost = {
                s: c for s, c in self._pending_cost.items() if s in unconfirmed
            }

            # Exposure gets the same treatment: the broker can only report a
            # market value for positions it knows about, so add back the cost
            # basis of the ones it hasn't caught up to yet.
            self._total_exposure_usd = sum(
                abs(float(p.market_value)) for p in positions.values()
            ) + sum(self._pending_cost.values())
        except Exception as e:
            logger.error(f"Error refreshing account snapshot: {e}")
            # Fail closed: on error, assume zero buying power and max exposure
            # so pre_entry_check blocks new entries rather than proceeding on
            # stale/unknown state. Equity and position count are left at their
            # last-known values since those aren't used to gate "can we buy" -
            # only buying_power and total_exposure_usd are, and both are
            # deliberately zeroed/maxed here.
            self._buying_power = 0.0
            self._total_exposure_usd = float("inf")

    @property
    def _open_position_count(self):
        """Number of open positions, derived from the reconciled symbol set."""
        return len(self._open_symbols)

    def pre_entry_check(self, qty, price):
        """
        Returns (ok: bool, reason: str). Checked BEFORE ever attempting a
        broker order for a new entry - closes the gap where only Alpaca's own
        margin rejection used to be the backstop against over-leveraging.
        Three independent checks, all must pass:
          1. Enough buying power for this specific order.
          2. Not already at max_concurrent_positions.
          3. Adding this position wouldn't push total committed capital past
             max_total_exposure_fraction of current equity.
        """
        cost = qty * price

        if cost > self._buying_power:
            return False, f"insufficient buying power (need ${cost:.2f}, have ${self._buying_power:.2f})"

        max_positions = self.config["trading"].get("max_concurrent_positions")
        if max_positions and self._open_position_count >= max_positions:
            return False, f"at max_concurrent_positions ({self._open_position_count}/{max_positions})"

        max_exposure_fraction = self.config["trading"].get("max_total_exposure_fraction")
        if max_exposure_fraction and self._equity > 0:
            max_exposure_usd = self._equity * max_exposure_fraction
            if self._total_exposure_usd + cost > max_exposure_usd:
                return False, (
                    f"would exceed max_total_exposure_fraction "
                    f"(${self._total_exposure_usd:.2f} committed + ${cost:.2f} > ${max_exposure_usd:.2f} cap)"
                )

        return True, ""

    def record_entry_meta(self, symbol, method, rsi, entry_time=None):
        """
        Record how/when a position was opened, independent of open_entries
        (which only holds price and is read by the P&L calc). Called for
        every entry path (three-bar momentum, pullback resumption, rapid
        increase, and reconciliation on restart) so submit_exit_order can
        attach it to that trade's row in the daily report.
        """
        self.entry_meta[symbol] = {
            "method": method,
            "rsi": rsi,
            "entry_time": entry_time or datetime.now().isoformat(),
        }

    def submit_entry_order(self, symbol, qty, price=None, entry_method=None, entry_rsi=None):
        """
        Submit a market order to enter a position. Returns the order on
        success, or None on failure (does NOT raise) - callers must check the
        return value and only commit the position to Strategy tracking
        (strategy.confirm_entry) when this returns non-None. This is the
        entry-side half of the phantom-entry fix: previously the caller
        recorded the position as open in Strategy BEFORE this was even
        called, so a failure here left an untracked phantom long that later
        became a real, invisible short. Returning None (not raising) makes
        "did it actually work" an explicit value the caller must check,
        rather than an exception that could be caught too late or in the
        wrong place.
        """
        try:
            order = self.broker.submit_market_order(symbol, qty, side="buy")
        except Exception as e:
            logger.error(f"Failed to submit entry order for {symbol}: {e}")
            return None

        self.open_entries[symbol] = price
        self.record_entry_meta(symbol, method=entry_method or "UNKNOWN", rsi=entry_rsi)

        self.order_history.append({
            "timestamp": datetime.now().isoformat(),
            "action": "ENTRY",
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "status": "SUBMITTED",
            "order_id": order.id if hasattr(order, "id") else None,
        })

        # Update the cached exposure snapshot immediately, not just on the next
        # refresh_account_snapshot() call - refresh only happens once per poll
        # cycle (deliberately, to avoid an API call per symbol), so without
        # this, multiple entries firing within the SAME poll (e.g. many
        # symbols moving together right at the open) would each check
        # pre_entry_check() against the same stale pre-poll snapshot and all
        # get approved, letting the account blow well past
        # max_concurrent_positions/max_total_exposure_fraction before the
        # next poll's fresh query ever catches up. This closes that race.
        if price:
            self._buying_power -= qty * price
            self._total_exposure_usd += qty * price
            self._pending_cost[symbol] = qty * price
        self._open_symbols.add(symbol)
        self._entry_recorded_at[symbol] = time.monotonic()

        logger.info(f"{ANSI_GREEN}Entry order submitted for {symbol}: {qty} shares at {price}{ANSI_RESET}")
        return order

    def submit_exit_order(self, symbol, qty, reason="", price=None, exit_rsi=None):
        """
        Submit a market order to exit a position and record a documented
        trade row. Returns the order on success, or None on failure (does NOT
        raise) - callers must check the return value and only commit the exit
        to Strategy tracking (strategy.confirm_exit) when this returns
        non-None, exactly mirroring submit_entry_order's contract. If a sell
        fails, the position must stay fully tracked so the same exit
        condition gets retried on the next poll cycle instead of the bot
        silently forgetting it still holds (and needs to protect) it.
        """
        try:
            order = self.broker.submit_market_order(symbol, qty, side="sell")
        except Exception as e:
            logger.error(f"Failed to submit exit order for {symbol}: {e}")
            return None

        # Mirror the immediate cache update in submit_entry_order, for the
        # same reason - keeps pre_entry_check() accurate for any entry
        # checked later in the SAME poll cycle, not just after the next
        # refresh_account_snapshot(). Only FIRST_EXIT_-0.5% is ever a partial
        # sale (see PARTIAL_EXIT_REASONS) - every other reason always sells
        # the entire remaining position, so the open-position count only
        # decrements for those.
        if price:
            self._buying_power += qty * price
            self._total_exposure_usd = max(0.0, self._total_exposure_usd - qty * price)
        if reason not in PARTIAL_EXIT_REASONS:
            self._open_symbols.discard(symbol)
            self._entry_recorded_at.pop(symbol, None)
            self._pending_cost.pop(symbol, None)

        try:
            entry_price = self.open_entries.get(symbol)
            meta = self.entry_meta.get(symbol, {})
            now_iso = datetime.now().isoformat()
            trade_record = {
                "timestamp": now_iso,
                "symbol": symbol,
                "entry_time": meta.get("entry_time"),
                "entry_price": entry_price,
                "entry_method": meta.get("method"),
                "entry_rsi": meta.get("rsi"),
                "exit_time": now_iso,
                "exit_price": price,
                "qty": qty,
                "exit_reason": reason,
                "exit_rsi": exit_rsi,
                "stop_loss_used": reason in STOP_LOSS_EXIT_REASONS,
                "order_id": order.id if hasattr(order, "id") else None,
            }
            if entry_price and price is not None:
                trade_record["pl"] = (price - entry_price) * qty
                trade_record["pl_pct"] = (price - entry_price) / entry_price * 100
            else:
                trade_record["pl"] = 0
                trade_record["pl_pct"] = 0
            self.trades_log.append(trade_record)

            self.order_history.append({
                "timestamp": trade_record["timestamp"],
                "action": "EXIT",
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "reason": reason,
                "status": "SUBMITTED",
                "order_id": trade_record["order_id"],
            })

            color = ANSI_YELLOW if reason in PARTIAL_EXIT_REASONS else ANSI_RED
            logger.info(
                f"{color}Exit order submitted for {symbol}: {qty} shares at {price} ({reason}) - "
                f"entry was {entry_price}, P&L: ${trade_record['pl']:.2f} ({trade_record['pl_pct']:+.2f}%){ANSI_RESET}"
            )
        except Exception as e:
            # The broker order above already succeeded - a failure here is a
            # bookkeeping bug (trades_log/order_history), not an order
            # failure. Still return the order so the caller correctly commits
            # the exit; a bookkeeping gap is far better than the alternative
            # (strategy thinking a genuinely-sold position is still held).
            logger.error(f"Exit order for {symbol} filled but record-keeping failed: {e}")

        return order

    def check_daily_loss_limit(self):
        """Check if we've exceeded max daily loss"""
        max_loss = self.config["trading"]["max_daily_loss_usd"]
        if self.daily_pnl <= -max_loss:
            logger.warning(f"Daily loss limit exceeded: {self.daily_pnl}")
            return True
        return False

    def get_daily_pnl(self):
        """Calculate unrealized + realized daily P&L"""
        try:
            account = self.broker.get_account()
            # Alpaca provides daily_pnl
            if hasattr(account, "daily_pnl"):
                self.daily_pnl = float(account.daily_pnl)
            return self.daily_pnl
        except Exception as e:
            logger.error(f"Error fetching daily PnL: {e}")
            return self.daily_pnl

    def flatten_all_positions(self):
        """
        Close all open positions at market. Returns the list of symbols that
        were ACTUALLY successfully flattened (order confirmed) - not just
        attempted. The Executor has no reference to Strategy and can't update
        strategy.trades itself, so the caller (run_trading_day, which holds
        both) MUST use this return value to reconcile strategy.trades after
        calling this - otherwise a flattened position stays stale in
        strategy.trades forever (this process reuses the same Strategy
        object across every trading day), and on a later day the exit-check
        loop will fire another sell against a position that's already been
        closed - the exact same phantom-position-becomes-a-real-short
        failure mode the entry-side fix addressed, via a different path.
        Only symbols that actually confirmed are included, so a partial
        failure correctly leaves that one symbol still tracked for retry.
        """
        flattened_symbols = []
        try:
            positions = self.broker.get_positions()

            for symbol, position in positions.items():
                qty = int(abs(float(position.qty)))
                if qty > 0:
                    # Use the broker's own fill data as the entry price if this
                    # executor didn't track the entry itself (e.g. a position
                    # left over from an earlier process) so P&L is still accurate.
                    if symbol not in self.open_entries or not self.open_entries[symbol]:
                        avg_entry = getattr(position, "avg_entry_price", None)
                        self.open_entries[symbol] = float(avg_entry) if avg_entry else None
                    if symbol not in self.entry_meta:
                        self.record_entry_meta(symbol, method="RECONCILED", rsi=None)
                    current_price = getattr(position, "current_price", None)
                    price = float(current_price) if current_price else None

                    order = self.submit_exit_order(symbol, qty, "FLATTEN_ALL", price)
                    if order is not None:
                        flattened_symbols.append(symbol)
                        logger.info(f"Flattened {symbol}: {qty} shares at {price}")
                    else:
                        logger.error(f"Failed to flatten {symbol}: {qty} shares - order was not submitted")

            return flattened_symbols
        except Exception as e:
            logger.error(f"Error flattening positions: {e}")
            raise

    def save_trades_log(self, filepath="logs/trades.json"):
        """
        Save today's trades log to file (overwritten each day - this is what
        email_notifier reads for the same-day summary) AND append every trade
        from today to a running, never-overwritten master CSV
        (logs/trade_history.csv) - one row per completed trade across every
        trading day this bot has ever run, for cross-day analysis.
        """
        try:
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w") as f:
                json.dump(self.trades_log, f, indent=2)
            logger.info(f"Trades log saved to {filepath}")
        except Exception as e:
            logger.error(f"Error saving trades log: {e}")

        self._append_trade_history_csv()

    def _append_trade_history_csv(self, filepath="logs/trade_history.csv"):
        """Append only NEW trades_log rows (since the last call) to the
        running master CSV, one row per completed trade. trades_log itself is
        never cleared across days in a long-running process, so _csv_appended_count
        tracks how much has already been written to avoid re-appending the
        same trades on every subsequent day's save_trades_log() call."""
        new_trades = self.trades_log[self._csv_appended_count:]
        if not new_trades:
            return

        fieldnames = [
            "date", "symbol", "entry_time", "entry_price", "entry_method", "entry_rsi",
            "exit_time", "exit_price", "exit_reason", "stop_loss_used", "exit_rsi",
            "qty", "pl", "pl_pct",
        ]
        try:
            path = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not path.exists() or path.stat().st_size == 0

            with open(path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                for trade in new_trades:
                    row = dict(trade)
                    row["date"] = (trade.get("exit_time") or trade.get("timestamp") or "")[:10]
                    writer.writerow(row)
            self._csv_appended_count = len(self.trades_log)
            logger.info(f"Appended {len(new_trades)} trade(s) to {filepath}")
        except Exception as e:
            logger.error(f"Error appending trade history CSV: {e}")
