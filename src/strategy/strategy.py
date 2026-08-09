import pandas as pd
import logging
from enum import Enum
from datetime import datetime, timedelta
import numpy as np

logger = logging.getLogger(__name__)

class TradeState(Enum):
    FLAT = "flat"
    ENTRY_PENDING = "entry_pending"
    LONG = "long"
    EXITING = "exiting"

class TradeManager:
    """Manages entry and exit logic for individual positions"""

    def __init__(self, symbol, entry_price, qty, config):
        self.symbol = symbol
        self.entry_price = entry_price
        self.entry_qty = qty
        self.qty_remaining = qty
        self.config = config

        self.state = TradeState.LONG
        self.entry_time = datetime.now()
        self.highest_price = entry_price
        self.first_exit_done = False

        # For momentum and resistance detection
        self.price_history = [entry_price]  # Track prices for momentum calc
        self.highest_since_entry = entry_price  # Track peak for resistance

        self.orders_log = []

    def check_first_exit(self, current_price):
        """Check if we should sell 33% at -0.5% loss"""
        loss_pct = (current_price - self.entry_price) / self.entry_price
        first_exit_trigger = self.config["trading"]["first_exit_loss_pct"] / 100

        if not self.first_exit_done and loss_pct <= first_exit_trigger:
            qty_to_sell = int(self.entry_qty * self.config["trading"]["first_exit_pct"])
            self.first_exit_done = True
            return qty_to_sell
        return 0

    def check_final_exit(self, current_price):
        """Check if we should sell all remaining at -1.0% loss"""
        loss_pct = (current_price - self.entry_price) / self.entry_price
        final_exit_trigger = self.config["trading"]["final_exit_loss_pct"] / 100

        if loss_pct <= final_exit_trigger:
            return self.qty_remaining
        return 0

    def update_trailing_stop(self, current_price):
        """Update highest price and check trailing stop"""
        if current_price > self.highest_price:
            self.highest_price = current_price

        trail_pct = self.config["trading"]["trailing_stop_pct"] / 100
        trail_level = self.highest_price * (1 - trail_pct)

        if current_price <= trail_level:
            return self.qty_remaining  # Exit all on trailing stop
        return 0

    def check_time_exit(self):
        """Check if we've hit the time stop"""
        time_stop_hour = self.config["trading"]["time_stop_hour"]
        now = datetime.now()

        if now.hour >= time_stop_hour:
            return self.qty_remaining
        return 0

    def calculate_momentum(self, window=5):
        """Calculate momentum (slope of last N prices)"""
        if len(self.price_history) < window:
            return None

        recent_prices = self.price_history[-window:]
        x = np.arange(len(recent_prices))
        y = np.array(recent_prices)

        # Simple linear regression slope
        if len(y) > 1:
            slope = np.polyfit(x, y, 1)[0]
            return slope
        return None

    def check_momentum_fade(self, current_price):
        """Check if momentum is fading (configurable time)"""
        now = datetime.now()

        # Only check momentum fade after configured time (default: 10:15 AM)
        momentum_fade_hour = self.config["trading"].get("momentum_fade_hour", 10)
        momentum_fade_min = self.config["trading"].get("momentum_fade_minute", 15)

        # Compare as minutes since midnight for easier comparison
        current_minutes = now.hour * 60 + now.minute
        fade_start_minutes = momentum_fade_hour * 60 + momentum_fade_min

        if current_minutes < fade_start_minutes:
            return 0

        momentum = self.calculate_momentum(window=5)

        # If momentum is negative or very weak, exit
        if momentum is not None and momentum < 0.0001:
            logger.info(f"{self.symbol}: Momentum fading (slope: {momentum:.6f}), exiting")
            return self.qty_remaining

        return 0

    def check_resistance(self, current_price):
        """Check if price hit resistance and bounced back"""
        # Resistance is when price approaches the high but can't break through
        # and then pulls back

        if len(self.price_history) < 3:
            return 0

        recent_3 = self.price_history[-3:]

        # Pattern: high, lower, even lower = failed breakout, exit
        if (recent_3[-1] < recent_3[-2] < recent_3[-3] and
            recent_3[-3] == self.highest_since_entry):
            logger.info(
                f"{self.symbol}: Resistance detected (high: {recent_3[-3]:.2f}, "
                f"now: {current_price:.2f}), exiting"
            )
            return self.qty_remaining

        return 0

    def process_exit(self, qty_to_exit, exit_reason):
        """Record an exit"""
        self.qty_remaining -= qty_to_exit
        self.orders_log.append({
            "action": "EXIT",
            "qty": qty_to_exit,
            "reason": exit_reason,
            "time": datetime.now(),
        })
        logger.info(f"{self.symbol}: Exiting {qty_to_exit} shares ({exit_reason})")

