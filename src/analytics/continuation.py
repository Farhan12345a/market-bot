"""
Continuation factors: does a burst have the characteristics of a move that
keeps going, or one that has already happened?

Every factor here is computed from data the bot ALREADY collects - streamed
bars (price + volume), the per-symbol price history the exit rules maintain,
the SPY samples already tracked for excess-return, and the daily bars the
screener already fetches. No new subscriptions, no new API calls.

DELIBERATELY NOT SCORED BY DEFAULT. use_continuation_score ships false, and the
factors are written to the signal journal regardless, because the weights are
the part nobody can know yet. A 7-factor weighted score fitted on five sessions
describes the past and predicts nothing; the honest sequence is to log the
factors against forward returns for a couple of weeks and then fit weights to
what actually correlated. The same discipline the opening-move feature is under.

Each factor returns 0-100, or None when its inputs are not available yet.
None is not zero - a missing factor is dropped from the weighting rather than
scored as bad.
"""
import logging

logger = logging.getLogger(__name__)


def efficiency_ratio(prices):
    """
    Momentum PERSISTENCE, 0-100: how directly price travelled, not how far.

        ER = |end - start| / sum(|each step|)

    100 = a straight line up. 0 = a lot of motion that went nowhere. This is
    what separates 100 -> 105 -> 107 -> 106 -> 108 -> 110 (a trend) from
    100 -> 108 -> 104 -> 109 -> 103 -> 110 (a whipsaw) when both end at +10%.

    Directly relevant to this strategy's central failure: trades closing with
    MFE +0.00%, which never traded above the fill for a single tick. Those are
    entries into motion, not into trend.
    """
    if not prices or len(prices) < 3:
        return None
    path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if path <= 0:
        return None
    net = prices[-1] - prices[0]
    if net <= 0:
        return 0.0          # went nowhere or down: no upward persistence
    return max(0.0, min(100.0, net / path * 100))


def relative_strength(symbol_pct, benchmark_pct):
    """
    Excess return over the benchmark, mapped to 0-100.

    Separates "this stock is strong" from "the whole market went up". Already
    recorded per signal as excess_vs_spy_pct; this only rescales it so it can
    sit alongside the other factors.

    +2% excess or better saturates at 100; matching the benchmark is 50.
    """
    if symbol_pct is None or benchmark_pct is None:
        return None
    excess = symbol_pct - benchmark_pct
    return max(0.0, min(100.0, 50 + excess * 25))


