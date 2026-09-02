import csv
import logging
from collections import deque
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.analytics.csv_schema import repair_header

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
# Matched by PREFIX for the two threshold-bearing reasons, because the
# threshold in the string is built from the position's own exit config - a
# burst trade says "FINAL_EXIT_-0.35%", a session trade "FINAL_EXIT_-1.0%".
STOP_LOSS_EXIT_PREFIXES = ("FINAL_EXIT_", "FIRST_EXIT_")
STOP_LOSS_EXIT_REASONS = {"TRAILING_STOP", "FLATTEN_ALL"}


def is_stop_loss_exit(reason):
    """True when `reason` names a loss-side exit, at any threshold."""
    reason = str(reason or "")
    return reason.startswith(STOP_LOSS_EXIT_PREFIXES) or reason in STOP_LOSS_EXIT_REASONS


# TAKE_PROFIT is deliberately NOT a stop-loss reason - it is a gain being banked.

# FIRST_EXIT_-0.5% is the only exit reason that ever sells a PARTIAL position
# (first_exit_pct, e.g. 33%) - every other reason always sells the entire
# qty_remaining (see TradeManager/Strategy.check_exit). Used to color-code
# entry/exit log lines for live journalctl monitoring - green (buy), red
# (100% sold), yellow (partial sold) - so a scan of the live log makes the
# nature of each fill obvious at a glance.
# TAKE_PROFIT sells take_profit_fraction and leaves the rest running, so like
# FIRST_EXIT it must not decrement the open-position count or be coloured as a
# full close.
PARTIAL_EXIT_REASONS = {"TAKE_PROFIT"}
PARTIAL_EXIT_PREFIXES = ("FIRST_EXIT_",)

# Take-profit reasons carry their tier ("TAKE_PROFIT_1.25%"), so membership has
# to be a prefix test rather than an exact match. The TOP tier sells everything
# remaining, so whether a take-profit is partial cannot be decided from the
# reason alone - it depends on whether shares are left afterwards, which is why
# is_partial_exit takes the quantities.
TAKE_PROFIT_PREFIX = "TAKE_PROFIT"

# Returned by submit_exit_order when there is nothing to sell: the broker
# holds zero shares of the symbol, so the "position" was a phantom - see
# submit_exit_order's PHANTOM ENTRY GUARD for how that happens.
#
# A third, distinct outcome from the existing two. `order is not None` means
# a real sell filled; `order is None` means a real attempt failed and should
# be retried next poll. Neither fits "there was nothing to try in the first
# place" - treating it as either would be wrong: as success, it would try to
# confirm_exit() a sale that never happened; as failure, it would retry an
# identical sell against the same zero holding forever, which is exactly what
# happened to NOW/PLTR/MSTR/RGTI/SOXL for 45+ minutes on 2026-09-01 before
# no_shorting turned the retries into log noise instead of real shorts.
PHANTOM_EXIT = object()


def is_partial_exit(reason, qty, qty_before):
    """
    True when this sale leaves the position OPEN.

    Getting this wrong in either direction is expensive: calling a full exit
    partial leaves a closed symbol counted against max_concurrent_positions for
    the rest of the session, and calling a partial exit full frees a slot while
    shares are still held, so the cap silently over-admits.
    """
    reason = str(reason or "")
    if qty_before is not None and qty is not None:
        try:
            return int(qty) < int(qty_before)
        except (TypeError, ValueError):
            pass
    if reason.startswith(TAKE_PROFIT_PREFIX):
        return True
    return reason.startswith(PARTIAL_EXIT_PREFIXES) or reason in PARTIAL_EXIT_REASONS

ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_RESET = "\033[0m"

