"""
Build the day's candidate pool from the whole market instead of a hand-written
list.

The problem
-----------
`stock_universe` was 50 names typed once, `candidates.txt` another 50, and the
screener picked 15. So the same tickers came back day after day - on 2026-08-26
every symbol traded except ADBE and CMG was in that hardcoded list. The screener
was not finding the best stocks, it was ranking a list someone wrote.

Why this is affordable
----------------------
`get_historical_bars` already accepts a LIST and passes it to Alpaca's
multi-symbol endpoint; the screener simply never used it, looping one symbol at
a time at ~6 calls each. Batched, a day of bars for a thousand symbols is a
handful of requests rather than a thousand.

The funnel
----------
Two stages, because the cheap and expensive features have very different costs:

    ~11,000 tradable assets      one request
      -> liquidity + price cut   one batched daily-bar sweep
      -> ~1,000 candidates       cheap scoring, no further requests
      -> top ~100                expensive per-symbol scoring (minute bars)
      -> top 15                  traded

Everything in stage one comes out of bars already fetched, so widening from 50
to 1,000 candidates costs requests proportional to 1,000/chunk_size, not to
1,000. Stage two is the per-symbol work that could not be batched - opening-move
history needs minute bars - and it only ever runs on the survivors.

What this does NOT change
-------------------------
How many positions get opened. The stream budget is ~28 subscriptions and
num_stocks_to_trade is 15; screening a thousand names changes WHICH 15, not how
many. That is the intended effect - on 2026-08-26, 12 of 22 positions went the
wrong way immediately - but it is worth being explicit that this is a selection
change, not a volume one.

Failure behaviour
-----------------
Every entry point returns empty rather than raising, and the caller falls back
to the static list. An unfiltered watchlist beats an empty one, and a screener
that cannot reach the network must not take the session down with it.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Alpaca accepts large symbol lists, but a request that is too large fails as a
# whole and takes its entire chunk with it. Chunking keeps one bad response from
# costing the universe, and lets a partial failure degrade instead of aborting.
DEFAULT_CHUNK = 200

# Share classes and structures this strategy cannot or should not trade. Checked
# against the asset NAME because Alpaca does not expose a security-type field
# granular enough to separate them.
NAME_EXCLUSIONS = (
    " warrant", " warrants", " right", " rights", " unit", " units",
    "preferred", "depositary", "acquisition corp",
)


def _is_ordinary_equity(asset):
    """
    Filter out the things that are technically tradable equities but are not
    what this strategy means by a stock.

    Warrants, rights and SPAC units trade in cents on thin books, gap on news
    that has nothing to do with the underlying, and quote spreads that make the
    -0.5% first exit meaningless. Preferreds barely move at all. None of them
    belong in a momentum screen, and each one costs a screening slot.
    """
    name = (asset.get("name") or "").lower()
    if any(x in name for x in NAME_EXCLUSIONS):
        return False
    symbol = asset.get("symbol") or ""
    # Class shares and warrants carry suffixes after a dot or dash: BRK.B, XYZ.WS
    if "." in symbol or "-" in symbol:
        return False
    if not symbol.isalpha():
        return False
    return True


def fetch_tradable_symbols(broker, config=None):
    """Every ordinary tradable US equity symbol. [] on any failure."""
    try:
        assets = broker.get_all_assets(tradable_only=True)
    except Exception as e:
        logger.error(f"universe: could not list assets ({e})")
        return []

    out = [a["symbol"] for a in assets if _is_ordinary_equity(a)]
    logger.info(
        f"universe: {len(out)} ordinary tradable equities "
        f"(from {len(assets)} tradable assets)"
    )
    return sorted(set(out))


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def daily_snapshot(broker, symbols, config=None, lookback_days=30, chunk=None):
    """
    One batched sweep of daily bars -> per-symbol statistics.

    Returns {symbol: {price, dollar_volume, avg_dollar_volume, return_5d,
                      volume_ratio, atr_pct, sessions}}

    Every field here is derived from the SAME fetch. That is the point: these
    are the features that used to cost a separate request each, and they now
    cost nothing beyond the sweep that the liquidity cut needs anyway.
    """
    cfg = (config or {}).get("trading", config or {}) or {}
    chunk = chunk or cfg.get("universe_chunk_size", DEFAULT_CHUNK)
    end = datetime.now()
    start = end - timedelta(days=lookback_days)

    stats = {}
    failed_chunks = 0
    for group in _chunks(list(symbols), chunk):
        try:
            bars = broker.get_historical_bars(group, start, end, "1Day")
        except Exception as e:
            failed_chunks += 1
            logger.warning(f"universe: bar chunk of {len(group)} failed ({e})")
            continue
        if not bars:
            failed_chunks += 1
            continue
        for symbol, rows in (bars or {}).items():
            s = _stats_from_bars(rows)
            if s:
                stats[symbol] = s

    # An empty result with no failures means the bars arrived and PARSING
    # rejected them, which is a different fault from a network problem and needs
    # to say so - that distinction is what took a day to find on 2026-08-27.
    if not stats and not failed_chunks and symbols:
        logger.error(
            "universe: every chunk returned data but not one symbol produced "
            "usable statistics. That is a PARSING failure, not a fetch failure - "
            "check the shape get_historical_bars returns against _series()."
        )
    if failed_chunks:
        logger.warning(
            f"universe: {failed_chunks} chunk(s) returned nothing - the snapshot "
            f"covers {len(stats)} symbols and is incomplete"
        )
    logger.info(f"universe: daily stats for {len(stats)} symbols")
    return stats


def _series(rows, name):
    """
    One column out of whatever get_historical_bars returned.

    It returns {symbol: pandas.DataFrame}, and iterating a DataFrame yields
    COLUMN NAMES, not rows. The first version of this function looped over
    `rows` expecting dicts or objects, so every symbol produced an empty list
    and was discarded - silently, with no exception, because a string simply
    has no `.close`. On 2026-08-27 that turned 11,413 candidates into "daily
    stats for 0 symbols" and the dynamic universe fell back to the static pool
    without a single error line naming the cause.

    Lists of dicts and lists of objects are still accepted so this works against
    a fixture as well as the live broker.
    """
    cols = getattr(rows, "columns", None)
    if cols is not None:                      # pandas DataFrame
        if name not in cols:
            return []
        return [float(x) for x in rows[name].tolist() if x is not None]
    out = []
    for b in rows or []:
        v = b.get(name) if isinstance(b, dict) else getattr(b, name, None)
        if v is not None:
            out.append(float(v))
    return out


def _stats_from_bars(rows):
    """Per-symbol statistics from one symbol's daily bars, or None."""
    try:
        closes = _series(rows, "close")
        volumes = _series(rows, "volume")
        highs = _series(rows, "high") or list(closes)
        lows = _series(rows, "low") or list(closes)
        n = min(len(closes), len(volumes))
        closes, volumes = closes[:n], volumes[:n]
        highs, lows = (highs + closes)[:n], (lows + closes)[:n]
        if len(closes) < 5:
            return None

        price = closes[-1]
        if price <= 0:
            return None

        # Recent dollar volume, not share volume: 10M shares of a $2 stock and
        # 10M of a $200 one are not the same market, and only one of them can
        # absorb a position without moving.
        recent_dv = [c * v for c, v in zip(closes[-5:], volumes[-5:])]
        dollar_volume = sum(recent_dv) / len(recent_dv)
        all_dv = [c * v for c, v in zip(closes, volumes)]
        avg_dollar_volume = sum(all_dv) / len(all_dv)

        base = closes[-6] if len(closes) >= 6 else closes[0]
        return_5d = (price - base) / base * 100 if base else 0.0

        older = volumes[:-5] or volumes
        avg_older = sum(older) / len(older)
        recent_vol = sum(volumes[-5:]) / 5
        volume_ratio = (recent_vol / avg_older) if avg_older else 1.0

        # True range is overkill on daily bars for a ranking cut; the high-low
        # range as a share of price is the same idea at no extra cost.
        ranges = [(h - l) / c * 100 for h, l, c in
                  zip(highs[-20:], lows[-20:], closes[-20:]) if c]
        atr_pct = sum(ranges) / len(ranges) if ranges else 0.0

        return {
            "price": price,
            "dollar_volume": dollar_volume,
            "avg_dollar_volume": avg_dollar_volume,
            "return_5d": return_5d,
            "volume_ratio": volume_ratio,
            "atr_pct": atr_pct,
            "sessions": len(closes),
        }
    except Exception:
        return None