def volume_acceleration(volumes):
    """
    Is volume RISING as the move continues, 0-100.

    The derivative, not the level. Rising price on rising volume means new
    participation is arriving; rising price on falling volume is the same buyers
    running out. Compares the most recent third of the window against the rest.

    Needs at least 6 samples to have two thirds worth comparing.
    """
    if not volumes or len(volumes) < 6:
        return None
    cut = max(2, len(volumes) // 3)
    recent, earlier = volumes[-cut:], volumes[:-cut]
    if not earlier:
        return None
    r = sum(recent) / len(recent)
    e = sum(earlier) / len(earlier)
    if e <= 0:
        return None
    ratio = r / e
    # 1.0x (flat) -> 50, 2.0x -> 100, 0.5x -> 0
    return max(0.0, min(100.0, 50 + (ratio - 1.0) * 50))


def relative_volume(rvol):
    """
    RVOL, 0-100: is the move backed by participation, or drifting on nothing?

    Distinct from volume_acceleration, which asks whether volume is RISING
    within the move. This asks whether today's volume is unusual for this
    symbol AT ALL. A move on 3x normal volume and a move on 0.6x normal volume
    can both show rising volume within themselves.

    Only became measurable on 2026-08-20: before that fix `end=today` coerced
    to midnight and excluded the current session, so this returned exactly
    1.00x for every symbol forever. Give it a week of real numbers before
    weighting it heavily.

    1.0x (normal) -> 50, 2.5x or more -> 100.
    """
    if rvol is None or rvol <= 0:
        return None
    return max(0.0, min(100.0, 50 + (rvol - 1.0) / 1.5 * 50))


def spread_quality(spread_pct):
    """
    Bid-ask spread as a share of price, 0-100, where TIGHT IS GOOD.

    The precise version of what min_stock_price approximates. That setting uses
    price as a proxy for spread, which is crude in both directions: it rejects a
    cheap stock that happens to be tight and accepts an expensive one that is
    wide. Measuring the spread directly is strictly better - and it is a real
    cost, paid on entry and again on exit, against a strategy whose average
    winner is well under 1%.

    0.05% or tighter -> 100; 0.5% or wider -> 0.
    """
    if spread_pct is None or spread_pct < 0:
        return None
    return max(0.0, min(100.0, (0.5 - spread_pct) / 0.45 * 100))


def vwap_position(price, vwap, atr_pct=None):
    """
    Where price sits relative to VWAP, 0-100.

    Above VWAP means buyers are still defending the session's average paid
    price. Below it means the opposite, whatever the day's return says.

    Scaled by the symbol's own volatility when available, so "1% above VWAP"
    means something different for ACN than for MARA. That normalisation is the
    same idea as the 2-sigma burst definition, applied to extension.
    """
    if not price or not vwap or vwap <= 0:
        return None
    dist_pct = (price - vwap) / vwap * 100
    scale = atr_pct if (atr_pct and atr_pct > 0) else 1.0
    return max(0.0, min(100.0, 50 + (dist_pct / scale) * 25))


def exhaustion(signal_pct, price, vwap, atr_pct=None):
    """
    How far the move has ALREADY run, 0-100, where HIGH IS BAD.

    The one factor here with direct evidence behind it. On 2026-08-19 signals
    of 1.0% or more produced 1 winner in 6 for -$531, while signals under 1.0%
    produced 4 winners in 14 for -$181. A stock up 2% in three minutes has made
    its move, and the -1.0% stop then sits exactly where the natural pullback
    lands.

    Combines how big the trigger was with how stretched price is from VWAP -
    a large move that is still near VWAP is far less extended than the same
    move a long way above it.
    """
    if signal_pct is None:
        return None
    # Signal size: 0.3% -> 0, 2.0% -> 100
    by_signal = max(0.0, min(100.0, (signal_pct - 0.3) / 1.7 * 100))

    by_extension = None
    if price and vwap and vwap > 0:
        dist = (price - vwap) / vwap * 100
        scale = atr_pct if (atr_pct and atr_pct > 0) else 1.0
        # 2x the symbol's own daily range above VWAP is fully extended
        by_extension = max(0.0, min(100.0, dist / scale / 2 * 100))

    if by_extension is None:
        return by_signal
    return max(by_signal, by_extension)


def breakout_quality(price, prior_high, opening_high):
    """
    Is this breaking a level that means something, 0-100.

    Clearing the prior session's high is worth more than clearing the opening
    range, which is worth more than drifting up through neither.
    """
    if not price:
        return None
    score, seen = 0.0, False
    if opening_high:
        seen = True
        score += 40 if price > opening_high else 0
    if prior_high:
        seen = True
        score += 60 if price > prior_high else 0
    if not seen:
        return None
    return max(0.0, min(100.0, score))


def continuation_score(factors, weights):
    """
    Weighted blend of whichever factors are available, 0-100.

    Missing factors are DROPPED and the remaining weights renormalised, rather
    than scored as zero - "not measurable yet" and "measurably bad" are
    different claims, and conflating them would penalise a symbol for the bot's
    own data gaps. Exhaustion is subtracted; everything else adds.
    """
    total_w = 0.0
    total = 0.0
    for name, value in factors.items():
        if value is None:
            continue
        w = weights.get(name, 0.0)
        if not w:
            continue
        total += (100 - value) * abs(w) if name == "exhaustion" and w < 0 else value * w
        total_w += abs(w)
    if total_w <= 0:
        return None
    return max(0.0, min(100.0, total / total_w))
