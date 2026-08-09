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
        self.trades_log = []
        self.daily_pnl = 0.0
        self.order_history = []

    def submit_entry_order(self, symbol, qty):
        """Submit a market order to enter a position"""
        try:
            order = self.broker.submit_market_order(symbol, qty, side="buy")

            trade_record = {
                "timestamp": datetime.now().isoformat(),
                "action": "ENTRY",
                "symbol": symbol,
                "qty": qty,
                "status": "SUBMITTED",
                "order_id": order.id if hasattr(order, "id") else None,
            }
            self.trades_log.append(trade_record)
            self.order_history.append(trade_record)

            logger.info(f"Entry order submitted for {symbol}: {qty} shares")
            return order
        except Exception as e:
            logger.error(f"Failed to submit entry order for {symbol}: {e}")
            raise

    def submit_exit_order(self, symbol, qty, reason=""):
        """Submit a market order to exit a position"""
        try:
            order = self.broker.submit_market_order(symbol, qty, side="sell")

            trade_record = {
                "timestamp": datetime.now().isoformat(),
                "action": "EXIT",
                "symbol": symbol,
                "qty": qty,
                "reason": reason,
                "status": "SUBMITTED",
                "order_id": order.id if hasattr(order, "id") else None,
            }
            self.trades_log.append(trade_record)
            self.order_history.append(trade_record)

            logger.info(f"Exit order submitted for {symbol}: {qty} shares ({reason})")
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

        try:
            if action == "ENTRY":
                return self.submit_entry_order(symbol, qty)
            elif action in ["EXIT_ALL", "PARTIAL_EXIT"]:
                return self.submit_exit_order(symbol, qty, reason)
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
                    order = self.submit_exit_order(symbol, qty, "FLATTEN_ALL")
                    closed_orders.append(order)
                    logger.info(f"Flattened {symbol}: {qty} shares")

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