def liquidity_cut(stats, config=None):
    """
    Price band + minimum dollar volume, then the top N by dollar volume.

    The price band is the same one the screener already enforces, applied HERE
    so an unbuyable symbol never occupies a scoring slot, a stream subscription
    or a poll. On 2026-08-24 AMC signalled ten times at ~$2.70 and was refused
    ten times, and each refusal still counted toward the burst width that
    throttles genuine entries.
    """
    cfg = (config or {}).get("trading", config or {}) or {}
    min_price = cfg.get("min_stock_price") or 0
    max_price = cfg.get("max_stock_price") or 0
    min_dv = cfg.get("universe_min_dollar_volume", 20_000_000)
    top_n = cfg.get("universe_size", 1000)

    kept = []
    for symbol, s in stats.items():
        px = s.get("price") or 0
        if min_price and px < min_price:
            continue
        if max_price and px > max_price:
            continue
        if s.get("dollar_volume", 0) < min_dv:
            continue
        kept.append((symbol, s))

    kept.sort(key=lambda kv: kv[1]["dollar_volume"], reverse=True)
    out = [symbol for symbol, _ in kept[:top_n]]
    logger.info(
        f"universe: {len(out)} symbols after the liquidity cut "
        f"(${min_dv/1e6:.0f}M+ daily, ${min_price}-${max_price}, top {top_n})"
    )
    return out


