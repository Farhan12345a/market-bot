import pandas as pd
import logging
import pytz
from enum import Enum
from datetime import datetime, timedelta
import numpy as np

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Tolerance for take-profit tier comparisons. Far below any price move that
# matters (1e-9 of a percent) and far above float representation error.
TIER_EPSILON = 1e-9


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

def _samples_for_minutes(config, minutes_key, samples_key, default_minutes, default_samples):
    """
    Convert a time window into a number of price samples at the CURRENT poll rate.

    These windows used to be raw sample counts, which silently meant whatever
    the poll interval happened to be. At a 60s poll, 6 samples was 6 minutes; at
    a 10s poll the same 6 samples is 60 SECONDS - the identical config, six times
    more sensitive, re-creating the hair-trigger exits fixed on 2026-08-20 where
    RESISTANCE fired 14 times on moves as small as 0.08%.

    Expressing the window in minutes makes it invariant to poll rate: change the
    interval and the rule keeps meaning the same span of market time.

    The sample-count key is still honoured when the minutes key is absent, so an
    older config behaves exactly as before.
    """
    trading = config["trading"]
    minutes = trading.get(minutes_key)
    if minutes is None:
        return trading.get(samples_key, default_samples)

    interval = trading.get("entry_check_interval_seconds", 60) or 60
    # At least 2 samples: a trend or a decline needs two points to exist at all.
    return max(2, round(float(minutes) * 60.0 / float(interval)))


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
        # When the position last made a NEW HIGH. Entry counts as the first
        # one, so a trade that never goes green is "stalled" from the moment
        # stall_after_minutes elapses - which is correct: it has had its time
        # and done nothing with it.
        self.last_high_at = self.entry_time
        self.first_exit_done = False
        # Indices of take-profit tiers already filled for this position. A set
        # rather than a single flag, because each tier fires at most once but
        # several can fire over the life of one position.
        self.take_profit_tiers_done = set()

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

    def take_profit_tiers(self):
        """
        Configured scale-out tiers, lowest gain first, normalised and validated.

        Falls back to the single-tier form (take_profit_pct /
        take_profit_fraction) when take_profit_tiers is absent, so an older
        config keeps working unchanged.
        """
        cfg = self.config["trading"]
        raw = cfg.get("take_profit_tiers")
        if not raw:
            return [{
                "gain_pct": cfg.get("take_profit_pct", 1.0),
                "sell_fraction": cfg.get("take_profit_fraction", 0.5),
            }]

        tiers = []
        for entry in raw:
            try:
                gain = float(entry["gain_pct"])
                frac = float(entry["sell_fraction"])
            except (KeyError, TypeError, ValueError):
                logger.warning(f"{self.symbol}: ignoring malformed take-profit tier {entry!r}")
                continue
            if gain <= 0 or frac <= 0:
                logger.warning(f"{self.symbol}: ignoring non-positive take-profit tier {entry!r}")
                continue
            tiers.append({"gain_pct": gain, "sell_fraction": frac})

        tiers.sort(key=lambda t: t["gain_pct"])
        return tiers

    def check_take_profit(self, current_price):
        """
        Scale out of a WINNER across several gain tiers.

        Returns (qty, reason) - unlike the other checks, which return a bare
        qty - because the caller needs to know WHICH tier fired, both to name
        the exit in the report and to mark the right tier as spent.

        Rules, and the reasoning behind each:

        - The HIGHEST tier whose threshold is met fires, not the lowest. A gap
          straight through +1.5% should sell what a +1.5% move deserves, not
          trickle out 33% and leave the rest to a rule that may never fire again.
        - Firing a tier retires every tier below it. Otherwise a position that
          jumped to +1.3% and pulled back to +1.0% would sell AGAIN on the way
          down, which is scaling into weakness, not out of strength.
        - sell_fraction is a fraction of the ORIGINAL position, so 0.33 + 0.40
          means 73% of what was bought - stable regardless of what earlier
          tranches took. A fraction >= 1.0 means "all remaining", which is how
          the top tier closes the position outright.
        - Never sells more than is still held, and never emits a zero-share
          order.

        Fires at most once per tier per position, and only ever commits in
        process_exit after the broker confirms - the same discipline as
        first_exit.
        """
        if not self.config["trading"].get("use_take_profit", False):
            return 0, None

        gain_pct = (current_price - self.entry_price) / self.entry_price * 100
        tiers = self.take_profit_tiers()

        for idx in range(len(tiers) - 1, -1, -1):
            if idx in self.take_profit_tiers_done:
                continue
            tier = tiers[idx]
            # Epsilon, not a bare <. A price computed as exactly the target
            # lands on 1.4999999999999998 in binary floating point, so a strict
            # comparison silently skips the tier that should fire. With tiers
            # only 0.25% apart that does not merely delay the exit - it fires
            # the WRONG tier, selling 40% where the whole position was intended.
            if gain_pct < tier["gain_pct"] - TIER_EPSILON:
                continue

            if tier["sell_fraction"] >= 1.0:
                qty = self.qty_remaining
            else:
                qty = int(self.entry_qty * tier["sell_fraction"])
            qty = max(0, min(qty, self.qty_remaining))
            if qty <= 0:
                return 0, None

            return qty, f"TAKE_PROFIT_{tier['gain_pct']:g}%"

        return 0, None

    def _breakeven_tiers(self):
        """
        [(trigger_pct, floor_pct)] highest trigger first.

        Reads breakeven_tiers when present, otherwise synthesises the single
        tier from breakeven_trigger_pct/breakeven_floor_pct so an older config
        keeps its exact previous behaviour.
        """
        trading = self.config["trading"]
        tiers = trading.get("breakeven_tiers")
        if not tiers:
            return [(trading.get("breakeven_trigger_pct", 0.5),
                     trading.get("breakeven_floor_pct", 0.05))]
        out = []
        for tier in tiers:
            try:
                out.append((float(tier["trigger_pct"]), float(tier["floor_pct"])))
            except (KeyError, TypeError, ValueError):
                logger.warning(f"{self.symbol}: ignoring malformed breakeven tier {tier!r}")
        if not out:
            return [(trading.get("breakeven_trigger_pct", 0.5),
                     trading.get("breakeven_floor_pct", 0.05))]
        return sorted(out, reverse=True)

    def check_breakeven_stop(self, current_price):
        """
        Once a position has proven itself, never let it become a loser.

        Arms when the PEAK since entry reaches a tier's trigger - the peak, not
        the current price, because a position that touched the level has shown
        something even if it has since fallen back. Once armed it stays armed.

        Tiers, added 2026-08-26. A single trigger at +0.5% only protects
        positions that got that far, and on 2026-08-26 the positions that went
        green but stopped short of +0.5% lost $293.10 across eight positions
        with no winners among them - the single largest recoverable bucket in
        the session. A lower tier arms earlier and takes those out flat instead.

        The floor sits slightly ABOVE entry rather than at it. Exiting at
        exactly the entry price is not a $0 trade: the sell crosses the spread,
        so a true zero fills a few cents down. +0.05% is what makes "no loss"
        actually mean no loss.

        Where the tiers conflict - a peak past several triggers - the HIGHEST
        floor wins, so adding a low tier can never loosen the protection a
        higher one already gave.

        The risk this carries, stated plainly: arming earlier means arming
        inside the dip that momentum entries usually take before they run. On
        2026-08-26 all seven tiered winners traded below entry at some point
        (MAE -0.03% to -0.56%) and none was scratched, because their dip came
        BEFORE the peak crossed +0.5%. A lower trigger moves the arm point
        into that window. MFE/MAE do not record which came first, so this is
        measurable only going forward - watch BREAKEVEN_STOP count and the MFE
        of what it exits.

        Why this instead of a wider trailing stop, which was tried and reverted
        on 2026-08-25: a trailing exit lands at roughly (MFE - trail), so a wider
        leash mechanically gives back MORE of the peak, and it goes inert
        entirely for positions peaking below the trail width. That day CHWY
        peaked +0.98% and left at -0.41%, CRM +0.93% -> -0.67%, COIN +1.32% ->
        -0.23%: three winners turned into losers by leash width. A floor fixes
        exactly those without loosening anything for positions that never go
        green.

        Composed with the trail rather than replacing it - Strategy.check_exit
        takes whichever fires - so a position that runs keeps its trailing stop.
        """
        trading = self.config["trading"]
        if not trading.get("use_breakeven_floor", False):
            return 0

        peak_gain = (self.highest_since_entry - self.entry_price) / self.entry_price

        # Epsilon for the same reason as the take-profit tiers: a peak computed
        # as exactly +0.5% lands just under 0.005 in binary floating point, and
        # a strict comparison would leave the position unarmed at precisely the
        # level that is supposed to arm it.
        armed = [floor for trigger, floor in self._breakeven_tiers()
                 if peak_gain >= trigger / 100 - TIER_EPSILON]
        if not armed:
            return 0

        floor_price = self.entry_price * (1 + max(armed) / 100)
        if current_price <= floor_price * (1 + TIER_EPSILON):
            return self.qty_remaining
        return 0

    def check_final_exit(self, current_price):
        """Check if we should sell all remaining at -1.0% loss"""
        loss_pct = (current_price - self.entry_price) / self.entry_price
        final_exit_trigger = self.config["trading"]["final_exit_loss_pct"] / 100

        if loss_pct <= final_exit_trigger:
            return self.qty_remaining
        return 0

    def check_gap_exit(self, current_price):
        """
        Exit immediately when price has GAPPED past the stop rather than
        walking to it.

        WHAT THIS CAN AND CANNOT DO, stated plainly, because the honest answer
        matters more than the feature. A stop is not a promise about
        execution. If a symbol halts at $100 and reopens at $97, a -0.5% stop
        does not fill at $99.50 - there were no trades between those prices.
        Nothing in software prevents that loss.

        What software CAN do is stop making it worse, and that is a real
        failure this bot is exposed to. The exit sweep evaluates rules in a
        fixed order, and several of them - trailing stop, breakeven floor,
        momentum fade, resistance - are written for a price that MOVED there.
        After a gap those rules can decline to fire, or fire for a partial,
        leaving a position open through a reopen that is still moving. The
        2026-09-01 phantom loop showed what "the exit path did not fire and
        nothing noticed" costs over 45 minutes.

        So: when price is more than gap_multiple x the configured stop below
        entry IN A SINGLE OBSERVATION, sell everything remaining and say so.
        No tiers, no partial, no trailing logic that assumes continuity.

        Deliberately NOT a tighter stop. It only fires BEYOND the stop, where
        the position was already condemned - so it can never exit something the
        normal rules would have kept.
        """
        cfg = self.config["trading"].get("gap_exit") or {}
        if not cfg.get("enabled"):
            return 0
        try:
            stop_pct = abs(float(self.config["trading"].get("final_exit_loss_pct", -1.0)))
            mult = float(cfg.get("gap_multiple", 1.5))
            move = (current_price - self.entry_price) / self.entry_price * 100
            if move <= -(stop_pct * mult):
                logger.warning(
                    f"{self.symbol}: GAP EXIT - {move:+.2f}% from entry, past "
                    f"{mult:g}x the {stop_pct:g}% stop in one observation. Selling "
                    f"everything remaining rather than running rules that assume "
                    f"price walked here."
                )
                return self.qty_remaining
        except (TypeError, ValueError, ZeroDivisionError):
            return 0
        return 0

    def effective_trail_pct(self):
        """
        The trailing distance this position should be using RIGHT NOW.

        The configured trailing_stop_pct is a single fixed number for the whole
        life of a position, which treats three very different situations
        identically: a trade still proving itself, a trade that has run and is
        being given room, and a trade that ran, stalled, and is quietly giving
        it all back. The third is the expensive one - 2026-08-28 had 19 of 30
        positions peak under +0.5% and lose $484 together, which is the shape
        of "went up a little, went nowhere, came back".

        Two independent tighteners, and the TIGHTER of the two wins:

          1. TIER RATCHET. Each take-profit tier that fills pulls the trail in
             by trail_tighten_per_tier_pct. Shares still held after a
             scale-out are, by definition, the part of the position being
             asked to run further - so the bar for giving back what it has
             already made should rise each time, not stay flat.

          2. STALL. A position that has been open longer than
             stall_after_minutes without making a new high is not consolidating,
             it is done. The trail tightens to stall_trail_pct so it exits on
             the next meaningful give-back rather than riding the full 0.75%
             down from a peak it set ten minutes ago.

        Never wider than the configured value, and never below min_trail_pct -
        a trail inside the spread exits on nothing happening at all.
        """
        t = self.config["trading"]
        base = float(t.get("trailing_stop_pct", 0.75))
        cfg = t.get("trail_tightening") or {}
        if not cfg.get("enabled"):
            return base

        trail = base
        try:
            per_tier = float(cfg.get("tighten_per_tier_pct", 0.15))
            trail -= per_tier * len(self.take_profit_tiers_done)

            stall_after = cfg.get("stall_after_minutes")
            if stall_after and self.last_high_at is not None:
                idle = (_now_et() - self.last_high_at).total_seconds() / 60.0
                if idle >= float(stall_after):
                    trail = min(trail, float(cfg.get("stall_trail_pct", 0.3)))

            trail = max(float(cfg.get("min_trail_pct", 0.15)), min(base, trail))
        except (TypeError, ValueError) as e:
            logger.debug(f"{self.symbol}: trail tightening skipped ({e})")
            return base
        return trail

    def update_trailing_stop(self, current_price):
        """Update highest price and check trailing stop"""
        if current_price > self.highest_price:
            self.highest_price = current_price
            # Timestamped so a position that stops making new highs can be
            # told apart from one that is still working. Only a NEW HIGH resets
            # this - drifting sideways below the peak is exactly the state the
            # stall rule is built to catch.
            self.last_high_at = _now_et()

        trail_pct = self.effective_trail_pct() / 100
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
        window = _samples_for_minutes(
            self.config, "momentum_fade_window_minutes",
            "momentum_fade_window_samples", 6, 6,
        )
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
        if not self.config["trading"].get("use_resistance_exit", True):
            return 0

        lookback = _samples_for_minutes(
            self.config, "resistance_lookback_minutes",
            "resistance_lookback_samples", 3, 3,
        )
        if len(self.price_history) < lookback:
            return 0

        recent = self.price_history[-lookback:]

        # "Falling", not "fell on every single tick".
        #
        # This used to require recent[i] < recent[i-1] for EVERY sample. At a
        # 60-second poll the window was 3 samples and that was a reasonable
        # description of a failed breakout. Converting the window to minutes
        # made it 18 samples at a 10-second poll - and 18 consecutive strictly
        # lower prices essentially never occur, which would have switched this
        # rule off silently rather than making it twitchier.
        #
        # The condition that actually matters is unchanged: the window opened at
        # the position's peak and price is meaningfully below it now. Allowing a
        # tolerance for up-ticks makes the rule mean the same thing at any poll
        # rate, which is the whole point of expressing the window in minutes.
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        max_ups = int((len(recent) - 1) * self.config["trading"].get(
            "resistance_max_uptick_fraction", 0.34))
        falling = recent[-1] < recent[0] and ups <= max_ups

        # Never sell into an upturn.
        #
        # The two conditions above describe the window as a whole, and a window
        # can still be net-down while price is turning back up at its right-hand
        # edge - which is exactly the case worth NOT exiting: the pullback ended
        # and the position is recovering. Requiring the last tick to be
        # non-rising costs nothing when the breakout has genuinely failed (price
        # is still dropping, so the condition holds) and stops the rule firing at
        # the bottom of a dip it should have held through.
        #
        # Deliberately only the most recent tick. A longer "is it recovering"
        # test would re-introduce the poll-rate sensitivity that expressing the
        # window in minutes was meant to remove.
        if len(recent) >= 2 and recent[-1] > recent[-2]:
            return 0

        if not (falling and recent[0] == self.highest_since_entry):
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

    def tighten_for_regime(self, final_pct, trail_pct, breakeven_trigger):
        """
        Pull this position's loss-side rules in, in place, because the MARKET
        turned - not because this position did anything.

        The regime read governs new entries only, so on 2026-09-02 nine longs
        opened into a tape already flagged "QQQ is not trending up" and each
        then travelled independently to its own -1.0% stop. They did not fail
        for nine reasons; they failed for one, and the one was knowable while
        they were still open.

        MONOTONIC, and that is the safety property. Every value can only move
        TIGHTER - a regime that flips back does not re-widen a stop, because
        re-widening would move a live stop AWAY from a position that is already
        losing, which is the single worst thing this could do. Returns True
        when something actually changed, so the caller can log a real change
        rather than a no-op every poll.
        """
        import copy as _copy
        changed = []
        cfg = _copy.deepcopy(self.config)
        t = cfg["trading"]

        cur_final = t.get("final_exit_loss_pct", -1.0)
        if final_pct is not None and final_pct > cur_final:
            t["final_exit_loss_pct"] = final_pct
            changed.append(f"final {cur_final}% -> {final_pct}%")

        cur_trail = t.get("trailing_stop_pct")
        if trail_pct is not None and cur_trail is not None and trail_pct < cur_trail:
            t["trailing_stop_pct"] = trail_pct
            changed.append(f"trail {cur_trail}% -> {trail_pct}%")

        if breakeven_trigger is not None:
            tiers = t.get("breakeven_tiers") or []
            lowest = min((x.get("trigger_pct", 99) for x in tiers), default=None)
            if lowest is None or breakeven_trigger < lowest:
                t.setdefault("breakeven_tiers", []).append(
                    {"trigger_pct": breakeven_trigger, "floor_pct": 0.0})
                t["use_breakeven_floor"] = True
                changed.append(f"breakeven arms at +{breakeven_trigger}%")

        if not changed:
            return None
        self.config = cfg
        return "; ".join(changed)

    def process_exit(self, qty_to_exit, exit_reason):
        """Record a CONFIRMED exit - call only after the broker has actually
        filled the sell order. This is where first_exit_done actually gets
        set (see check_first_exit's docstring for why)."""
        self.qty_remaining -= qty_to_exit
        # Prefix, not equality: the threshold in the reason string is built
        # from THIS trade's config, so a burst position says
        # "FIRST_EXIT_-0.3%". Matching the literal -0.5% would leave
        # first_exit_done unset on every burst trade, letting the first exit
        # fire again on the next poll.
        if str(exit_reason).startswith("FIRST_EXIT_"):
            self.first_exit_done = True
        if str(exit_reason).startswith("TAKE_PROFIT"):
            # Retire the tier that fired AND everything below it - see
            # check_take_profit for why selling again on a pullback is wrong.
            tiers = self.take_profit_tiers()
            for idx, tier in enumerate(tiers):
                if exit_reason == f"TAKE_PROFIT_{tier['gain_pct']:g}%":
                    self.take_profit_tiers_done.update(range(idx + 1))
                    break
            else:
                # Single-tier/legacy reason string: retire everything.
                self.take_profit_tiers_done.update(range(len(tiers)))
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

    def confirm_entry(self, symbol, price, qty, config_override=None):
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
        # config_override gives one position its OWN exit rules. TradeManager
        # already reads every exit threshold from the config it is handed -
        # first exit, final exit, trailing, breakeven tiers, take-profit tiers -
        # so a different config is a different exit profile, with no branching
        # in the exit path itself. Used by the opening-move experiment, whose
        # trades are meant to be short scalps and want tighter stops than the
        # session they run alongside.
        #
        # The position keeps whatever config it was opened with for its whole
        # life. That matters: changing exit rules under an open position
        # mid-session would make its behaviour unattributable to either profile.
        cfg = config_override or self.config
        self.trades[symbol] = TradeManager(symbol, price, qty, cfg)
        if config_override is not None:
            t = cfg["trading"]
            logger.info(
                f"{symbol}: Entered {qty} shares at {price} under a CUSTOM exit "
                f"profile - first {t.get('first_exit_loss_pct')}%, final "
                f"{t.get('final_exit_loss_pct')}%, trail {t.get('trailing_stop_pct')}%, "
                f"tiers {[x.get('gain_pct') for x in (t.get('take_profit_tiers') or [])]}"
            )
        else:
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

    def correct_entry_qty(self, symbol, actual_qty):
        """
        Rebase an open position onto the share count the broker ACTUALLY holds.

        submit_entry_order records the quantity it ASKED for. A market order can
        fill partially - it happened repeatedly on 2026-08-24, where HOOD's
        average entry price moved across four consecutive polls as the order
        filled in pieces - and until now nothing reconciled the count. The bot
        would then believe it held 79 shares while the broker held 40.

        Two concrete harms that fixes. Exit orders sized to a position that does
        not exist get rejected or partially filled, leaving shares stranded. And
        every fraction-based rule - the take-profit tiers, the -0.5% first exit -
        sizes off entry_qty, so a 33% tranche of a phantom position is wrong in
        the same proportion.

        Only ever reduces. A broker count HIGHER than tracked usually means an
        exit has been submitted but not yet settled, and trusting that number
        would resurrect shares the strategy has already sold.
        """
        if symbol not in self.trades:
            return False
        try:
            actual_qty = int(actual_qty)
        except (TypeError, ValueError):
            return False
        if actual_qty < 0:
            return False

        trade = self.trades[symbol]
        if actual_qty >= trade.qty_remaining:
            return False

        old_remaining, old_entry = trade.qty_remaining, trade.entry_qty
        # Shrink the original size by the same proportion, so fraction-based
        # rules keep sizing off something real.
        if old_remaining > 0:
            trade.entry_qty = max(1, int(trade.entry_qty * actual_qty / old_remaining))
        trade.qty_remaining = actual_qty

        logger.warning(
            f"{symbol}: PARTIAL FILL reconciled - tracking said {old_remaining} "
            f"shares, broker holds {actual_qty}. Original size {old_entry} -> "
            f"{trade.entry_qty} so tiers and the first exit size off what is "
            f"actually held."
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

        # Take-profit is checked separately because it names its own reason
        # (which tier fired); everything else has a fixed reason.
        tp_qty, tp_reason = trade.check_take_profit(current_price)

        # The two loss-exit reasons name their own THRESHOLD, and until
        # 2026-09-02 they named it as a hardcoded literal - "FINAL_EXIT_-1.0%"
        # regardless of what the position's exit rules actually were. The
        # opening burst runs a CUSTOM exit profile (first -0.3%, final -0.35%,
        # trail 0.4%), so every burst trade ever taken was stamped with the
        # session's numbers instead of its own. AI on 2026-09-02 was recorded
        # as FINAL_EXIT_-1.0% having exited at -0.42%, which is the -0.35% rule
        # plus slippage.
        #
        # That string is not cosmetic: it is written to trade_history.csv and
        # read back by ops/replay.py, ops/grid.py and ops/be-outcomes.py. A
        # mislabelled threshold silently corrupts every exit-rule study those
        # tools produce. Built from the trade's OWN config so it always
        # describes the rule that actually fired.
        # Formatted to match the labels already in trade_history.csv: -1.0%
        # and -0.5%, not -1% and -0.5%. A :g format drops the trailing zero and
        # would silently split every historical FINAL_EXIT_-1.0% row from every
        # new one, breaking the exit-reason grouping in replay/grid/session-
        # metrics for no benefit. At least one decimal, at most two.
        def _lbl(v):
            out = f"{float(v):.2f}".rstrip("0")
            return out + "0" if out.endswith(".") else out

        _t = trade.config["trading"]
        _final_label = f"FINAL_EXIT_{_lbl(_t.get('final_exit_loss_pct', -1.0))}%"
        _first_label = f"FIRST_EXIT_{_lbl(_t.get('first_exit_loss_pct', -0.5))}%"

        checks = [
            # FIRST, ahead of everything. A gap is the one case where the other
            # rules cannot be trusted: they are written for a price that walked
            # here, and after a halt or a news gap it did not. Firing this
            # first means the position is closed by the rule that knows that,
            # rather than by whichever continuity-assuming rule happens to
            # match. It only ever triggers BEYOND the final stop, so it cannot
            # take a position the normal ladder would have kept.
            ("GAP_EXIT", trade.check_gap_exit),
            (_final_label, trade.check_final_exit),
            (_first_label, trade.check_first_exit),
            (tp_reason or "TAKE_PROFIT", (lambda _p: tp_qty)),
            ("BREAKEVEN_STOP", trade.check_breakeven_stop),
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

    def tighten_all_for_regime(self, final_pct=None, trail_pct=None,
                               breakeven_trigger=None):
        """
        Apply tighten_for_regime to every open position. Returns
        {symbol: what changed} for the ones that actually changed.
        """
        out = {}
        for symbol, trade in list(self.trades.items()):
            try:
                note = trade.tighten_for_regime(final_pct, trail_pct, breakeven_trigger)
                if note:
                    out[symbol] = note
            except Exception as e:
                logger.error(f"{symbol}: regime stop tightening failed: {e}")
        return out

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

    def drop_phantom(self, symbol):
        """
        Remove a tracked position that was never actually held at the broker -
        the entry order was submitted but never filled, so `confirm_entry` was
        called (submit_entry_order only checks the order was ACCEPTED, not
        FILLED) against shares that do not exist.

        Distinct from confirm_exit: that commits a REAL sale (updates
        qty_remaining, records realized P&L). This commits nothing, because
        nothing was ever bought or sold - the position is simply wrong and
        needs to stop being checked. A no-op if the symbol isn't tracked,
        since the executor's phantom-guard and this call can both observe
        the same broker state on a busy poll.
        """
        self.trades.pop(symbol, None)

    def get_open_trades(self):
        """Return all open trades"""
        return self.trades.copy()
