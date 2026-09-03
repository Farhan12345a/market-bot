"""
Halt-proneness, as far as it can honestly be estimated from this data.

WHAT CANNOT BE DONE, first, because the limit shapes everything else. A trading
halt is almost always a NEWS event - an FDA decision, a merger, a short-seller
report, an earnings leak - or an LULD volatility trip caused by one. None of
that is predictable from price history. A filter claiming to avoid halts is
claiming to know tomorrow's news, and it does not.

WHAT CAN BE DONE is narrower and still worth something: halts are not
distributed evenly. They concentrate overwhelmingly in a describable KIND of
stock, and that kind IS visible in data already on hand:

  - HIGH ATR. An LULD halt trips on a 5-10% move inside five minutes. A stock
    that routinely travels 0.8% a day essentially cannot reach that; one that
    routinely travels 6% is one ordinary session away from it. This is the
    single most informative input available here.
  - LOW PRICE. LULD bands are wider below $3, and cheap stocks are where
    low-float promotion and dilution events live.
  - THIN DOLLAR VOLUME. A name that trades $3M/day gaps on order flow that a
    $300M/day name absorbs without noticing.
  - A SCHEDULED BINARY EVENT. Earnings today is the one genuinely predictive
    signal in the set, because the date is known in advance.

Returns a SCORE and the reasons behind it, not a verdict. The caller decides
what to do with it, and the reasons are returned so a refusal can be read back
rather than being an unexplained absence.

WORTH KNOWING BEFORE ENABLING THIS. The pre-open list builder deliberately ADDS
symbols reporting earnings today, because an earnings reaction is a gap plus
volume and that is what this strategy hunts. Those are also the most halt-prone
names on the watchlist. That tension is a real strategy decision, not an
oversight - turning on the earnings component of this filter partly undoes a
feature that was added on purpose. Decide it deliberately.
"""

import logging

logger = logging.getLogger(__name__)

DEFAULTS = {
    "atr_pct_high": 4.0,        # above this, an LULD trip is an ordinary day
    "atr_pct_extreme": 8.0,
    "price_low": 5.0,
    "price_very_low": 2.0,
    "dollar_volume_thin": 20_000_000,
    "dollar_volume_very_thin": 5_000_000,
    "earnings_today_points": 3,
    "refuse_at_score": 5,
}


def halt_risk_score(symbol, atr_pct=None, price=None, dollar_volume=None,
                    earnings_today=False, cfg=None):
    """
    (score, reasons). Higher is more halt-prone. 0 means nothing flagged.

    Missing inputs contribute NOTHING, which means an unmeasured symbol scores
    0 and is allowed. State that plainly rather than dressing it up: an
    unmeasured symbol is not a calm one, and this scores it as if it were.

    That is the deliberate choice, for the same reason every other guard here
    fails open - refusing on absent data turns one flaky screener call into a
    blank trading day. It is only defensible because the screener has ALREADY
    applied min_avg_volume and universe_min_dollar_volume upstream, so a symbol
    reaching this function has passed a liquidity floor. If this is ever called
    from somewhere without that guarantee, revisit the choice there rather than
    changing it here.
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    score, reasons = 0, []

    try:
        if atr_pct is not None:
            atr_pct = float(atr_pct)
            if atr_pct >= c["atr_pct_extreme"]:
                score += 4
                reasons.append(f"ATR {atr_pct:.1f}% is extreme")
            elif atr_pct >= c["atr_pct_high"]:
                score += 2
                reasons.append(f"ATR {atr_pct:.1f}% is high")

        if price is not None:
            price = float(price)
            if price <= c["price_very_low"]:
                score += 3
                reasons.append(f"${price:.2f} is in the widest LULD band")
            elif price <= c["price_low"]:
                score += 1
                reasons.append(f"${price:.2f} is low")

        if dollar_volume is not None:
            dv = float(dollar_volume)
            if dv <= c["dollar_volume_very_thin"]:
                score += 3
                reasons.append(f"${dv / 1e6:.1f}M/day is very thin")
            elif dv <= c["dollar_volume_thin"]:
                score += 1
                reasons.append(f"${dv / 1e6:.1f}M/day is thin")

        if earnings_today:
            score += int(c["earnings_today_points"])
            reasons.append("reports earnings today (a SCHEDULED binary event - "
                           "the only genuinely predictable item here)")
    except (TypeError, ValueError) as e:
        logger.debug(f"halt risk scoring failed for {symbol}: {e}")
        return 0, []

    return score, reasons


def is_halt_prone(symbol, cfg=None, **kw):
    """(refuse: bool, why: str|None) against the configured threshold."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    score, reasons = halt_risk_score(symbol, cfg=cfg, **kw)
    if score >= int(c["refuse_at_score"]):
        return True, (f"halt-prone (score {score}/{c['refuse_at_score']}): "
                      + "; ".join(reasons))
    return False, None


def filter_symbols(rows, cfg=None):
    """
    (kept, dropped) from [{symbol, atr_pct, price, dollar_volume,
    earnings_today}, ...].

    NEVER empties the list. The same rule every other filter here follows: a
    watchlist of nothing guarantees a blank day, which is a worse outcome than
    watching something imperfect.
    """
    kept, dropped = [], []
    for r in rows or []:
        sym = r.get("symbol")
        refuse, why = is_halt_prone(
            sym, cfg=cfg,
            atr_pct=r.get("atr_pct"), price=r.get("price"),
            dollar_volume=r.get("dollar_volume"),
            earnings_today=bool(r.get("earnings_today")),
        )
        (dropped if refuse else kept).append((sym, why) if refuse else sym)
    if not kept:
        logger.warning(
            "halt-risk filter would have dropped EVERY symbol - keeping the "
            "list intact instead. Watching nothing guarantees a blank day."
        )
        return [r.get("symbol") for r in rows or []], []
    return kept, dropped
