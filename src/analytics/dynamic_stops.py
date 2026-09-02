"""
Dynamic stop placement from a symbol's own measured behaviour, rather than
one flat percentage for every name.

PENDING_WORK.md backlog item 3 - "the most technically sound of the batch",
blocked until 2026-09-02 on both of its inputs and unblocked by the same
day's work:

  - ATR: `_get_volatility_percentile` was a 5-bucket ladder that computed a
    real atr_pct and threw it away. It now keeps it (item 0d).
  - MAE percentiles: mae_pct has been logged per trade since 2026-08-24;
    `ops/mae-percentiles.py` turns it into percentile bands.

THE RULE (option (b), chosen by the user 2026-09-02):

    stop = the symbol's own MAE percentile when there is enough of its own
           history to mean anything (n >= min_samples), otherwise the pooled
           percentile across all symbols, otherwise the static config stop.

    ...then blended with an ATR floor so a genuinely volatile name is not
    given a stop inside its own noise.

    ...then CAPPED. The result can only ever be TIGHTER than
    final_exit_loss_pct, never wider.

THE CAP IS THE WHOLE SAFETY ARGUMENT, and it is why option (a) was rejected:
`max(ATR, MAE)` takes the WIDER of two numbers and both can exceed 1%, so a
3%-ATR name would have been handed a stop several times looser than today's
-1.0% - larger losses per trade, the opposite of the goal. Capping makes this
a "tighten where the evidence supports it" rule with a bounded downside: in
the worst case every symbol falls back to exactly today's behaviour.

WHY MILESTONES, NOT EVERY TICK. Recalculating continuously lets the stop
chase the price and thrash - the level moves under the position on every
poll, and the trade exits on noise in the stop rather than noise in the
market. Stops are therefore recomputed only when a position crosses a
MILESTONE (entry, +0.5%, +1.0% by default): discrete, monotonic, and each
recalculation is triggered by the position having genuinely improved.
"""

import math

# A symbol needs at least this many of its own recorded trades before its
# personal MAE distribution is used instead of the pooled one. Below it the
# "percentile" is a rank among almost nothing - the same guard
# ops/mae-percentiles.py applies with --min-n.
DEFAULT_MIN_SAMPLES = 15

# Percentile of the MAE distribution the stop is placed at. 75 means "wide
# enough that three quarters of past trades in this name would not have been
# stopped out by their own drawdown".
DEFAULT_PERCENTILE = 75

# Milestones (in % gain since entry) at which the stop is recomputed.
DEFAULT_MILESTONES = (0.0, 0.5, 1.0)


def percentile(sorted_vals, p):
    """Nearest-rank percentile of an ASCENDING-sorted, non-empty list."""
    n = len(sorted_vals)
    k = max(1, min(n, math.ceil(p / 100 * n)))
    return sorted_vals[k - 1]


def mae_level(mae_values, pct=DEFAULT_PERCENTILE):
    """
    The drawdown level that `pct` percent of trades stayed within, as a
    NEGATIVE percentage. None when there is nothing to measure.

    mae_pct is <= 0 by construction (TradeManager.excursions computes
    (lowest - entry) / entry), so "75% stayed within X" is the (100-75)th
    percentile of the ascending values - the same convention
    ops/mae-percentiles.py documents and is tested against.
    """
    vals = sorted(v for v in mae_values if v is not None)
    if not vals:
        return None
    return percentile(vals, 100 - pct)


