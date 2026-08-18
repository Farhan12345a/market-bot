import csv
import logging
import json
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Exit reasons that represent a protective stop firing (vs. a discretionary/
# time-based exit) - used to fill the "was a stop-loss used" column in the
# daily trade report.
STOP_LOSS_EXIT_REASONS = {"FINAL_EXIT_-1.0%", "FIRST_EXIT_-0.5%", "TRAILING_STOP", "FLATTEN_ALL"}

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
        """Submit a market order to enter a position"""
        try:
            order = self.broker.submit_market_order(symbol, qty, side="buy")
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

            logger.info(f"Entry order submitted for {symbol}: {qty} shares at {price}")
            return order
        except Exception as e:
            logger.error(f"Failed to submit entry order for {symbol}: {e}")
            raise

    def submit_exit_order(self, symbol, qty, reason="", price=None, exit_rsi=None):
        """Submit a market order to exit a position and record a documented trade row"""
        try:
            order = self.broker.submit_market_order(symbol, qty, side="sell")

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

            logger.info(
                f"Exit order submitted for {symbol}: {qty} shares at {price} ({reason}) - "
                f"entry was {entry_price}, P&L: ${trade_record['pl']:.2f} ({trade_record['pl_pct']:+.2f}%)"
            )
            return order
        except Exception as e:
            logger.error(f"Failed to submit exit order for {symbol}: {e}")
            raise

    def execute_signal(self, signal):
        """Execute a trade signal from the strategy"""
        if signal is None:
            return None

        action = signal.get("action")
        symbol = signal.get("symbol")
        qty = signal.get("qty")
        reason = signal.get("reason", "")
        price = signal.get("price")

        try:
            if action == "ENTRY":
                return self.submit_entry_order(
                    symbol, qty, price,
                    entry_method=signal.get("entry_method"),
                    entry_rsi=signal.get("entry_rsi"),
                )
            elif action in ["EXIT_ALL", "PARTIAL_EXIT"]:
                return self.submit_exit_order(symbol, qty, reason, price, exit_rsi=signal.get("exit_rsi"))
        except Exception as e:
            logger.error(f"Error executing signal: {e}")
            raise

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
        """Close all open positions at market"""
        try:
            positions = self.broker.get_positions()
            closed_orders = []

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
                    closed_orders.append(order)
                    logger.info(f"Flattened {symbol}: {qty} shares at {price}")

            return closed_orders
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