class Executor:
    """Handles order submission and trade tracking"""

    def __init__(self, broker, config):
        # Set by main to Strategy.correct_entry_price. Optional so the executor
        # stays usable on its own (and in tests) without a strategy attached.
        self.on_entry_price_corrected = None
        # Set by main to MarketDataManager.entry_price_source, so each fill
        # records whether it was priced live or from delayed REST data.
        self.entry_price_source = None
        # Set by main to Strategy.correct_entry_qty - see that method for why a
        # partially filled entry must reach the strategy, not just the executor.
        self.on_entry_qty_corrected = None
        # Exits awaiting a post-exit price check: each is
        # {row, symbol, exit_price, due_at}. See note_post_exit_prices - this is
        # how "what happened AFTER we sold" becomes answerable at all. Until
        # now the record stopped at the exit, so questions like "do stopped-out
        # losers keep falling or bounce?" had no data behind them.
        self._post_exit_pending = []
        self.broker = broker
        self.config = config
        # trades_log holds one row PER COMPLETED EXIT - self-contained with
        # entry_price, exit_price, qty, pl, pl_pct, exit_reason - this is what
        # gets saved to trades.json and read by the email/push-notification
        # summaries. Raw order submissions (entries + exits, with order IDs)
        # are tracked separately in order_history for auditability.
        self.trades_log = []
        self.daily_pnl = 0.0  # realized + unrealized, refreshed every poll by refresh_account_snapshot()
        self._realized_pnl_today = 0.0  # fallback accumulator, reset per-day by _add_realized_pnl
        self._pnl_date = None
        self.order_history = []
        self.open_entries = {}  # symbol -> entry price, used to compute P&L when the matching exit(s) happen
        self.entry_meta = {}  # symbol -> {method, rsi, entry_time, burst_logic}, for the daily trade report
        self.day_burst_summary = ""  # one-line description of the burst logic actually used today
        # Soft loss-velocity warnings raised today (see check_loss_velocity).
        # Kept so the daily report can show that the day was flagged on its way
        # down, not only that it ended down.
        self.loss_velocity_notes = []
        self._loss_warnings_fired = set()
        self._loss_warn_day = None
        self._loss_watch_started_at = None
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
        self._last_close_at = {}  # symbol -> (time.monotonic() at full close, closed_at_loss) for the re-entry cooldown
        self._pending_cost = {}  # symbol -> cost basis, for exposure while the broker lags
        # symbol -> the qty submit_exit_order ACTUALLY submitted, when it
        # differs from what the caller asked for (the phantom-entry guard
        # corrects a stale/over-large qty down to what the broker really
        # holds). The caller commits to Strategy with the qty it originally
        # computed unless it reads this first - see exit_qty_actually_submitted.
        self._last_exit_qty = {}
        # symbol -> time.monotonic() when a PHANTOM entry was last dropped, and
        # symbol -> how many entry orders have been SUBMITTED for it today.
        #
        # 2026-09-02: WDAY was submitted 8 times and RBLX 4 in five minutes.
        # Every one was a phantom - the order never filled, the guard below
        # dropped it, and nothing stopped the next poll from buying it again,
        # because reentry_cooldown_after_loss_only is on and a dropped phantom
        # is not a LOSS. A failure to fill is precisely the event that should
        # not be retried ten seconds later, so it arms the cooldown on its own
        # terms, independent of that setting.
        self._phantom_dropped_at = {}
        # symbol -> consecutive marketable-limit exit attempts that have not
        # filled, so the exit can escalate to a market order rather than
        # chasing a falling price one poll at a time.
        self._limit_exit_attempts = {}
        self._logged_loss_limit = None
        self._logged_loss_tier = 1.0
        # PRE-TRADE RATE CONTROLS. A rolling window of (monotonic_ts, notional)
        # for every order SUBMITTED, entries and exits alike. See
        # rate_limit_check - this is the guard against a runaway loop, which is
        # a different failure from any single order being wrong.
        self._order_times = deque(maxlen=2000)
        self._entry_attempts = {}
        self._entry_attempts_day = None

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

            # Reconcile entry prices against what was ACTUALLY paid.
            #
            # submit_entry_order records the price the SIGNAL fired at, because
            # a market order's fill price is not known at submission time. Those
            # differ, and on 2026-08-20 they differed a lot: 11 of 20 entries
            # filled worse than the decision price, for $115.94 of slippage on a
            # -$239.00 day. HUT decided at 84.30, filled at 84.91 (+0.72%) and
            # was reported as +$101.00 when it actually lost $15.69.
            #
            # Every downstream number was therefore computed off a price the bot
            # never paid: trade P&L, the CSV, the report, and MFE/MAE. The
            # broker's avg_entry_price is the truth, and it costs nothing extra
            # here since positions are already fetched.
            for symbol, position in positions.items():
                actual = getattr(position, "avg_entry_price", None)
                if not actual:
                    continue
                try:
                    actual = float(actual)
                except (TypeError, ValueError):
                    continue
                recorded = self.open_entries.get(symbol)
                if recorded and abs(actual - recorded) > 1e-6:
                    slip_pct = (actual - recorded) / recorded * 100
                    logger.info(
                        f"{symbol}: entry price corrected {recorded:.4f} -> {actual:.4f} "
                        f"({slip_pct:+.2f}% slippage vs the signal price)"
                    )
                    # KEEP IT. This number was computed and thrown away since
                    # the day it was written, so the one cost that scales with
                    # every single trade has never been analysable. It matters
                    # more here than in most strategies: targets are 0.75-1.5%,
                    # and 2026-09-02 saw +0.45% on NOW and +0.91% on OLLI -
                    # most of a winning trade gone before the position existed.
                    # Accumulated per symbol because a position can be
                    # corrected more than once as fills arrive.
                    meta = self.entry_meta.setdefault(symbol, {})
                    meta["signal_price"] = meta.get("signal_price", recorded)
                    meta["entry_slippage_pct"] = round(
                        (actual - meta["signal_price"]) / meta["signal_price"] * 100, 4)
                    self.open_entries[symbol] = actual
                    # Tell the strategy too, or every exit rule for this
                    # position keeps measuring against the signal price.
                    if self.on_entry_price_corrected is not None:
                        try:
                            self.on_entry_price_corrected(symbol, actual)
                        except Exception as e:
                            logger.error(
                                f"Could not rebase {symbol} onto its fill price: {e}"
                            )

            # Reconcile SHARE COUNT as well as price. A market order can fill
            # partially, and the bot would otherwise keep believing it holds the
            # quantity it asked for. Skipped inside the entry grace window,
            # where the broker's list is simply lagging a fill rather than
            # reporting a short one.
            if self.on_entry_qty_corrected is not None:
                for symbol, position in positions.items():
                    if now - self._entry_recorded_at.get(symbol, 0.0) < ENTRY_CONFIRM_GRACE_SECONDS:
                        continue
                    try:
                        held = int(float(getattr(position, "qty", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                    if held <= 0:
                        continue
                    try:
                        self.on_entry_qty_corrected(symbol, held)
                    except Exception as e:
                        logger.error(f"Could not reconcile share count for {symbol}: {e}")

            self.daily_pnl = self._compute_daily_pnl(account, positions)
        except Exception as e:
            logger.error(f"Error refreshing account snapshot: {e}")
            # Fail closed: on error, assume zero buying power and max exposure
            # so pre_entry_check blocks new entries rather than proceeding on
            # stale/unknown state. Equity and position count are left at their
            # last-known values since those aren't used to gate "can we buy" -
            # only buying_power and total_exposure_usd are, and both are
            # deliberately zeroed/maxed here.
            #
            # daily_pnl is deliberately LEFT AT ITS LAST KNOWN VALUE rather
            # than zeroed. Zeroing it would clear a breached loss limit on a
            # transient API error and let trading resume straight into the
            # loss it had just stopped for - the one direction this must
            # never fail in.
            self._buying_power = 0.0
            self._total_exposure_usd = float("inf")

    def note_post_exit_prices(self, price_lookup, minutes=15):
        """
        Fill in each closed trade's price `minutes` after it was sold.

        price_lookup(symbol) -> float or None. Called once per poll with the
        price source the bot is already reading, so this costs no extra API
        calls for symbols still on the watchlist.

        Writes two fields onto the trade row:
            post_exit_pct   price change since the exit, in %
            post_exit_note  "kept falling" / "bounced" / "flat", from the
                            perspective of the trade - for a LOSER a rise after
                            the exit means the stop was early, and for a winner
                            a further rise means the exit was early.

        Deliberately does not touch pl or pl_pct: this is observation, never a
        restatement of what was actually booked.
        """
        if not self._post_exit_pending:
            return
        now = time.time()
        still_pending = []
        for item in self._post_exit_pending:
            if now < item["due_at"]:
                still_pending.append(item)
                continue
            try:
                price = price_lookup(item["symbol"])
            except Exception:
                price = None
            if not price:
                continue          # drop it rather than guess
            base = item["exit_price"]
            if not base:
                continue
            pct = (price - base) / base * 100
            row = item["row"]
            row["post_exit_pct"] = round(pct, 3)
            was_loss = (row.get("pl") or 0) < 0
            if abs(pct) < 0.15:
                note = "flat"
            elif pct > 0:
                note = "bounced - exit was early" if was_loss else "ran further"
            else:
                note = "kept falling - exit was right" if was_loss else "gave it back"
            row["post_exit_note"] = note
        self._post_exit_pending = still_pending

    def _count_entry_attempt(self, symbol):
        """Tally entry SUBMISSIONS per symbol per day, for the attempt cap."""
        today = datetime.now().date()
        if self._entry_attempts_day != today:
            self._entry_attempts_day = today
            self._entry_attempts = {}
        self._entry_attempts[symbol] = self._entry_attempts.get(symbol, 0) + 1

    def entry_attempts_today(self, symbol):
        """How many entry orders have been submitted for `symbol` today."""
        if self._entry_attempts_day != datetime.now().date():
            return 0
        return self._entry_attempts.get(symbol, 0)

    def _note_position_closed(self, symbol, closed_at_loss):
        """Record when a position fully closed, to enforce the re-entry cooldown."""
        self._last_close_at[symbol] = (time.monotonic(), closed_at_loss)

    def phantom_cooldown_remaining(self, symbol):
        """
        Seconds left before `symbol` may be bought again after a PHANTOM drop.

        Checked SEPARATELY from the ordinary cooldown, and never skipped, so
        that opening_burst.skip_reentry_cooldown cannot switch it off. That
        flag exists so an opening trade does not block the normal session from
        re-entering the same name later - a deliberate choice about the
        EXPERIMENT. It was never meant to license re-submitting an order the
        broker has just declined to fill, which is a different thing entirely
        and the shape that produced eight WDAY submissions in five minutes.
        """
        at = self._phantom_dropped_at.get(symbol)
        if at is None:
            return 0.0
        minutes = self.config["trading"].get("phantom_reentry_cooldown_minutes")
        if minutes is None:
            minutes = self.config["trading"].get("reentry_cooldown_minutes", 0)
        if not minutes:
            return 0.0
        return max(0.0, minutes * 60 - (time.monotonic() - at))

    def reentry_cooldown_remaining(self, symbol):
        """
        Seconds left before `symbol` may be bought again, or 0.0 if it's free.

        On 2026-08-19 the bot stopped out of a name and then bought it back
        minutes later off a fresh signal, repeatedly, on names that were
        simply trending down all morning: RGTI -$340 over 4 exits, MRVL -$328
        over 3, CLSK -$238 over 4, PLUG -$137, UPST -$134. Every one of those
        was entered, stopped out, and re-entered into the same decline.

        The cooldown only starts after a LOSING exit when
        reentry_cooldown_after_loss_only is set (the default). Re-entering a
        name that just paid out is a different situation and was profitable
        that day - UBER +$190 over 2 entries, CHWY +$159 over 4, CMG +$109
        over 2 - so blocking every re-entry indiscriminately would have cost
        more than it saved.
        """
        minutes = self.config["trading"].get("reentry_cooldown_minutes", 0)

        # A dropped PHANTOM is checked FIRST and is never subject to
        # reentry_cooldown_after_loss_only. It is neither a win nor a loss -
        # it is the broker declining to fill - so the loss-only gate lets it
        # through, which is how one symbol got submitted eight times in five
        # minutes on 2026-09-02. Its own knob so it can be tuned (or zeroed)
        # without touching the ordinary post-exit cooldown; falls back to that
        # one when unset.
        left = self.phantom_cooldown_remaining(symbol)
        if left > 0:
            return left

        if not minutes:
            return 0.0

        entry = self._last_close_at.get(symbol)
        if entry is None:
            return 0.0

        closed_at, closed_at_loss = entry
        if self.config["trading"].get("reentry_cooldown_after_loss_only", True) and not closed_at_loss:
            return 0.0

        elapsed = time.monotonic() - closed_at
        return max(0.0, minutes * 60 - elapsed)

    @property
    def equity(self):
        """Account equity as of the last refresh_account_snapshot(). 0.0 before
        the first refresh, which callers treat as fail-closed."""
        return self._equity

    @property
    def _open_position_count(self):
        """Number of open positions, derived from the reconciled symbol set."""
        return len(self._open_symbols)

    def _note_order_submitted(self, qty, price):
        """Record one submitted order for the rate window."""
        try:
            self._order_times.append((time.monotonic(), abs(float(qty or 0) * float(price or 0))))
        except (TypeError, ValueError):
            self._order_times.append((time.monotonic(), 0.0))

    def rate_limit_check(self, qty=None, price=None):
        """
        (ok, reason) for PRE-TRADE rate controls. Never raises.

        THE FAILURE THIS EXISTS FOR is different in kind from every other guard
        in this file. max_concurrent_positions, max_total_exposure_fraction and
        max_daily_loss_usd all bound the STATE the account ends up in; they are
        checked against a snapshot that refreshes once per poll. None of them
        bounds the RATE at which orders leave, and a loop that submits the same
        order repeatedly inside one poll - a retry that never terminates, a
        callback wired twice, a websocket reconnect that replays a buffer - can
        do its damage entirely between two snapshots.

        2026-09-02 is the mild version: 22 entries in seven minutes, and the
        only thing that stopped it was the daily loss limit, i.e. a
        consequence-based stop rather than a pre-trade control. WDAY alone was
        submitted eight times in five minutes. Nothing structural said no.

        Four independent limits, all optional, all fail-OPEN on a config
        problem but fail-CLOSED on an actual breach:

          max_orders_per_minute      - orders of any kind in a rolling 60s
          max_notional_per_minute    - dollars committed in a rolling 60s
          max_shares_per_order       - one absurd qty, e.g. from a bad price
          max_notional_per_order     - the same in dollars

        Counts ENTRIES AND EXITS together on purpose. A runaway loop does not
        care which side it is on, and an exit storm against a broker that keeps
        rejecting is exactly as damaging as an entry storm.
        """
        cfg = (self.config.get("trading") or {}).get("rate_limits") or {}
        if not cfg.get("enabled", True):
            return True, None

        try:
            q = abs(float(qty or 0))
            px = abs(float(price or 0))
            notional = q * px

            max_shares = cfg.get("max_shares_per_order")
            if max_shares and q > float(max_shares):
                return False, (f"order of {q:,.0f} shares exceeds "
                               f"max_shares_per_order {max_shares:,}")

            max_notional = cfg.get("max_notional_per_order")
            if max_notional and notional > float(max_notional):
                return False, (f"order of ${notional:,.0f} exceeds "
                               f"max_notional_per_order ${float(max_notional):,.0f}")

            window = float(cfg.get("window_seconds", 60))
            cutoff = time.monotonic() - window
            recent = [(t, n) for t, n in self._order_times if t >= cutoff]

            max_orders = cfg.get("max_orders_per_minute")
            if max_orders and len(recent) >= int(max_orders):
                return False, (
                    f"{len(recent)} orders in the last {window:.0f}s is at "
                    f"max_orders_per_minute ({max_orders}) - refusing until the "
                    f"window clears. This is the runaway-loop guard, not a "
                    f"judgement about this particular order."
                )

            max_min_notional = cfg.get("max_notional_per_minute")
            if max_min_notional:
                used = sum(n for _, n in recent)
                if used + notional > float(max_min_notional):
                    return False, (
                        f"${used:,.0f} already committed in the last {window:.0f}s; "
                        f"this ${notional:,.0f} order would pass "
                        f"max_notional_per_minute ${float(max_min_notional):,.0f}"
                    )
            return True, None
        except Exception as e:
            # A malformed limit must not block trading outright - but it must
            # be loud, because it means the guard is not guarding.
            logger.error(f"rate_limit_check failed ({type(e).__name__}: {e}) - allowing the order")
            return True, None

    def pre_entry_check(self, qty, price, symbol=None):
        """
        Returns (ok: bool, reason: str). Checked BEFORE ever attempting a
        broker order for a new entry - closes the gap where only Alpaca's own
        margin rejection used to be the backstop against over-leveraging.
        Four independent checks, all must pass:
          1. Enough buying power for this specific order.
          2. Not already at max_concurrent_positions.
          3. Adding this position wouldn't push total committed capital past
             max_total_exposure_fraction of current equity.
          4. Not already at max_positions_per_sector for this symbol's complex.

        `symbol` is optional so an older caller keeps working; without it the
        sector check is skipped rather than guessed at.
        """
        cost = qty * price

        # PDT floor. Under $25,000 equity, FINRA's pattern-day-trader rule caps
        # a margin account at 3 day trades per 5 business days - and this bot
        # does 20-30 in a single morning. Below the threshold the strategy
        # cannot legally be run as designed, so it stops OPENING positions.
        #
        # Deliberately does not flatten what is already open: forced selling on
        # an equity dip is a worse outcome than letting existing positions reach
        # their own exits, and the rule is about opening trades, not holding
        # them.
        #
        # Paper accounts are not subject to PDT, so this is inert today - it
        # exists so that switching paper_trading to false does not quietly put
        # the account in violation.
        min_equity = self.config["trading"].get("min_account_equity_usd", 0)
        if min_equity and self._equity and self._equity < min_equity:
            return False, (
                f"account equity ${self._equity:,.2f} is below "
                f"min_account_equity_usd ${min_equity:,.2f} - no new entries "
                f"(pattern-day-trader rule allows only 3 day trades per 5 days "
                f"below this level; open positions are still managed normally)"
            )

        if cost > self._buying_power:
            return False, f"insufficient buying power (need ${cost:.2f}, have ${self._buying_power:.2f})"

        max_positions = self.config["trading"].get("max_concurrent_positions")
        if max_positions and self._open_position_count >= max_positions:
            return False, f"at max_concurrent_positions ({self._open_position_count}/{max_positions})"

        # Sector concentration.
        #
        # The screener does not choose a sector; volatility does. rapid_increase
        # fires on whatever moves most, and on 2026-08-28 that was crypto miners
        # - MARA fired 21 times, RIOT 17, CIFR 17 - which filled 9 of 30
        # positions with one complex and returned 0 winners for -$131. The day
        # before, the same complex was +$318 on 4 of 4. That is not stock
        # selection working or failing; it is one sector call, taken twice,
        # sized as if it were nine independent bets.
        #
        # Counted from _open_symbols, the SAME reconciled set that
        # max_concurrent_positions above uses - not from open_entries, which
        # keeps an entry price around for P&L attribution and can therefore
        # still name a symbol the broker no longer holds. Two concentration
        # guards disagreeing about what is held is how one of them ends up
        # refusing entries against positions that closed minutes ago.
        # Hard per-symbol attempt cap. The cooldown above is the first line of
        # defence against a re-entry loop; this is the backstop for the case
        # the cooldown does not cover, because a loop of this shape has now
        # been produced twice from two different causes (45 minutes of retried
        # sells on 2026-09-01, eight submissions of one symbol on 2026-09-02).
        # A guard that only closes the specific hole last seen will be
        # rediscovered by the next one.
        # Rate controls FIRST - the cheapest check, and the one whose whole
        # purpose is to fire before anything else has a chance to run away.
        ok, why = self.rate_limit_check(qty, price)
        if not ok:
            return False, why

        max_attempts = self.config["trading"].get("max_entry_attempts_per_symbol_per_day")
        if max_attempts and symbol:
            attempts = self.entry_attempts_today(symbol)
            if attempts >= max_attempts:
                return False, (
                    f"at max_entry_attempts_per_symbol_per_day "
                    f"({attempts}/{max_attempts}) - not buying {symbol} again today"
                )

        max_per_sector = self.config["trading"].get("max_positions_per_sector")
        if max_per_sector and symbol:
            try:
                from src.analytics.sectors import sector_for
                sector = sector_for(symbol)
                # An unmapped symbol has no complex to be concentrated in, so it
                # is never refused - better to let one through than to lump every
                # unknown name into a single phantom bucket and starve it.
                if sector:
                    held = sum(1 for s_ in self._open_symbols
                               if s_ != symbol and sector_for(s_) == sector)
                    if held >= max_per_sector:
                        return False, (
                            f"at max_positions_per_sector for {sector} "
                            f"({held}/{max_per_sector}) - already holding "
                            f"{', '.join(sorted(s_ for s_ in self._open_symbols if sector_for(s_) == sector))}"
                        )
            except Exception as e:
                # Never block an entry because the sector map failed to load.
                logger.debug(f"{symbol}: sector concentration check skipped: {e}")

        max_exposure_fraction = self.config["trading"].get("max_total_exposure_fraction")
        if max_exposure_fraction and self._equity > 0:
            max_exposure_usd = self._equity * max_exposure_fraction
            if self._total_exposure_usd + cost > max_exposure_usd:
                return False, (
                    f"would exceed max_total_exposure_fraction "
                    f"(${self._total_exposure_usd:.2f} committed + ${cost:.2f} > ${max_exposure_usd:.2f} cap)"
                )

        return True, ""

    def record_entry_meta(self, symbol, method, rsi, entry_time=None, price_source=None):
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
            # Which data path priced this entry: "tick"/"stream bar" (live) or
            # "REST" (~15 min delayed). With only ~14 stream slots against a
            # larger watchlist, both happen in the same session, and until this
            # existed there was no way to tell the two apart after the fact -
            # so the stream's actual effect on fill quality was unmeasurable.
            "price_source": price_source or "unknown",
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
        self.record_entry_meta(
            symbol, method=entry_method or "UNKNOWN", rsi=entry_rsi,
            price_source=(self.entry_price_source(symbol) if self.entry_price_source else None),
        )

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
        self._count_entry_attempt(symbol)
        self._note_order_submitted(qty, price)
        # A NEW position gets a fresh marketable-limit budget. Deliberately
        # reset here and not on exit: this executor records a position as
        # closed the moment the exit ORDER is submitted, not when it fills, so
        # clearing the counter there reset it after every single attempt and
        # the escalation to a market order could never fire. Caught by
        # tests/test_0902b.py section 11.
        self._limit_exit_attempts.pop(symbol, None)

        logger.info(f"{ANSI_GREEN}Entry order submitted for {symbol}: {qty} shares at {price}{ANSI_RESET}")
        return order

    def submit_exit_order(self, symbol, qty, reason="", price=None, exit_rsi=None,
                          mfe_pct=None, mae_pct=None, qty_before=None, side="sell"):
        """
        Submit a market order to exit a position and record a documented
        trade row. Returns the order on success, or None on failure (does NOT
        raise) - callers must check the return value and only commit the exit
        to Strategy tracking (strategy.confirm_exit) when this returns
        non-None, exactly mirroring submit_entry_order's contract. If a sell
        fails, the position must stay fully tracked so the same exit
        condition gets retried on the next poll cycle instead of the bot
        silently forgetting it still holds (and needs to protect) it.

        `side` exists because this was hardcoded to "sell" until 2026-09-02,
        which is correct for a long and catastrophic for a short: selling a
        short position DOUBLES it. flatten_all_positions (the 16:00 time stop)
        runs through here, so any short the bot was holding at 16:00 got
        bigger instead of closing - found 2026-08-28, after ~$1,015 of damage
        from positions that reached short via the phantom-entry path.
        `qty` is always a POSITIVE magnitude; `side` says which way to trade
        it. Callers holding a broker position should derive it from the sign
        of position.qty rather than assuming.

        The bot never intends to open a short, so side="buy" here always means
        "close something that should not exist" - it is a safety path, not a
        short-selling strategy.
        """
        # Everything downstream that has a direction to it - P&L, the cash
        # effect on the buying-power cache, what counts as "closed at a loss"
        # for the re-entry cooldown - flips for a cover. One factor, applied
        # consistently, rather than three separate branches that can disagree.
        is_cover = (side == "buy")
        direction = -1 if is_cover else 1
        # Clear any still-working entry order for this symbol FIRST. Alpaca
        # rejects an exit while an opposite-side order is open ("potential wash
        # trade detected"), which on 2026-08-24 blocked four exits - including
        # PDD's, whose -0.56% RESISTANCE exit was refused and which then left at
        # the -1.0% final stop for $86.56 instead of roughly $47.
        #
        # Safe as an unconditional step: once the strategy has decided to exit,
        # the unfilled remainder of the entry is something it no longer wants.
        try:
            cancelled = self.broker.cancel_open_orders(symbol)
            if cancelled:
                logger.info(
                    f"{symbol}: cancelled {cancelled} working order(s) before exiting"
                )
        except Exception as e:
            # Never block the exit on this - a failed cancel just means the
            # submit below may hit the same rejection it would have anyway.
            logger.debug(f"{symbol}: pre-exit cancel failed, submitting anyway: {e}")

        # PHANTOM ENTRY GUARD.
        #
        # submit_entry_order records the position (open_entries, _open_symbols,
        # strategy.trades via the caller) the instant the entry ORDER is
        # SUBMITTED, not once it FILLS - a market order is not guaranteed to
        # fill before the next ~10s poll, and Alpaca's own fill latency is
        # occasionally longer than that gap. On 2026-09-01, NOW's exit check
        # fired while its entry BUY was still "working, 0/28 filled": this
        # code cancelled that working BUY (see above) and then submitted a
        # SELL for the full tracked qty against a position that had never
        # actually been bought - a short, rejected by no_shorting, retried
        # every ~10s for the rest of the session because nothing here ever
        # asked "does the broker actually hold anything to sell." The same
        # shape hit PLTR, MSTR, RGTI, SOXL the same day.
        #
        # Ask the broker what it ACTUALLY holds right before selling, rather
        # than trusting the qty this call was handed:
        #   - 0 shares: the entry never filled (or was already fully closed by
        #     some other path). There is nothing to sell. Clean up the
        #     phantom's bookkeeping directly and hand the caller PHANTOM_EXIT
        #     instead of submitting an order that can only be rejected or,
        #     worse, silently open a real short if no_shorting were ever off.
        #   - fewer shares than requested: a genuine partial fill the poll
        #     cycle hasn't reconciled yet (refresh_account_snapshot's qty
        #     correction only runs OUTSIDE the entry grace window). Sell what
        #     is actually held instead of over-asking and hitting Alpaca's
        #     "insufficient qty available" rejection - CRM hit exactly this on
        #     2026-09-01 (requested 16, available 15).
        # One extra get_positions() call per exit attempt, not per poll - exits
        # are far rarer than polls, so this is not the cost entry-side
        # per-symbol checks would be.
        try:
            live_positions = self.broker.get_positions()
        except Exception as e:
            logger.debug(
                f"{symbol}: could not verify live quantity before exiting "
                f"({e}) - proceeding with the tracked qty"
            )
            live_positions = None

        if live_positions is not None:
            live_pos = live_positions.get(symbol)
            try:
                live_qty = int(abs(float(getattr(live_pos, "qty", 0) or 0))) if live_pos else 0
            except (TypeError, ValueError):
                live_qty = None

            if live_qty == 0:
                logger.warning(
                    f"{symbol}: exit skipped - the broker holds 0 shares "
                    f"(the entry never filled). Dropping the phantom position "
                    f"instead of selling against nothing."
                )
                self._open_symbols.discard(symbol)
                self._entry_recorded_at.pop(symbol, None)
                self._pending_cost.pop(symbol, None)
                self.open_entries.pop(symbol, None)
                self._phantom_dropped_at[symbol] = time.monotonic()
                return PHANTOM_EXIT

            if live_qty is not None and live_qty < qty:
                logger.info(
                    f"{symbol}: exit qty corrected {qty} -> {live_qty} "
                    f"(broker holds less than tracked)"
                )
                qty = live_qty
                # The caller (main.py) computed its own qty before this call
                # and commits to Strategy with THAT number unless it checks
                # here first - see exit_qty_actually_submitted(). Without this,
                # confirm_exit would subtract more than was actually sold,
                # taking qty_remaining negative or below what the broker
                # genuinely still holds.
                self._last_exit_qty[symbol] = qty

        # MARKETABLE LIMIT rather than pure market, when configured.
        #
        # A market sell takes whatever the book offers. In a thin, fast,
        # one-directional tape that is an unbounded price: on 2026-09-02 a
        # -1.0% stop realized -1.46% (NOW), -1.59% (WDAY) and -1.07% (CRM).
        # A limit placed slippage_pct BELOW the current bid still crosses the
        # spread - so it fills immediately in any normal book - but refuses to
        # fill arbitrarily far away. The trade-off is explicit and worth
        # stating: an order that does not fill leaves the position OPEN, which
        # is why the band is generous relative to a stop (default 0.30% against
        # a 0.5%/1.0% stop) and why an unfilled limit is retried as a plain
        # market order on the NEXT poll rather than being left hanging.
        limit_px = None
        exit_cfg = (self.config.get("trading") or {}).get("marketable_limit_exits") or {}
        # ESCALATION. An unfilled limit is cancelled and re-submitted by the
        # next poll (see the unconditional cancel_open_orders above), which in
        # a gapping tape means chasing the price down one poll at a time and
        # never actually getting out. After max_attempts tries this drops back
        # to a plain market order: a bounded-price exit is better than a market
        # exit, but ANY exit is better than an open position the stop has
        # already condemned.
        _tries = self._limit_exit_attempts.get(symbol, 0)
        _max_tries = int(exit_cfg.get("max_attempts", 2) or 0)
        if _max_tries and _tries >= _max_tries:
            logger.warning(
                f"{symbol}: {_tries} marketable-limit exit attempts did not fill - "
                f"falling back to a MARKET order to guarantee the exit"
            )
            exit_cfg = {}
        if exit_cfg.get("enabled") and price:
            try:
                band = float(exit_cfg.get("slippage_pct", 0.3)) / 100.0
                # Selling: allow filling BELOW the reference. Covering a short:
                # allow filling above it.
                limit_px = round(price * ((1 + band) if is_cover else (1 - band)), 2)
            except Exception as e:
                logger.debug(f"{symbol}: marketable limit price unavailable ({e}) - using market")
                limit_px = None

        # Exits are COUNTED against the rate window but never BLOCKED by it.
        # An exit is how risk gets smaller; refusing one to satisfy a rate
        # limit would leave a position open precisely when something is already
        # going wrong. Counting them still matters - an exit storm consumes the
        # same broker capacity an entry storm does, so entries feel the
        # pressure even when exits do not.
        self._note_order_submitted(qty, price)
        _rate_ok, _rate_why = self.rate_limit_check(qty, price)
        if not _rate_ok:
            logger.warning(
                f"{symbol}: exit exceeds a rate limit ({_rate_why}) - submitting "
                f"ANYWAY. Exits are never rate-blocked; an unclosed position is "
                f"the larger risk. New ENTRIES are already refused."
            )

        try:
            order = None
            if limit_px:
                try:
                    order = self.broker.submit_limit_order(symbol, qty, limit_px, side=side)
                    self._limit_exit_attempts[symbol] = _tries + 1
                    logger.info(
                        f"{symbol}: exit routed as a MARKETABLE LIMIT at {limit_px} "
                        f"({exit_cfg.get('slippage_pct', 0.3)}% through {price}) - "
                        f"bounds the fill instead of accepting any price the book offers"
                    )
                except Exception as e:
                    # A limit route that cannot even be SUBMITTED must never
                    # cost the exit. Bounding the fill price is an improvement;
                    # getting out at all is the requirement.
                    logger.warning(
                        f"{symbol}: marketable-limit exit could not be submitted "
                        f"({type(e).__name__}: {e}) - falling back to a market order"
                    )
                    order = None
            if order is None:
                order = self.broker.submit_market_order(symbol, qty, side=side)
        except Exception as e:
            logger.error(f"Failed to submit exit order for {symbol}: {e}")
            return None

        # Mirror the immediate cache update in submit_entry_order, for the
        # same reason - keeps pre_entry_check() accurate for any entry
        # checked later in the SAME poll cycle, not just after the next
        # refresh_account_snapshot(). A partial sale leaves the symbol open, so
        # the open-position count must NOT decrement for it; see
        # is_partial_exit, which uses the quantities rather than the reason
        # string because the top take-profit tier sells the whole position.
        if price:
            # Selling a long returns cash; BUYING to cover a short spends it.
            self._buying_power += direction * qty * price
            self._total_exposure_usd = max(0.0, self._total_exposure_usd - qty * price)
        if not is_partial_exit(reason, qty, qty_before):
            self._open_symbols.discard(symbol)
            self._entry_recorded_at.pop(symbol, None)
            self._pending_cost.pop(symbol, None)
            # Computed here rather than from trade_record below, because the
            # record-keeping block is wrapped in its own try/except and must
            # never be what decides whether a cooldown gets applied.
            entry_px = self.open_entries.get(symbol)
            # A long loses when price falls below entry; a short loses when it
            # RISES above it.
            closed_at_loss = bool(
                entry_px and price is not None
                and ((price > entry_px) if is_cover else (price < entry_px))
            )
            self._note_position_closed(symbol, closed_at_loss)

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
                "burst_logic": meta.get("burst_logic") or "n/a",
                "price_source": meta.get("price_source") or "unknown",
                "signal_pct": meta.get("signal_pct"),
                "list_source": meta.get("list_source"),
                # Max favorable / adverse excursion: the best and worst
                # unrealized moves this position saw before closing. Purely
                # observational, but they answer a question the exit reason
                # alone cannot - whether a loser was ever actually winning.
                # Until these were recorded, that could only be reconstructed
                # by re-fetching minute bars after the fact.
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "entry_rsi": meta.get("rsi"),
                "exit_time": now_iso,
                "exit_price": price,
                "qty": qty,
                "exit_reason": reason,
                "exit_rsi": exit_rsi,
                "stop_loss_used": is_stop_loss_exit(reason),
                "order_id": order.id if hasattr(order, "id") else None,
            }
            # EXIT SLIPPAGE: what the exit rule asked for vs what the broker
            # actually gave. `price` above is the DECISION price - the level
            # the stop or target fired at - and the fill can be well away from
            # it. On 2026-09-02 a -1.0% stop realized -1.46% (NOW), -1.59%
            # (WDAY) and -1.07% (CRM); those numbers only existed because they
            # were computed by hand from the log afterwards. Entry slippage has
            # been captured since 2026-08-20 and the exit half never was, so
            # half of the single largest recurring cost was invisible.
            #
            # Recorded, never acted on here: it is a measurement, and the thing
            # that acts on it is marketable_limit_exits.
            fill_px = None
            for attr in ("filled_avg_price", "avg_fill_price"):
                raw = getattr(order, attr, None)
                if raw:
                    try:
                        fill_px = float(raw)
                        break
                    except (TypeError, ValueError):
                        continue
            _meta = self.entry_meta.get(symbol) or {}
            trade_record["entry_slippage_pct"] = _meta.get("entry_slippage_pct")
            trade_record["decision_price"] = price
            trade_record["fill_price"] = fill_px
            if fill_px and price:
                # Signed so it reads the same way for both sides: NEGATIVE is
                # always worse for this position. A sell filled below the
                # decision price and a cover filled above it are both adverse.
                trade_record["exit_slippage_pct"] = round(
                    direction * (fill_px - price) / price * 100, 4)
            else:
                trade_record["exit_slippage_pct"] = None

            # The fill is the truth for P&L when we have it; the decision price
            # is only a stand-in for it.
            price_for_pnl = fill_px or price

            if entry_price and price_for_pnl is not None:
                # direction flips the sign for a buy-to-cover: a short that is
                # bought back BELOW its entry made money, and recording that as
                # a loss would corrupt every downstream P&L read (the daily
                # report, trade_history.csv, session-metrics, the daily-loss
                # limit's own accounting).
                trade_record["pl"] = direction * (price_for_pnl - entry_price) * qty
                trade_record["pl_pct"] = direction * (price_for_pnl - entry_price) / entry_price * 100
            else:
                trade_record["pl"] = 0
                trade_record["pl_pct"] = 0
            self.trades_log.append(trade_record)
            # One row per trade for the exit-rule replay: the market/stock
            # state captured at ENTRY (stashed on entry_meta by main.py)
            # joined to the outcome computed here. Guarded separately from
            # the rest of this block - research data must never be able to
            # interrupt an exit that has already been submitted.
            try:
                from src.analytics import trade_recorder as _TR
                _TR.record_context(_TR.build_context_row(symbol, meta, trade_record))
            except Exception as _ce:
                logger.debug(f"{symbol}: trade context row not written: {_ce}")
            # Queue the post-exit price check. Holds a reference to the row, so
            # filling it in later updates the report and the CSV in place.
            try:
                delay = self.config["trading"].get("post_exit_track_minutes", 15)
                self._post_exit_pending.append({
                    "row": trade_record,
                    "symbol": symbol,
                    "exit_price": price,
                    "due_at": time.time() + delay * 60,
                })
            except Exception as e:
                logger.debug(f"Could not queue the post-exit check for {symbol}: {e}")
            self._add_realized_pnl(trade_record["pl"])

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

            color = ANSI_YELLOW if is_partial_exit(reason, qty, qty_before) else ANSI_RED
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

    def exit_qty_actually_submitted(self, symbol, default):
        """
        The qty the last submit_exit_order() call for `symbol` actually
        submitted, if the phantom-entry guard corrected it down from what the
        caller asked for - otherwise `default`.

        Call this AFTER submit_exit_order() and BEFORE strategy.confirm_exit(),
        with the qty the caller itself computed as `default`. Without it,
        confirm_exit commits whatever the caller originally decided even when
        this class sold less (a broker-side partial fill it corrected for),
        which subtracts too much from qty_remaining and can take it negative.

        Pops the value - a correction is a one-time fact about that specific
        exit call, not a standing override for the symbol's next one.
        """
        return self._last_exit_qty.pop(symbol, default)

    def _add_realized_pnl(self, pl):
        """
        Accumulate realized P&L for TODAY only. trades_log is never cleared in
        this long-running process, so summing it would carry every previous
        day's losses into today's limit - the same trap _csv_appended_count
        exists to avoid for the CSV. The date stamp resets the running total
        on the first exit of a new day.
        """
        today = datetime.now().date()
        if self._pnl_date != today:
            self._pnl_date = today
            self._realized_pnl_today = 0.0
        self._realized_pnl_today += pl or 0.0

    def _compute_daily_pnl(self, account, positions):
        """
        Daily P&L including OPEN positions, not just closed ones.

        Preferred source is the broker's own equity vs. last_equity (previous
        session's closing equity). That is authoritative, already includes
        unrealized P&L on everything currently held, and rolls over on its own
        each session, so nothing here has to track when a day starts.

        Falls back to realized-today plus unrealized-on-open-positions if the
        broker doesn't supply last_equity, so the limit still works rather
        than silently reverting to never firing.
        """
        last_equity = getattr(account, "last_equity", None)
        if last_equity not in (None, ""):
            try:
                return float(account.equity) - float(last_equity)
            except (TypeError, ValueError):
                pass

        unrealized = 0.0
        for p in positions.values():
            try:
                unrealized += float(getattr(p, "unrealized_pl", 0) or 0)
            except (TypeError, ValueError):
                continue
        return self._realized_pnl_today + unrealized

    def daily_loss_limit_usd(self):
        """
        Today's loss ceiling in dollars.

        Fixed by default. When trading.daily_loss_limit.mode is
        "percent_of_equity" it is a FRACTION OF THE ACCOUNT instead, which is
        the right shape for a risk limit - $500 means something different on a
        $50k account than on a $200k one, and hardcoding dollars means the
        limit silently changes meaning as the account moves.

        THE CEILING IS THE POINT, and it is why this is not simply
        pct x equity. A limit that rises automatically with the balance lets a
        good run quietly authorise larger losses - the account grows, the
        permitted daily loss grows with it, and nobody ever decided that.
        Raising ceiling_usd is a deliberate act, taken after the strategy has
        shown a stable edge, not a side effect of a profitable week. The floor
        is the mirror image: a drawdown must not shrink the limit to something
        so tight that one ordinary opening trade ends the session.

        Falls back to max_daily_loss_usd on any problem, including equity not
        yet being known - a risk limit must never fail OPEN.
        """
        t = self.config.get("trading") or {}
        static = t.get("max_daily_loss_usd")
        cfg = t.get("daily_loss_limit") or {}
        if (cfg.get("mode") or "fixed") != "percent_of_equity":
            return static
        try:
            equity = float(self._equity or 0)
            if equity <= 0:
                return static
            pct = float(cfg.get("pct_of_equity", 0.75)) / 100.0
            floor = float(cfg.get("floor_usd", static or 500))
            ceiling = float(cfg.get("ceiling_usd", static or 1000))
            limit = max(floor, min(ceiling, equity * pct))
            if self._logged_loss_limit != round(limit, 2):
                self._logged_loss_limit = round(limit, 2)
                logger.info(
                    f"Daily loss limit: ${limit:,.2f} "
                    f"({cfg.get('pct_of_equity', 0.75)}% of ${equity:,.2f} equity, "
                    f"floor ${floor:,.0f} / ceiling ${ceiling:,.0f}). The ceiling "
                    f"does NOT rise with the account on its own - raise it "
                    f"deliberately once the edge is established."
                )
            return limit
        except Exception as e:
            logger.warning(
                f"Percent-of-equity loss limit failed ({e}) - falling back to "
                f"the fixed ${static} limit."
            )
            return static

    def check_daily_loss_limit(self):
        """
        True once the day is down by more than max_daily_loss_usd.

        self.daily_pnl is refreshed every poll by refresh_account_snapshot().
        It previously was not: daily_pnl was assigned 0.0 in __init__ and
        reassigned only inside get_daily_pnl(), which had no callers anywhere
        in the codebase - so it stayed 0.0 for the life of the process and
        this check could never return True. The limit had never once fired.
        get_daily_pnl() would not have worked even if called, since it read
        account.daily_pnl behind a hasattr() guard and Alpaca's account model
        has no such field, making it a silent no-op.
        """
        max_loss = self.daily_loss_limit_usd()
        if not max_loss:
            return False
        if self.daily_pnl <= -abs(max_loss):
            logger.warning(
                f"Daily loss limit exceeded: ${self.daily_pnl:,.2f} "
                f"(limit ${-abs(max_loss):,.2f}) - flattening and stopping for the day"
            )
            return True
        return False

    def loss_tier_multiplier(self):
        """
        Size scalar from how deep the day's loss already is, or 1.0.

        THE GAP THIS FILLS. Until now the day was binary: fine, or over. The
        velocity warnings fired at 40/60/80% of the ceiling and did nothing but
        log, so a session that was clearly not working kept taking full-size
        trades until the hard stop. 2026-09-02 burned $500 in eight minutes
        with three warnings printed along the way and every entry after them
        sized exactly as if nothing had happened.

        Tiers are FRACTIONS of the computed limit, never dollars, so they keep
        meaning the same thing as the account grows - the same reason the limit
        itself became percent-of-equity.

        Composes multiplicatively with regime_size_multiplier, and that is
        deliberate: a choppy tape (0.5x) and a day already half-spent (0.5x)
        are two independent reasons to be smaller, and a system that honoured
        only the larger of them would ignore one of them entirely.

        Never returns 0 - stopping entirely is the hard limit's job, and having
        two mechanisms that can both end the day makes it ambiguous which one
        did.
        """
        cfg = (self.config.get("trading") or {}).get("loss_tiers") or {}
        if not cfg.get("enabled"):
            return 1.0
        try:
            limit = abs(self.daily_loss_limit_usd() or 0)
            if limit <= 0:
                return 1.0
            loss = -float(self.daily_pnl or 0)
            if loss <= 0:
                return 1.0
            fraction = loss / limit
            mult = 1.0
            for tier in sorted(cfg.get("tiers") or [], key=lambda t: t.get("at_fraction", 0)):
                if fraction >= float(tier.get("at_fraction", 1.0)):
                    mult = float(tier.get("size_multiplier", 1.0))
            if mult != self._logged_loss_tier:
                self._logged_loss_tier = mult
                logger.warning(
                    f"LOSS TIER: down ${loss:,.2f}, {fraction * 100:.0f}% of the "
                    f"${limit:,.2f} daily limit - new entries sized at {mult:g}x. "
                    f"Exits and open positions are untouched."
                )
            return mult
        except Exception as e:
            logger.error(f"loss_tier_multiplier failed ({e}) - no size reduction applied")
            return 1.0

    def check_loss_velocity(self, now=None):
        """
        Soft warning as the day's loss approaches max_daily_loss_usd, well
        before the hard stop fires. Returns a note string the first time each
        threshold is crossed, else None. NEVER halts anything.

        Why this exists (PENDING_WORK.md item 8). max_daily_loss_usd was
        doing double duty as both "circuit breaker" and "the only number
        that exists": the day was either fine or over, with nothing in
        between and no signal on the way there. At $500 that gap matters
        more, not less - 2026-08-31 closed at -$546.24, which under today's
        setting would have flattened the book mid-session with no prior
        warning that it was heading there.

        Two readings, because they answer different questions:
          - DEPTH: loss as a fraction of the ceiling. "60% of the way to the
            stop" is the number a human acts on.
          - VELOCITY: dollars lost per minute since the first check today,
            projected forward to when the ceiling would be reached at this
            rate. -$300 by 10:00 and -$300 by 15:30 are the same depth and
            very different days.

        Each threshold fires ONCE per day (latched in _loss_warnings_fired,
        reset by the same day-rollover the realized-P&L accumulator uses), so
        a ~10s poll cannot turn this into a wall of identical lines.
        """
        cfg = (self.config.get("trading") or {}).get("loss_velocity_warning") or {}
        if not cfg.get("enabled"):
            return None
        # The SAME number check_daily_loss_limit uses, so the warnings are
        # fractions of the limit that will actually fire rather than of a
        # static value the percent-of-equity mode may have replaced.
        max_loss = abs(self.daily_loss_limit_usd() or 0)
        if not max_loss:
            return None

        now = now or datetime.now()
        today = now.date()
        if getattr(self, "_loss_warn_day", None) != today:
            self._loss_warn_day = today
            self._loss_warnings_fired = set()
            self._loss_watch_started_at = now

        loss = -self.daily_pnl          # positive when the day is DOWN
        if loss <= 0:
            return None

        fraction = loss / max_loss
        thresholds = sorted(cfg.get("warn_fractions") or [0.4, 0.6, 0.8])
        crossed = [t for t in thresholds
                   if fraction >= t and t not in self._loss_warnings_fired]
        if not crossed:
            return None
        level = max(crossed)
        self._loss_warnings_fired.update(crossed)

        elapsed_min = max(
            (now - self._loss_watch_started_at).total_seconds() / 60.0, 1e-9
        )
        rate = loss / elapsed_min      # $/min
        remaining = max(max_loss - loss, 0.0)
        if rate > 0 and remaining > 0:
            eta_min = remaining / rate
            eta = (f"at this rate the ${max_loss:,.0f} stop is ~{eta_min:.0f} min away "
                   f"(~{(now + timedelta(minutes=eta_min)):%H:%M})")
        elif remaining <= 0:
            eta = "the hard stop is already met"
        else:
            eta = "no measurable rate yet"

        note = (
            f"LOSS VELOCITY WARNING: down ${loss:,.2f}, {fraction * 100:.0f}% of the "
            f"${max_loss:,.0f} daily stop, after {elapsed_min:.0f} min "
            f"(${rate:,.2f}/min) - {eta}. This is a WARNING ONLY; entries and exits "
            f"continue normally until the hard limit."
        )
        logger.warning(note)
        self.loss_velocity_notes.append(note)
        return note

    def get_daily_pnl(self):
        """Daily P&L (realized + unrealized) as of the last snapshot refresh."""
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
              # PER-POSITION GUARD. Without it, one malformed row aborts the
              # whole sweep and every OTHER position stays open overnight -
              # and this is the 16:00 time stop, the last thing between the
              # account and holding risk it never intended to carry. A symbol
              # that cannot be parsed is one to log and step over, not a
              # reason to stop flattening the rest.
              try:
                # The SIGN matters and used to be thrown away here. A short is
                # closed by BUYING it back; submitting a sell doubles it. The
                # 16:00 time stop runs through this loop, so before 2026-09-02
                # any short held at 16:00 got bigger instead of closing - found
                # 2026-08-28 (CRWD/MTCH/OKTA, ~$1,015). The bot never intends
                # to be short, so reaching this branch is itself evidence of
                # another bug - but leaving a real short open overnight is
                # worse than closing it, so it gets closed and logged loudly.
                raw_qty = float(position.qty)
                qty = int(abs(raw_qty))
                side = "buy" if raw_qty < 0 else "sell"
                if qty > 0:
                    if side == "buy":
                        logger.error(
                            f"{symbol}: flattening a SHORT position ({raw_qty:g} shares) - "
                            f"buying to cover. The bot never opens shorts deliberately, so "
                            f"this position is evidence of a separate bug; check how it was "
                            f"opened (phantom-entry path, or a position adopted at startup)."
                        )
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

                    order = self.submit_exit_order(symbol, qty, "FLATTEN_ALL", price, side=side)
                    if order is not None:
                        flattened_symbols.append(symbol)
                        logger.info(
                            f"Flattened {symbol}: {qty} shares at {price} "
                            f"({'bought to cover' if side == 'buy' else 'sold'})"
                        )
                    else:
                        logger.error(f"Failed to flatten {symbol}: {qty} shares - order was not submitted")
              except Exception as pos_err:
                logger.error(
                    f"{symbol}: could not be flattened ({pos_err}) - SKIPPING it and "
                    f"continuing with the rest of the book. This symbol may still be "
                    f"open; check the account manually."
                )
                continue

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

            # Written to a temp file and renamed, NOT straight to filepath.
            # open(filepath, "w") truncates immediately, so a serialization
            # failure part-way through left a half-written trades.json behind -
            # which is what happened on 2026-08-20: an un-serializable UUID in
            # order_id raised mid-dump, and the daily report then died reading
            # the truncated file ("Expecting value: line 16 column 17"). One
            # fault, two failures, and no report for the day. A rename is
            # atomic, so the previous good file survives any failure here.
            #
            # default=str covers the UUID itself: Alpaca returns order.id as a
            # UUID object, which json cannot encode. Applied broadly rather
            # than to that one field so any future non-JSON type degrades to
            # its string form instead of destroying the file.
            tmp = f"{filepath}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.trades_log, f, indent=2, default=str)
            os.replace(tmp, filepath)
            logger.info(f"Trades log saved to {filepath}")
        except Exception as e:
            logger.error(f"Error saving trades log: {e}")
            try:
                if os.path.exists(f"{filepath}.tmp"):
                    os.remove(f"{filepath}.tmp")
            except Exception:
                pass

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
            "date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic", "price_source", "signal_pct", "post_exit_pct", "post_exit_note", "entry_rsi",
            "mfe_pct", "mae_pct",
            "exit_time", "exit_price", "exit_reason", "stop_loss_used", "exit_rsi",
            "qty", "pl", "pl_pct",
            # Appended at the END, never inserted - repair_header remaps by name
            # and a column added mid-schema is what made the old header rot
            # unreadable. Any new column goes here.
            "list_source",
            # 2026-09-02: execution cost, the one number that scales with every
            # trade and had never been persisted. entry_slippage_pct was
            # computed and logged since 2026-08-20 and discarded; the exit half
            # was not measured at all. Both are signed so NEGATIVE is always
            # adverse for the position, whichever side it is.
            "entry_slippage_pct", "decision_price", "fill_price", "exit_slippage_pct",
        ]
        try:
            path = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Same stale-header fault as the signal journal: this file was
            # created with 14 columns and has been writing 21 ever since, which
            # put the burst note under entry_rsi for every reader.
            # Every past version of this schema, oldest first - see
            # csv_schema. v1 predates the burst/excursion/post-exit columns.
            legacy = [
                # v1: before the burst/excursion/post-exit columns
                [
                    "date", "symbol", "entry_time", "entry_price", "entry_method",
                    "entry_rsi", "exit_time", "exit_price", "exit_reason",
                    "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct",
                ],
                # v2: before list_source
                [c for c in fieldnames if c not in
                 ("list_source", "entry_slippage_pct", "decision_price",
                  "fill_price", "exit_slippage_pct")],
                # v3: before the slippage columns
                [c for c in fieldnames if c not in
                 ("entry_slippage_pct", "decision_price", "fill_price",
                  "exit_slippage_pct")],
            ]
            repair_header(str(path), fieldnames, legacy_schemas=legacy)
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