class Strategy:
    """Main strategy logic"""

    def __init__(self, config):
        self.config = config
        self.trades = {}  # symbol -> TradeManager

    def check_entry_signal(self, symbol, current_bar, avg_volume):
        """
        Entry signal: volume spike + price above open

        Returns: qty to enter, or 0 if no entry
        """
        volume_spike_multiplier = self.config["trading"]["volume_spike_multiplier"]
        current_volume = current_bar.get("volume", 0)
        current_close = current_bar.get("close", 0)
        current_open = current_bar.get("open", 0)

        # Check volume spike
        volume_spiked = current_volume >= avg_volume * volume_spike_multiplier

        # Check price above open
        price_above_open = current_close > current_open

        if volume_spiked and price_above_open:
            # Calculate qty based on position size
            max_position_usd = self.config["trading"]["max_position_per_stock_usd"]
            qty = int(max_position_usd / current_close)
            logger.info(
                f"{symbol}: Entry signal triggered. Volume: {current_volume} "
                f"(avg: {avg_volume}, spike: {volume_spike_multiplier}x), "
                f"Price: {current_close} > {current_open}"
            )
            return qty
        return 0

    def process_bar(self, symbol, current_bar, avg_volume):
        """Process a new bar for a symbol"""
        current_price = current_bar.get("close", 0)

        # Check for new entry
        if symbol not in self.trades:
            qty = self.check_entry_signal(symbol, current_bar, avg_volume)
            if qty > 0:
                self.trades[symbol] = TradeManager(
                    symbol, current_price, qty, self.config
                )
                logger.info(f"{symbol}: Entered {qty} shares at {current_price}")
                return {"action": "ENTRY", "symbol": symbol, "qty": qty, "price": current_price}

        # Process active trade
        if symbol in self.trades:
            trade = self.trades[symbol]

            # Update price history for momentum calculation
            trade.price_history.append(current_price)
            if current_price > trade.highest_since_entry:
                trade.highest_since_entry = current_price

            # Check exits in priority order
            exits = []

            # 1. Check final exit first (most aggressive)
            qty = trade.check_final_exit(current_price)
            if qty > 0:
                exits.append({"qty": qty, "reason": "FINAL_EXIT_-1.0%"})

            # 2. Check first partial exit
            if not exits:
                qty = trade.check_first_exit(current_price)
                if qty > 0:
                    exits.append({"qty": qty, "reason": "FIRST_EXIT_-0.5%"})

            # 3. Check momentum fade (around 10 AM)
            if not exits:
                qty = trade.check_momentum_fade(current_price)
                if qty > 0:
                    exits.append({"qty": qty, "reason": "MOMENTUM_FADE"})

            # 4. Check resistance rejection
            if not exits:
                qty = trade.check_resistance(current_price)
                if qty > 0:
                    exits.append({"qty": qty, "reason": "RESISTANCE"})

            # 5. Check trailing stop
            if not exits:
                qty = trade.update_trailing_stop(current_price)
                if qty > 0:
                    exits.append({"qty": qty, "reason": "TRAILING_STOP"})

            # 6. Check time stop (full day close at 4 PM)
            if not exits:
                qty = trade.check_time_exit()
                if qty > 0:
                    exits.append({"qty": qty, "reason": "TIME_STOP_4PM"})

            if exits:
                for exit_info in exits:
                    trade.process_exit(exit_info["qty"], exit_info["reason"])
                    if trade.qty_remaining == 0:
                        del self.trades[symbol]
                        return {
                            "action": "EXIT_ALL",
                            "symbol": symbol,
                            "qty": exit_info["qty"],
                            "reason": exit_info["reason"],
                            "price": current_price,
                        }
                    else:
                        return {
                            "action": "PARTIAL_EXIT",
                            "symbol": symbol,
                            "qty": exit_info["qty"],
                            "reason": exit_info["reason"],
                            "price": current_price,
                        }

        return None

    def get_open_trades(self):
        """Return all open trades"""
        return self.trades.copy()
