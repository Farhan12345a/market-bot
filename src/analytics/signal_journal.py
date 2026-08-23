"""
Records every entry signal that fires - TAKEN OR NOT - along with the features
available at that moment, then follows each one forward to see what the symbol
actually did.

Why
---
Any ranking scheme ("buy the best 3 of the 20 that fired") is a claim that the
ones you skipped would have done worse. Nothing in this bot has ever been able
to test that claim: skipped signals were logged as prose
("entry skipped - at max_concurrent_positions") and the outcome was never
recorded, so the counterfactual vanished. Every weighting would be a guess.

This produces the dataset that makes the question answerable. It deliberately
records signals the bot REFUSED as well as ones it took, because the refused
ones are the entire control group.

Deliberately observational
--------------------------
Nothing here influences a trading decision. It is called after the entry
decision has already been made, and every public method swallows its own
exceptions, so a bug in analytics can never block, delay, or alter an order.
"""

import csv
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

JOURNAL_FIELDS = [
    "date", "signal_time", "symbol", "entry_method", "price",
    # --- features available at signal time ---
    "signal_pct",        # how far the move had already run when it fired
    "excess_vs_spy_pct",  # same move minus SPY's, i.e. what is NOT market beta
    "spy_pct",           # the market's own move over the same window
    "rvol",              # this bar's volume vs this symbol's recent bar average
    "spread_pct",        # bid-ask as a % of price, the real cost-to-trade
    "burst_width",              # how many symbols fired in this same poll
    # Opening-move history, recorded whether or not it is being SCORED. The
    # whole point is to test the "patterns repeat" claim against forward
    # returns rather than assume it: with these columns, two weeks of journal
    # answers whether a high past hit-rate actually predicts anything.
    "opening_hit_rate", "opening_avg_gain", "opening_sessions",
    # --- what the bot decided ---
    "taken", "skip_reason", "qty", "size_multiplier",
    # --- what actually happened next (the label) ---
    "price_15min", "pct_15min", "price_30min", "pct_30min",
]


class SignalJournal:
    """
    Buffers signals in memory, fills in forward returns as time passes, and
    writes completed rows to CSV.
    """

    def __init__(self, config):
        analytics = config.get("analytics", {})
        self.enabled = analytics.get("log_signals", True)
        self.path = analytics.get("signal_log_file", "logs/signal_journal.csv")
        self.horizons = analytics.get("forward_return_minutes", [15, 30])

        self._pending = []  # rows still waiting on their forward returns
        self._written = 0
        self._taken_written = 0

    # ---- recording -------------------------------------------------------

    def record(self, **fields):
        """
        Log one signal. Accepts any subset of JOURNAL_FIELDS; anything absent
        is written blank rather than guessed at.
        """
        if not self.enabled:
            return
        try:
            now = datetime.now()
            row = {k: fields.get(k) for k in JOURNAL_FIELDS}
            row["date"] = now.strftime("%Y-%m-%d")
            row["signal_time"] = now.isoformat()
            self._pending.append({"row": row, "born": now, "symbol": fields.get("symbol")})
        except Exception as e:
            logger.debug(f"SignalJournal.record failed (ignored): {e}")

    def update_forward_returns(self, price_lookup):
        """
        Fill in each pending row's forward returns once enough time has passed.

        price_lookup(symbol) -> float or None. Called once per poll with a
        function that reads whatever price source the bot is already using, so
        this costs no extra API calls beyond symbols already being watched.
        """
        if not self.enabled or not self._pending:
            return
        try:
            now = datetime.now()
            for entry in self._pending:
                row, born, symbol = entry["row"], entry["born"], entry["symbol"]
                base = row.get("price")
                if not base:
                    continue
                for minutes in self.horizons:
                    key_price, key_pct = f"price_{minutes}min", f"pct_{minutes}min"
                    if row.get(key_price) is not None:
                        continue  # already captured
                    if now - born < timedelta(minutes=minutes):
                        continue  # not due yet
                    price = price_lookup(symbol)
                    if price:
                        row[key_price] = round(price, 4)
                        row[key_pct] = round((price - base) / base * 100, 4)
        except Exception as e:
            logger.debug(f"SignalJournal.update_forward_returns failed (ignored): {e}")

    # ---- output ----------------------------------------------------------

    def _is_complete(self, entry):
        """True once every forward horizon for this row has elapsed."""
        if not self.horizons:
            return True
        return datetime.now() - entry["born"] >= timedelta(minutes=max(self.horizons))

    def flush(self, final=True):
        """
        Write buffered rows to CSV.

        final=True (session end): writes EVERYTHING. Rows whose forward horizon
        never elapsed - a signal at 09:54 with a 30-minute horizon - are written
        with those columns blank rather than dropped, because what fired and
        what the bot decided is a valid observation on its own.

        final=False (during the session): writes only rows whose horizons have
        ALL elapsed, keeping the rest buffered so their forward returns can
        still be filled in. Called every poll so a crash, a restart or an OOM
        kill costs at most the last few minutes of signals instead of the whole
        day. Until 2026-08-21 flush() ran in exactly one place - finish_day -
        so any session that did not end cleanly wrote nothing at all.

        Append-only, so incremental writes never risk the rows already on disk.
        """
        if not self.enabled or not self._pending:
            return None

        if final:
            ready, keep = self._pending, []
        else:
            ready = [e for e in self._pending if self._is_complete(e)]
            keep = [e for e in self._pending if not self._is_complete(e)]
            if not ready:
                return None

        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            write_header = not os.path.exists(self.path)
            with open(self.path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
                if write_header:
                    writer.writeheader()
                for entry in ready:
                    writer.writerow(entry["row"])
            self._written += len(ready)
            self._taken_written += sum(1 for e in ready if e["row"].get("taken"))
            logger.info(
                f"Wrote {len(ready)} signal(s) to {os.path.abspath(self.path)}"
                + (f" ({len(keep)} still awaiting forward returns)" if keep else "")
            )
            self._pending = keep
            return self.path
        except Exception as e:
            logger.error(f"Could not write signal journal: {e}")
            return None

    def stats(self):
        """
        Counts across the whole session, not just what is still buffered.
        `taken` used to be computed from self._pending, which flush() empties -
        so end-of-day logging always reported taken=0 even on a day with 20
        entries (observed 2026-08-20). Written totals are accumulated at flush
        time instead.
        """
        return {
            "pending": len(self._pending),
            "taken": self._taken_written + sum(1 for e in self._pending if e["row"].get("taken")),
            "written": self._written,
        }