def cheap_score(s):
    """
    0-100 from the daily snapshot alone. No further requests.

    Deliberately a coarse ranking, not a prediction. Its only job is to decide
    which ~100 of ~1,000 symbols are worth the expensive per-symbol scoring, so
    being roughly right about the top decile is the whole requirement. The real
    scoring still happens in StockScreener.score_stock.

    Mirrors the weights score_stock uses for the features they share, so this
    stage does not rank on a different idea of what "good" means than the stage
    it feeds.
    """
    if not s:
        return 0.0
    score = 0.0

    # Volatility: the strategy needs a move to exist at all.
    score += min(35.0, (s.get("atr_pct") or 0) * 7)

    # Volume surge relative to the symbol's own norm.
    vr = s.get("volume_ratio") or 0
    if vr > 1.8:
        score += 25
    elif vr > 1.5:
        score += 18
    elif vr > 1.2:
        score += 12
    elif vr > 1.0:
        score += 6

    # Recent momentum, penalised when negative - same shape as score_stock.
    m = s.get("return_5d") or 0
    if m > 5:
        score += 25
    elif m > 2:
        score += 16
    elif m > 0:
        score += 10
    else:
        score += max(0.0, 10 + m * 2)

    # Liquidity, mildly: enough to trade is what matters, more is not better.
    dv = s.get("dollar_volume") or 0
    score += min(15.0, dv / 1e8 * 15)

    return round(min(100.0, score), 2)


def rank_candidates(stats, symbols, config=None):
    """[(symbol, cheap_score)] best first, for the given symbols."""
    scored = [(sym, cheap_score(stats.get(sym))) for sym in symbols]
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored


# --- caching ---------------------------------------------------------------
#
# The liquidity cut answers "what is liquid enough to trade", which changes over
# weeks, not overnight. Rebuilding it daily would spend the pre-open window on a
# question whose answer is nearly always yesterday's. The cheap SCORES are not
# cached - those are today's tape and are recomputed every session.

def _cache_age_days(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return None


def load_cached_universe(path, max_age_days):
    age = _cache_age_days(path)
    if age is None:
        return None
    if max_age_days and age > max_age_days:
        logger.info(f"universe: cache is {age:.1f} days old, rebuilding")
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        symbols = data.get("symbols") or []
        if not symbols:
            return None
        logger.info(f"universe: reusing cached list of {len(symbols)} symbols "
                    f"({age:.1f} days old, built {data.get('built')})")
        return symbols
    except Exception as e:
        logger.warning(f"universe: unreadable cache at {path} ({e})")
        return None


def save_cached_universe(path, symbols):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"built": datetime.now().isoformat(timespec="seconds"),
                       "count": len(symbols), "symbols": symbols}, f)
    except Exception as e:
        logger.warning(f"universe: could not write cache to {path} ({e})")


def build_universe(broker, config, force_rebuild=False):
    """
    The liquid tradable universe, from cache when it is fresh enough.

    Returns [] on any failure, which the caller must treat as "fall back to the
    static list" rather than "trade nothing".
    """
    cfg = (config or {}).get("trading", config or {}) or {}
    path = cfg.get("universe_cache_file", "logs/universe.json")
    max_age = cfg.get("universe_cache_days", 7)

    if not force_rebuild:
        cached = load_cached_universe(path, max_age)
        if cached:
            return cached

    symbols = fetch_tradable_symbols(broker, config)
    if not symbols:
        return []

    stats = daily_snapshot(broker, symbols, config)
    if not stats:
        logger.error("universe: no daily bars came back - keeping the static list")
        return []

    liquid = liquidity_cut(stats, config)
    if liquid:
        save_cached_universe(path, liquid)
    return liquid


def select_candidates(broker, config, static_pool=(), force_rebuild=False):
    """
    Today's candidate pool for the expensive screener.

    Returns (symbols, info). `symbols` is the top `universe_shortlist_size` by
    cheap score, with `static_pool` always folded in - the hand-written names
    are not privileged, but dropping them silently would change two things at
    once and make the first session's results unreadable.
    """
    cfg = (config or {}).get("trading", config or {}) or {}
    info = {"source": "static", "universe": 0, "shortlist": 0}

    if not cfg.get("use_dynamic_universe", False):
        return list(static_pool), info

    universe = build_universe(broker, config, force_rebuild=force_rebuild)
    if not universe:
        logger.warning("universe: build failed - falling back to the static pool")
        return list(static_pool), info

    # Today's tape for the universe. Separate from the cached membership list:
    # what is liquid changes over weeks, what is moving changes overnight.
    stats = daily_snapshot(broker, universe, config)
    if not stats:
        logger.warning("universe: no stats today - falling back to the static pool")
        return list(static_pool), info

    shortlist_n = cfg.get("universe_shortlist_size", 100)
    ranked = rank_candidates(stats, universe, config)
    shortlist = [s for s, _ in ranked[:shortlist_n]]

    merged = list(dict.fromkeys(shortlist + [s.upper() for s in static_pool]))
    info.update({"source": "dynamic", "universe": len(universe),
                 "shortlist": len(shortlist), "candidates": len(merged),
                 "top": ranked[:10]})
    logger.info(
        f"universe: {len(universe)} liquid -> top {len(shortlist)} by cheap score "
        f"-> {len(merged)} candidates with the static pool folded in"
    )
    if ranked[:5]:
        logger.info("universe: best cheap scores - " +
                    ", ".join(f"{s}={v:.0f}" for s, v in ranked[:5]))
    return merged, info
