import logging
import json
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

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

    def submit_entry_order(self, symbol, qty, price=None):
        """Submit a market order to enter a position"""
        try:
            order = self.broker.submit_market_order(symbol, qty, side="buy")
            self.open_entries[symbol] = price

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

    def submit_exit_order(self, symbol, qty, reason="", price=None):
        """Submit a market order to exit a position and record a documented trade row"""
        try:
            order = self.broker.submit_market_order(symbol, qty, side="sell")

            entry_price = self.open_entries.get(symbol)
            trade_record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": price,
                "qty": qty,
                "exit_reason": reason,
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
                return self.submit_entry_order(symbol, qty, price)
            elif action in ["EXIT_ALL", "PARTIAL_EXIT"]:
                return self.submit_exit_order(symbol, qty, reason, price)
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
        """Save trades log to file"""
        try:
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w") as f:
                json.dump(self.trades_log, f, indent=2)
            logger.info(f"Trades log saved to {filepath}")
        except Exception as e:
            logger.error(f"Error saving trades log: {e}")
