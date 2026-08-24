"""
Pre-open watchlist augmentation: an earnings list and a QQQ-constituent list,
each narrowed to its best N names and merged into the day's watchlist.

Runs in the buffer between the screener (09:05 ET) and the open, not at 09:05,
because both inputs are only meaningful once pre-market is properly awake:
earnings reactions need pre-market prints to have accumulated, and the QQQ
trend read is a read of TODAY's tape, not yesterday's.

Everything here is additive and fails soft. If the earnings endpoint is down,
the constituent file is missing, or QQQ can't be priced, the day's watchlist is
exactly what the screener and stock_universe produced - never fewer symbols
than before, never an exception that reaches the trading loop.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
QQQ_CONSTITUENTS_FILE = os.path.join(_HERE, "qqq_constituents.txt")

# Nasdaq's public earnings calendar. No API key, but it refuses a default
# python-requests User-Agent, hence the browser string.
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------
# Earnings list
# --------------------------------------------------------------------------

def _fetch_nasdaq_earnings(date_str: str, timeout: int) -> List[dict]:
    """One day of the Nasdaq earnings calendar. Returns [] on any failure."""
    import requests

    url = NASDAQ_EARNINGS_URL.format(date=date_str)
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout)
    except Exception as e:
        logger.warning(f"Earnings calendar unreachable for {date_str}: {type(e).__name__}: {e}")
        return []

    if resp.status_code != 200:
        logger.warning(f"Earnings calendar returned HTTP {resp.status_code} for {date_str}")
        return []

    try:
        payload = resp.json()
    except Exception as e:
        logger.warning(f"Earnings calendar returned non-JSON for {date_str}: {e}")
        return []

    # {"data": {"rows": [...]}} on a normal day; "data" is null on a day with
    # no reporters, which is a valid answer rather than an error.
    data = payload.get("data") or {}
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return []
    return rows


def _earnings_surprise(row: dict):
    """
    Percentage EPS surprise for a row that has already reported, or None.

    Nasdaq exposes `surprise` directly on reported rows, and `eps` /
    `epsForecast` as fallbacks. Values arrive as strings, sometimes with $ or
    parentheses for negatives, and are simply absent before a company reports -
    absent is NOT a miss, so it returns None and the caller decides.
    """
    def num(v):
        if v is None:
            return None
        t = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
        if not t or t in ("N/A", "--", "-"):
            return None
        neg = t.startswith("(") and t.endswith(")")
        if neg:
            t = t[1:-1]
        try:
            return -float(t) if neg else float(t)
        except ValueError:
            return None

    direct = num(row.get("surprise"))
    if direct is not None:
        return direct

    actual, forecast = num(row.get("eps")), num(row.get("epsForecast"))
    if actual is None or forecast is None or forecast == 0:
        return None
    return (actual - forecast) / abs(forecast) * 100


def _report_timing(row: dict) -> str:
    """
    Normalise Nasdaq's timing field to 'bmo' / 'amc' / 'unknown'.

    The distinction is the whole point of this list. A company reporting AFTER
    today's close has no catalyst at 09:35 - the news doesn't exist yet. The
    names that actually gap into the open are those that reported BEFORE today's
    bell, or after YESTERDAY's.
    """
    raw = str(row.get("time") or "").strip().lower()
    if "pre-market" in raw or raw in ("bmo", "before market open"):
        return "bmo"
    if "after-hours" in raw or raw in ("amc", "after market close"):
        return "amc"
    return "unknown"


def fetch_earnings_symbols(now_et: datetime, config: dict) -> Tuple[List[str], Dict[str, str]]:
    """
    Symbols whose earnings are a live catalyst for TODAY's open:
    today's before-the-bell reporters plus yesterday's after-the-bell ones.

    Returns (symbols, {symbol: label}, {symbol: eps_surprise_pct or None}).
    """
    trading = config["trading"]
    timeout = trading.get("earnings_calendar_timeout_seconds", 20)

    today = now_et.date()
    # Friday's open reacts to Thursday's AMC prints; on a Monday, to Friday's.
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)

    symbols, why, surprises = [], {}, {}

    for date, wanted, label in (
        (today, "bmo", "today BMO"),
        (prev, "amc", "prev AMC"),
    ):
        rows = _fetch_nasdaq_earnings(date.strftime("%Y-%m-%d"), timeout)
        kept = 0
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym or not sym.isalpha():   # drop warrants/units/blanks
                continue
            if _report_timing(row) != wanted:
                continue
            if sym not in why:
                surprise = _earnings_surprise(row)
                symbols.append(sym)
                why[sym] = label
                surprises[sym] = surprise
                kept += 1
        logger.info(f"Earnings calendar {date} ({wanted.upper()}): {len(rows)} rows -> {kept} usable symbols")

    if not symbols:
        logger.warning(
            "Earnings list is empty - either no qualifying reporters, or the "
            "calendar fetch failed. Watchlist is unaffected."
        )
    return symbols, why, surprises


# --------------------------------------------------------------------------
# QQQ constituents + trend
# --------------------------------------------------------------------------

def load_qqq_constituents(path: str = QQQ_CONSTITUENTS_FILE) -> List[str]:
    """Bundled Nasdaq-100 tracking list. Returns [] if the file is missing."""
    try:
        with open(path) as f:
            names = [
                line.strip().upper()
                for line in f
                if line.strip() and not line.startswith("#")
            ]
        return list(dict.fromkeys(names))
    except FileNotFoundError:
        logger.warning(f"QQQ constituent file not found at {path} - QQQ list disabled for today")
        return []
    except Exception as e:
        logger.warning(f"Could not read QQQ constituent file: {e}")
        return []


def qqq_trend(screener, config) -> Tuple[bool, Dict]:
    """
    Is QQQ trending up into today's open?

    Three independent reads, each a plain yes/no, and the verdict is a majority.
    A single measure is too easy to fool: a green gap on a downtrending tape,
    or a strong 5-day run that has already rolled over this morning.

      1. Gap        - current pre-market price vs yesterday's close
      2. Momentum   - N-day return (qqq_trend_lookback_days)
      3. Structure  - current price above its own N-day average

    Returns (is_up, details). Falls back to is_up=False on missing data, which
    is the conservative direction: the QQQ list simply isn't added.
    """
    trading = config["trading"]
    lookback = trading.get("qqq_trend_lookback_days", 5)
    details = {"gap_pct": None, "return_pct": None, "above_avg": None, "votes": 0}

    try:
        gap = screener._get_recent_gap("QQQ")
        ret = screener._get_5day_return("QQQ")
        price = screener._get_current_price("QQQ") or screener._get_price("QQQ")

        end = datetime.now(screener.et).date()
        bars = screener.broker.get_historical_bars("QQQ", end - timedelta(days=lookback * 3), end, "1Day")
        avg = None
        if "QQQ" in bars and not bars["QQQ"].empty:
            closes = bars["QQQ"].sort_values("timestamp")["close"].tail(lookback)
            if len(closes) > 0:
                avg = float(closes.mean())

        details["gap_pct"] = gap
        details["return_pct"] = ret
        details["above_avg"] = (price > avg) if (avg and price) else None

        votes = 0
        if gap is not None and gap > 0:
            votes += 1
        if ret is not None and ret > 0:
            votes += 1
        if details["above_avg"]:
            votes += 1
        details["votes"] = votes

        is_up = votes >= 2
        logger.info(
            f"QQQ trend: gap {gap:+.2f}%, {lookback}d return {ret:+.2f}%, "
            f"price {price:.2f} vs {lookback}d avg "
            f"{avg:.2f} -> {votes}/3 up-votes -> "
            f"{'TRENDING UP' if is_up else 'not trending up'}"
            if avg and price else
            f"QQQ trend: partial data, {votes}/3 up-votes -> {'TRENDING UP' if is_up else 'not trending up'}"
        )
        return is_up, details

    except Exception as e:
        logger.warning(f"QQQ trend check failed ({type(e).__name__}: {e}) - treating as not trending up")
        return False, details


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

def open_burst_score(screener, symbol, config) -> Tuple[float, Dict]:
    """
    Rank a symbol by how likely it is to make a fast UPWARD move in the first
    ten minutes. Deliberately not called a probability: it is an ordering
    score, and nothing here has been calibrated against outcomes yet. The
    signal journal is what will eventually turn this into a real probability -
    until it has weeks of data, claiming one would be false precision.

    Four inputs, weighted by how well each is actually evidenced:

      Movability   35pts  ATR-based volatility percentile. Nearest thing to a
                          prerequisite: a stock that doesn't travel 0.3% in
                          three minutes can never trigger the entry signal at
                          all, however good the story is.
      RVOL         30pts  Today's volume vs its own norm. The best available
                          proxy for "the crowd is here today" - and it only
                          became measurable on 2026-08-20, when the end=today
                          bug that pinned it at exactly 1.00x was fixed.
      Gap          20pts  Capped, and deliberately NOT linear. On 2026-08-19
                          MRVL had the largest gap of the day (11.2%) and was
                          the second-worst symbol (-$328); signals >=1.0%
                          produced 1 winner in 6 and lost $531. A gap says a
                          catalyst exists, which is why it scores at all - but
                          a big one says the move has already happened, so the
                          curve peaks in the 1-3% band and decays above it.
      Trend        15pts  5-day return. Weakest of the four; continuation is a
                          real effect but a slow one relative to ten minutes.

    Returns (score, details).
    """
    details = {"symbol": symbol}
    score = 0.0
    try:
        vol_pct = screener._get_volatility_percentile(symbol)
        movability = (vol_pct or 0) * 0.35
        score += movability
        details["volatility_percentile"] = vol_pct
        details["movability_score"] = movability

        rvol = screener._get_volume_ratio(symbol) or 0
        if rvol >= 2.5:
            rvol_score = 30
        elif rvol >= 1.8:
            rvol_score = 24
        elif rvol >= 1.4:
            rvol_score = 16
        elif rvol >= 1.1:
            rvol_score = 8
        else:
            rvol_score = 0
        score += rvol_score
        details["rvol"] = rvol
        details["rvol_score"] = rvol_score

        gap = screener._get_recent_gap(symbol) or 0
        gap_abs = abs(gap)
        if gap_abs < 0.5:
            gap_score = gap_abs * 12          # 0 -> 6pts: catalyst barely visible
        elif gap_abs <= 3.0:
            gap_score = 20                    # the band that has room left to run
        elif gap_abs <= 6.0:
            gap_score = 20 - (gap_abs - 3.0) * 4   # decaying: move largely made
        else:
            gap_score = 4                     # already gone; -1% stop sits in the pullback
        score += gap_score
        details["gap_pct"] = gap
        details["gap_score"] = gap_score

        ret5 = screener._get_5day_return(symbol) or 0
        if ret5 > 5:
            trend_score = 15
        elif ret5 > 0:
            trend_score = 8
        else:
            trend_score = max(0, 8 + ret5)
        score += trend_score
        details["return_5d_pct"] = ret5
        details["trend_score"] = trend_score

        price = screener._get_current_price(symbol) or screener._get_price(symbol)
        details["price"] = price

        details["score"] = score
        return score, details

    except Exception as e:
        logger.debug(f"open_burst_score failed for {symbol}: {e}")
        details["score"] = 0
        return 0.0, details


def rank_top_n(screener, symbols, config, top_n, label, exclude=()) -> List[str]:
    """Score `symbols` and return the best `top_n`, logging the full ranking."""
    exclude = set(exclude)
    pool = [s for s in dict.fromkeys(symbols) if s not in exclude]
    if not pool:
        logger.info(f"{label}: nothing to rank (all {len(set(symbols))} already watched)")
        return []

    min_price = config["trading"].get("min_stock_price", 0)
    max_price = config["trading"].get("max_stock_price", 0)

    scored = []
    for sym in pool:
        score, det = open_burst_score(screener, sym, config)
        price = det.get("price") or 0
        # Apply the same price gates the entry path applies, so the list can't
        # spend its ten slots on names that would be rejected at entry anyway.
        if min_price and price and price < min_price:
            continue
        if max_price and price and price > max_price:
            continue
        scored.append((score, sym, det))

    scored.sort(key=lambda x: x[0], reverse=True)
    picked = scored[:top_n]

    logger.info(f"----- {label}: top {len(picked)} of {len(pool)} candidates -----")
    for score, sym, det in picked:
        logger.info(
            f"{sym:6} | Burst score: {score:5.1f} | "
            f"Movability: {det.get('movability_score', 0):4.1f} | "
            f"RVOL: {det.get('rvol', 0):4.2f}x | "
            f"Gap: {det.get('gap_pct', 0):+5.2f}% | "
            f"5d: {det.get('return_5d_pct', 0):+5.1f}% | "
            f"${det.get('price', 0):7.2f}"
        )
    return [sym for _, sym, _ in picked]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _filter_earnings_candidates(screener, symbols, why, surprises, config):
    """
    Keep only earnings names worth trading at the open: those that BEAT, and
    that have NOT already made the move.

    The logic behind both halves. A beat is the catalyst - a miss gaps a stock
    DOWN, and this strategy is long-only, so a missing company is not a
    candidate at any price. And a name already up 6% pre-market has spent the
    catalyst: on 2026-08-21 BKE gapped +6.68% and BEKE +5.21%, and those two
    plus BJ cost -$144.30 on a -$30.78 day. The move to trade is the one that
    has not happened yet.

    Names whose surprise cannot be read are KEPT, not dropped. Nasdaq often
    has not published EPS for a before-the-bell reporter by 09:20, and
    discarding every one of them would empty the list on most mornings. An
    unknown is an unknown, not a miss.
    """
    trading = config["trading"]
    require_beat = trading.get("earnings_require_beat", True)
    max_gap = trading.get("earnings_max_gap_pct", 3.0)

    require_known = trading.get("earnings_require_known_beat", False)

    kept = []
    for sym in symbols:
        surprise = surprises.get(sym)

        if require_beat and surprise is not None and surprise <= 0:
            logger.info(f"  - {sym} skipped: MISSED earnings ({surprise:+.1f}%)")
            continue

        if require_known and surprise is None:
            # Nasdaq often has not published EPS for a before-the-bell reporter
            # by 09:20, so "unknown" usually means "too early", not "bad". This
            # setting trades coverage for certainty: on 2026-08-24 the only
            # earnings name added was PDD, logged "surprise unknown", and it was
            # the worst trade of the day at -$86.56 on a -$119.38 session.
            #
            # The cost is real - some mornings this will empty the list - which
            # is why it is a separate flag rather than folded into
            # earnings_require_beat.
            logger.info(
                f"  - {sym} skipped: earnings surprise not published yet "
                f"(earnings_require_known_beat is on)"
            )
            continue

        gap = 0.0
        try:
            gap = screener._get_recent_gap(sym) or 0.0
        except Exception as e:
            logger.debug(f"Could not read gap for {sym}: {e}")

        if max_gap and gap > max_gap:
            logger.info(
                f"  - {sym} skipped: already gapped +{gap:.2f}% "
                f"(over earnings_max_gap_pct {max_gap}%) - the move is made"
            )
            continue

        kept.append(sym)

    logger.info(
        f"Earnings filter: {len(symbols)} reported -> {len(kept)} tradeable "
        f"(beat required: {require_beat}, known beat required: {require_known}, "
        f"max gap {max_gap}%)"
    )
    return kept


def augment_symbols(config, screener, existing, now_et=None) -> Tuple[List[str], List[str]]:
    """
    Extend the day's watchlist with the earnings and QQQ lists.

    Returns (full_list, added) where full_list starts with `existing`, in order,
    so nothing already selected can be displaced. Never raises.
    """
    trading = config["trading"]
    existing = list(dict.fromkeys(existing))
    added: List[str] = []

    if screener is None:
        logger.warning("No screener available - skipping list augmentation")
        return existing, added

    now_et = now_et or datetime.now(screener.et)

    # --- earnings ---------------------------------------------------------
    if trading.get("use_earnings_list", False):
        try:
            cands, why, surprises = fetch_earnings_symbols(now_et, config)
            if cands:
                logger.info(f"Earnings candidates before filtering: {len(cands)}")
                cands = _filter_earnings_candidates(
                    screener, cands, why, surprises, config
                )
                picks = rank_top_n(
                    screener, cands, config,
                    trading.get("earnings_list_top_n", 3),
                    "EARNINGS LIST",
                    exclude=set(existing) | set(added),
                )
                for p in picks:
                    sp = surprises.get(p)
                    logger.info(
                        f"  + {p} (earnings: {why.get(p, 'n/a')}"
                        + (f", beat by {sp:+.1f}%" if sp is not None else ", surprise unknown")
                        + ")"
                    )
                added.extend(picks)
        except Exception as e:
            logger.error(f"Earnings list failed, continuing without it: {e}", exc_info=True)

    # --- QQQ --------------------------------------------------------------
    if trading.get("use_qqq_list", False):
        try:
            is_up, _ = qqq_trend(screener, config)
            if is_up:
                constituents = load_qqq_constituents()
                if constituents:
                    picks = rank_top_n(
                        screener, constituents, config,
                        trading.get("qqq_list_top_n", 10),
                        "QQQ LIST",
                        exclude=set(existing) | set(added),
                    )
                    added.extend(picks)
            else:
                logger.info(
                    "QQQ is not trending up - skipping the QQQ constituent list. "
                    "Buying index-correlated large caps into a flat or falling "
                    "index is 10 versions of the same bet."
                )
        except Exception as e:
            logger.error(f"QQQ list failed, continuing without it: {e}", exc_info=True)

    full = list(dict.fromkeys(existing + added))
    logger.info(
        f"List augmentation: {len(existing)} watched -> {len(full)} "
        f"(+{len(added)}: {', '.join(added) if added else 'none'})"
    )
    return full, added
