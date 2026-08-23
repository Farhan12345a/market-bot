import pandas as pd
import logging
import pytz
from enum import Enum
from datetime import datetime, timedelta
import numpy as np

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


def _now_et():
    """
    Current time in US market time.

    Every hour-of-day threshold in config (time_stop_hour, momentum_fade_hour)
    is documented and intended as ET, but these checks used a naive
    datetime.now(), which is the SERVER's local clock. The production VPS runs
    UTC, so on 2026-08-19 both gates were evaluated four hours ahead of where
    they were meant to sit:

      momentum_fade_hour: 10  ->  10:15 UTC = 06:15 ET, i.e. already elapsed
                                  before the opening bell, so momentum-fade
                                  was live from the first bar of the session
                                  instead of being dormant until 10:15 ET.
                                  All 17 of that day's momentum-fade exits
                                  fired between 09:38 and 09:52 ET, inside
                                  the window the rule was supposed to be off.
      time_stop_hour: 16      ->  16:00 UTC = 12:00 ET, closing every open
                                  position at noon rather than 4pm.
    """
    return datetime.now(ET)

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
        self.entry_time = _now_et()
        self.highest_price = entry_price
        self.first_exit_done = False
        self.take_profit_done = False

        # For momentum and resistance detection
        self.price_history = [entry_price]  # Track prices for momentum calc
        self.highest_since_entry = entry_price  # Track peak for resistance
        # Excursion tracking, for post-trade analysis rather than any exit
        # decision. MFE is the best unrealized gain a position ever reached and
        # MAE the worst drawdown - the pair that answers "was this loser ever
        # actually winning?". Both were previously computable only by
        # re-fetching minute bars after the fact, so no log, CSV or report
        # could show them.
        self.lowest_since_entry = entry_price

        self.orders_log = []

    def check_first_exit(self, current_price):
        """
        Check if we should sell 33% at -0.5% loss. Pure check - does NOT set
        first_exit_done (that only happens in process_exit, once the broker
        has actually confirmed the sell filled). Setting it here, before
        confirmation, would permanently disable the first-exit tranche for
        this position if the order ever failed to submit.
        """
        loss_pct = (current_price - self.entry_price) / self.entry_price
        first_exit_trigger = self.config["trading"]["first_exit_loss_pct"] / 100

        if not self.first_exit_done and loss_pct <= first_exit_trigger:
            return int(self.entry_qty * self.config["trading"]["first_exit_pct"])
        return 0

    def check_take_profit(self, current_price):
        """
        Scale out of a WINNER: sell take_profit_fraction of the original
        position once price is up take_profit_pct. Fires at most once per
        position (take_profit_done, set only in process_exit after the broker
        confirms - same commit-after-confirmation discipline as first_exit).

        EXPERIMENTAL, added 2026-08-20 at the user's request, and the 2026-08-20
        data argues against it: only 3 of 25 trades that day ever reached +1%,
        and those three (MARA +$121, RIOT +$59, LYFT +$44) were the entire
        day's profit. Trimming half of each would have clipped the only
        winners while doing nothing for the other 22 trades. Watch
        take_profit_pct vs the MFE column before keeping this.
        """
        if not self.config["trading"].get("use_take_profit", False):
            return 0
        if self.take_profit_done:
            return 0

        trigger = self.config["trading"].get("take_profit_pct", 1.0) / 100
        gain_pct = (current_price - self.entry_price) / self.entry_price
        if gain_pct < trigger:
            return 0

        fraction = self.config["trading"].get("take_profit_fraction", 0.5)
        qty = int(self.entry_qty * fraction)
        # Never try to sell more than is still held (a first-exit tranche may
        # already have gone), and never emit a 0-share order.
        return max(0, min(qty, self.qty_remaining))

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
        now = _now_et()

        if now.hour >= time_stop_hour:
            return self.qty_remaining
        return 0

    def calculate_momentum(self):
        """
        Calculate momentum: slope of a linear regression fit over the last N
        price samples (N = momentum_fade_window_samples in config). Each sample
        is one exit-check pass (~once per minute in live trading, see
        run_exit_monitoring's 60s check gate), so window=5 means "the trend
        over roughly the last 4-5 minutes." The x-axis is the sample INDEX
        (0, 1, 2, ...), not elapsed seconds, so the slope is $ per sample,
        which only approximates $ per minute for as long as checks land close
        to every 60s.
        """
        window = self.config["trading"].get("momentum_fade_window_samples", 5)
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
        """Check if momentum is fading (configurable time, window, and threshold)"""
        now = _now_et()

        # Only check momentum fade after configured time (default: 10:15 AM)
        momentum_fade_hour = self.config["trading"].get("momentum_fade_hour", 10)
        momentum_fade_min = self.config["trading"].get("momentum_fade_minute", 15)

        # Compare as minutes since midnight for easier comparison
        current_minutes = now.hour * 60 + now.minute
        fade_start_minutes = momentum_fade_hour * 60 + momentum_fade_min

        if current_minutes < fade_start_minutes:
            return 0

        momentum = self.calculate_momentum()
        slope_threshold = self.config["trading"].get("momentum_fade_slope_threshold", 0.0001)

        # If momentum is negative or very weak (slope <= threshold), exit
        if momentum is not None and momentum < slope_threshold:
            logger.info(f"{self.symbol}: Momentum fading (slope: {momentum:.6f}), exiting")
            return self.qty_remaining

        return 0

    def check_resistance(self, current_price):
        """
        Failed-breakout exit: the oldest sample in the lookback is the peak,
        every sample since has been strictly lower, AND the total decline off
        that peak exceeds resistance_min_decline_pct.

        That last condition is the fix for this rule's real defect. The
        decline test is `recent[i] < recent[i-1]`, which a single cent
        satisfies, so with no magnitude floor the rule fired on pure noise -
        on 2026-08-19 it exited NFLX on a 0.08% drop, UBER on 0.11% and ASTS
        on 0.14%, all inside the bid-ask spread. It was the second most
        common exit reason that day (14 of 71) and closed several positions
        at a profit of a tenth of a percent.

        It fires so readily because price_history is SEEDED with the entry
        price, so `recent[0] == highest_since_entry` is satisfied by default
        for any position that never traded above its fill - and this strategy
        buys thrusts, which frequently means buying the local top. Two
        down-ticks after entry were therefore enough to trigger a full exit
        two minutes in. Requiring a real decline is what separates "the
        breakout failed" from "the price wobbled".
        """
        lookback = self.config["trading"].get("resistance_lookback_samples", 3)
        if len(self.price_history) < lookback:
            return 0

        recent = self.price_history[-lookback:]
        monotonic_decline = all(recent[i] < recent[i - 1] for i in range(1, len(recent)))

        if not (monotonic_decline and recent[0] == self.highest_since_entry):
            return 0

        peak = recent[0]
        min_decline = self.config["trading"].get("resistance_min_decline_pct", 0.0)
        decline_pct = (peak - current_price) / peak * 100 if peak > 0 else 0.0

        if decline_pct < min_decline:
            return 0

        logger.info(
            f"{self.symbol}: Resistance detected (high: {peak:.2f}, "
            f"now: {current_price:.2f}, -{decline_pct:.2f}%), exiting"
        )
        return self.qty_remaining

    def excursions(self):
        """
        (mfe_pct, mae_pct) - best and worst unrealized moves seen since entry,
        as % of entry price. Observational only; nothing gates on these.
        """
        if not self.entry_price:
            return None, None
        mfe = (self.highest_since_entry - self.entry_price) / self.entry_price * 100
        mae = (self.lowest_since_entry - self.entry_price) / self.entry_price * 100
        return round(mfe, 4), round(mae, 4)

    def process_exit(self, qty_to_exit, exit_reason):
        """Record a CONFIRMED exit - call only after the broker has actually
        filled the sell order. This is where first_exit_done actually gets
        set (see check_first_exit's docstring for why)."""
        self.qty_remaining -= qty_to_exit
        if exit_reason == "FIRST_EXIT_-0.5%":
            self.first_exit_done = True
        if exit_reason == "TAKE_PROFIT":
            self.take_profit_done = True
        self.orders_log.append({
            "action": "EXIT",
            "qty": qty_to_exit,
            "reason": exit_reason,
            "time": _now_et(),
        })
        logger.info(f"{self.symbol}: Exiting {qty_to_exit} shares ({exit_reason})")

