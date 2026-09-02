"""
Correlation limiter: refuse a new entry that is highly correlated with what
is already held.

PENDING_WORK backlog item 1. `max_positions_per_sector` (2026-08-31) is a
cheap proxy that is already earning its keep - it refused 8 entries on
2026-08-31 - but it buckets by ETF MEMBERSHIP, not by measured co-movement.
Two names in different sectors can trade as one bet (a crypto miner and a
crypto exchange sit in different buckets), and two in the same sector can be
genuinely independent.

This measures the thing directly: rolling per-poll returns for each symbol,
Pearson correlation between the candidate and each open position, refuse
above a threshold.

WHAT THIS COSTS, STATED PLAINLY. It can only ever REDUCE the number of
positions taken, and the measured bottleneck right now is breadth - `edge`
(taken vs. do-nothing) has been positive on every session including losing
ones, which says selection works and opportunity is scarce. A limiter set
too tight makes a breadth problem worse. That is why:

  - the default threshold is deliberately LOOSE (0.85). At that level it
    only catches near-duplicates - the same bet wearing two tickers - not
    merely related names.
  - `min_samples` is high enough that an early-session candidate with three
    data points is never refused on a correlation computed from noise.
  - a refusal is logged with the actual coefficient and the symbol it
    collided with, so its cost is measurable in the journal rather than
    invisible.

If it turns out to refuse entries that would have won, the journal will show
it: skip_reason is recorded and those signals still carry forward returns.
"""

import math

DEFAULT_THRESHOLD = 0.85
DEFAULT_MIN_SAMPLES = 10


def returns_from_prices(prices):
    """Simple per-step returns from a price series. Length n-1."""
    out = []
    prev = None
    for p in prices:
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        if prev is not None:
            out.append((p - prev) / prev)
        prev = p
    return out


def pearson(a, b):
    """
    Pearson correlation of two equal-length return series, or None when it
    is undefined (too short, or either series is flat - a constant series has
    zero variance and no correlation with anything).
    """
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    denom = math.sqrt(va * vb)
    if denom <= 0:
        return None
    r = cov / denom
    if not math.isfinite(r):
        return None
    return max(-1.0, min(1.0, r))


def correlation_block(config, symbol, price_history, open_symbols):
    """
    (blocked, reason) for taking `symbol` while `open_symbols` are held.

    price_history is {symbol: [prices...]} - the same per-symbol deques the
    trading loop already maintains for momentum, so this costs no new data.

    Fails OPEN in every ambiguous case: not enough history, a flat series, a
    symbol with no data. Refusing on an unmeasurable correlation would be
    refusing on no evidence, and the whole argument for this feature is that
    it measures rather than assumes.
    """
    trading = (config or {}).get("trading", config or {})
    cfg = trading.get("correlation_limit") or {}
    if not cfg.get("enabled"):
        return False, None

    threshold = float(cfg.get("threshold", DEFAULT_THRESHOLD))
    min_samples = int(cfg.get("min_samples", DEFAULT_MIN_SAMPLES))
    max_correlated = int(cfg.get("max_correlated_positions", 1) or 1)

    mine = returns_from_prices((price_history or {}).get(symbol) or [])
    if len(mine) < min_samples:
        return False, None

    hits = []
    for other in open_symbols or ():
        if other == symbol:
            continue
        theirs = returns_from_prices((price_history or {}).get(other) or [])
        if len(theirs) < min_samples:
            continue
        n = min(len(mine), len(theirs))
        if n < min_samples:
            continue
        r = pearson(mine[-n:], theirs[-n:])
        if r is None:
            continue
        if r >= threshold:
            hits.append((other, r))

    if len(hits) < max_correlated:
        return False, None

    hits.sort(key=lambda kv: -kv[1])
    shown = ", ".join(f"{s} r={r:.2f}" for s, r in hits[:3])
    return True, (
        f"correlated with {len(hits)} open position(s) at or above "
        f"r={threshold:.2f} ({shown}) - the same bet held twice, not two bets"
    )