class DynamicStops:
    """
    Stop levels from per-symbol history, with a pooled fallback and a hard
    cap at the configured static stop.

    Reads history it is GIVEN (a {symbol: [mae_pct, ...]} map, built by the
    caller from trade_history.csv) rather than reading the CSV itself - so
    this stays a pure calculation, testable without a filesystem, and the
    trading loop decides when the cost of loading history is paid.
    """

    def __init__(self, config, history=None, atr_by_symbol=None):
        trading = (config or {}).get("trading", config or {})
        self.cfg = (trading.get("dynamic_stops") or {})
        self.enabled = bool(self.cfg.get("enabled"))
        self.static_stop = abs(float(trading.get("final_exit_loss_pct", -1.0) or -1.0))
        self.min_samples = int(self.cfg.get("min_samples", DEFAULT_MIN_SAMPLES))
        self.pct = float(self.cfg.get("mae_percentile", DEFAULT_PERCENTILE))
        self.atr_multiple = float(self.cfg.get("atr_multiple", 0) or 0)
        self.floor_pct = abs(float(self.cfg.get("min_stop_pct", 0.25) or 0.25))
        self.milestones = tuple(self.cfg.get("milestones") or DEFAULT_MILESTONES)
        self.history = {k: list(v) for k, v in (history or {}).items()}
        self.atr_by_symbol = dict(atr_by_symbol or {})

    # ---- inputs ----------------------------------------------------------

    def _pooled(self):
        out = []
        for vals in self.history.values():
            out.extend(vals)
        return out

    def _mae_component(self, symbol):
        """(level, source) - level is a negative %, source names which
        distribution produced it, so the log can say why a stop is where it
        is rather than presenting a number with no provenance."""
        own = self.history.get(symbol) or []
        if len(own) >= self.min_samples:
            lvl = mae_level(own, self.pct)
            if lvl is not None:
                return lvl, f"own MAE p{self.pct:g} (n={len(own)})"
        pooled = self._pooled()
        if len(pooled) >= self.min_samples:
            lvl = mae_level(pooled, self.pct)
            if lvl is not None:
                return lvl, f"pooled MAE p{self.pct:g} (n={len(pooled)}, own n={len(own)})"
        return None, f"no MAE history (own n={len(own)})"

    def _atr_component(self, symbol):
        """A stop inside a symbol's own 1-bar noise gets hit by nothing
        happening. Returns a negative % or None when ATR is unknown or the
        multiple is switched off."""
        if not self.atr_multiple:
            return None
        atr = self.atr_by_symbol.get(symbol)
        if not atr or atr <= 0:
            return None
        return -abs(float(atr) * self.atr_multiple)

    # ---- the rule --------------------------------------------------------

    def stop_for(self, symbol, gain_pct=0.0):
        """
        (stop_pct, reason) for `symbol` at its current `gain_pct` since entry.

        stop_pct is NEGATIVE (e.g. -0.62 means "exit at -0.62% from entry").
        Returns the static configured stop unchanged when disabled or when
        there is no evidence to do better - "not measurable" must never make
        a position riskier than the default.
        """
        static = -self.static_stop
        if not self.enabled:
            return static, "dynamic stops disabled - static final_exit_loss_pct"

        mae, mae_src = self._mae_component(symbol)
        atr = self._atr_component(symbol)

        if mae is None and atr is None:
            return static, f"{mae_src}, no ATR - falling back to the static stop"

        # Widest of the evidence-based candidates, so the stop clears both the
        # symbol's typical drawdown AND its bar-to-bar noise...
        candidate = min(x for x in (mae, atr) if x is not None)

        # ...then capped so it can NEVER be looser than the static stop. This
        # is the safety property that makes the whole feature bounded: the
        # worst case is today's behaviour, never something riskier.
        capped = max(candidate, static)

        # ...and floored, so a freakishly quiet name is not handed a stop so
        # tight the spread alone triggers it.
        final = min(-self.floor_pct, capped)

        bits = [mae_src]
        if atr is not None:
            bits.append(f"ATR x{self.atr_multiple:g} = {atr:.3f}%")
        if capped != candidate:
            bits.append(f"CAPPED at the static {static:.2f}%")
        if final != capped:
            bits.append(f"floored at {-self.floor_pct:.2f}%")
        bits.append(f"milestone {self._milestone_for(gain_pct):g}%")
        return round(final, 3), "; ".join(bits)

    def _milestone_for(self, gain_pct):
        """The highest milestone this position has reached."""
        reached = [m for m in self.milestones if gain_pct >= m]
        return max(reached) if reached else min(self.milestones)

    def should_recalculate(self, gain_pct, last_milestone):
        """
        True when a position has crossed into a NEW milestone band and its
        stop should be recomputed.

        This is the anti-thrash rule. Recalculating every tick lets the stop
        chase price and exit on noise in the level rather than in the market;
        milestones make each recalculation a response to the position having
        genuinely improved. Monotonic by construction - a position falling
        back below a milestone does NOT re-widen its stop, because
        last_milestone only ever moves up.
        """
        current = self._milestone_for(gain_pct)
        if last_milestone is None:
            return True
        return current > last_milestone