class Strategy:
    """Main strategy logic"""

    def __init__(self, config):
        self.config = config
        self.trades = {}  # symbol -> TradeManager

    def check_rapid_increase_entry(self, symbol, price_now, price_then):
        """
        Entry signal: price rose by at least `rapid_increase_pct` between
        price_then (start of the lookback window) and price_now (latest sample).

        Returns: (qty, pct_change). qty is 0 if no entry signal.
        """
        if symbol in self.trades or price_then <= 0:
            return 0, 0.0

        pct_threshold = self.config["trading"]["rapid_increase_pct"]
        pct_change = (price_now - price_then) / price_then * 100

        if pct_change >= pct_threshold:
            max_position_usd = self.config["trading"]["max_position_per_stock_usd"]
            qty = int(max_position_usd / price_now)
            return qty, pct_change

        return 0, pct_change

    def can_enter(self, symbol, qty):
        """Pure eligibility check - no state mutation. Symbol must not already
        be tracked and qty must be positive."""
        return symbol not in self.trades and qty > 0

    def confirm_entry(self, symbol, price, qty):
        """
        Commit a new position to internal tracking. Call this ONLY after the
        broker has confirmed the entry order actually filled - never before.

        Calling this before confirmation was the root cause of the 2026-08-18
        phantom-entry bug: the old enter_trade() committed the position here
        unconditionally, before the broker call even happened. When a buy
        later failed (e.g. insufficient buying power), the bot still believed
        it held a long position. When exit logic eventually fired against
        that phantom long, the resulting SELL order - for shares that were
        never actually bought - was accepted by Alpaca's margin account as
        opening a real, completely untracked SHORT position. 16 of them
        happened this way in one session before being caught.
        """
        self.trades[symbol] = TradeManager(symbol, price, qty, self.config)
        logger.info(f"{symbol}: Entered {qty} shares at {price}")

    def correct_entry_price(self, symbol, actual):
        """
        Rebase an open position onto the price actually PAID.

        submit_entry_order records the price the signal fired at; the broker's
        avg_entry_price is what was really paid, and it arrives a poll later.
        The executor already reconciled its own copy, but until 2026-08-21 it
        never told the strategy - so every exit rule kept measuring against the
        signal price for the life of the position.

        That is not a rounding difference. On 2026-08-21 mean adverse slippage
        was +0.42%, so a "-1.0%" final exit was really firing around -1.4% from
        the true cost, and MARA's take-profit fired at a genuine +0.07% because
        12.29 is +1.28% above the signal price of 12.135 but only +0.07% above
        the 12.2817 actually paid. Stops fired late and profit targets fired
        early, every time the fill missed.

        The peak/trough trackers are rebased too: they were seeded from the
        signal price, so a position that gapped away from its signal would
        otherwise carry a high-water mark it never actually traded at.
        """
        if symbol not in self.trades:
            return False
        try:
            actual = float(actual)
        except (TypeError, ValueError):
            return False
        if actual <= 0:
            return False

        trade = self.trades[symbol]
        if abs(trade.entry_price - actual) <= 1e-6:
            return False

        old_price = trade.entry_price
        trade.entry_price = actual
        trade.highest_since_entry = max(trade.highest_since_entry, actual)
        trade.lowest_since_entry = min(trade.lowest_since_entry, actual)
        trade.highest_price = max(trade.highest_price, actual)

        logger.info(
            f"{symbol}: exit rules rebased to the actual fill "
            f"{old_price:.4f} -> {actual:.4f} "
            f"({(actual - old_price) / old_price * 100:+.2f}%)"
        )
        return True

    def check_exit(self, symbol, current_bar):
        """
        Check whether `symbol`'s open position should exit, in priority order.
        Updates lightweight tracking bookkeeping (price_history, highest_since_entry)
        inline since that's harmless observation, not a commitment - but does
        NOT decrement qty_remaining or remove the trade from self.trades. That
        only happens in confirm_exit, after the broker confirms the sell filled.

        Returns an exit_info dict ({"qty", "reason", "price"}) or None.
        """
        if symbol not in self.trades:
            return None

        trade = self.trades[symbol]
        current_price = current_bar.get("close", 0)

        trade.price_history.append(current_price)
        if current_price > trade.highest_since_entry:
            trade.highest_since_entry = current_price
        if current_price < trade.lowest_since_entry:
            trade.lowest_since_entry = current_price

        checks = [
            ("FINAL_EXIT_-1.0%", trade.check_final_exit),
            ("FIRST_EXIT_-0.5%", trade.check_first_exit),
            ("TAKE_PROFIT", trade.check_take_profit),
            ("MOMENTUM_FADE", trade.check_momentum_fade),
            ("RESISTANCE", trade.check_resistance),
            ("TRAILING_STOP", trade.update_trailing_stop),
            ("TIME_STOP_4PM", lambda price: trade.check_time_exit()),
        ]
        for reason, check_fn in checks:
            qty = check_fn(current_price)
            if qty > 0:
                return {"qty": qty, "reason": reason, "price": current_price}

        return None

    def confirm_exit(self, symbol, qty, reason, price):
        """
        Commit a CONFIRMED exit - call ONLY after the broker has actually
        filled the sell order. If the broker order fails instead, don't call
        this: the position stays fully tracked exactly as before, and the
        same exit condition will simply be re-checked (and retried) on the
        next poll cycle rather than silently vanishing from tracking while
        still genuinely open at the broker.
        """
        trade = self.trades[symbol]
        trade.process_exit(qty, reason)
        if trade.qty_remaining == 0:
            del self.trades[symbol]
            return {"action": "EXIT_ALL", "symbol": symbol, "qty": qty, "reason": reason, "price": price}
        else:
            return {"action": "PARTIAL_EXIT", "symbol": symbol, "qty": qty, "reason": reason, "price": price}

    def get_open_trades(self):
        """Return all open trades"""
        return self.trades.copy()
