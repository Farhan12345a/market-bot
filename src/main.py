#!/usr/bin/env python3
"""
Market opening trading bot - Paper trading version

Watches screened candidates for a rapid price rise during the entry window,
then manages exits with trailing stop + scale-out logic for the rest of the day.

Setup:
  1. export APCA_API_KEY_ID="your_key"
  2. export APCA_API_SECRET_KEY="your_secret"
  3. python main.py
"""

import os
import sys
import yaml
import logging
import time
import csv
import copy
import math
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
import pytz

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.broker.alpaca_broker import AlpacaBroker
from src.strategy.strategy import Strategy, TradeManager
from src.data.market_data import MarketDataManager
from src.data.stream import PriceStream
from src.executor.executor import Executor, PHANTOM_EXIT
from src.screener.stock_screener import StockScreener
from src.screener.list_builder import augment_symbols
from src.notifications.email_notifier import EmailNotifier
from src.analytics.signal_journal import SignalJournal
from src.analytics import trade_recorder as TR

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/trading.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

def load_config(config_file="config.yaml"):
    """Load config from YAML"""
    with open(config_file, "r") as f:
        return yaml.safe_load(f)

def parse_hhmm_today(hhmm_str, et_tz):
    """Turn a config 'HH:MM' string into today's datetime in the given tz"""
    hour, minute = map(int, hhmm_str.split(":"))
    return datetime.now(et_tz).replace(hour=hour, minute=minute, second=0, microsecond=0)

def _measure_breadth(config, market_data, symbols, state, now, et):
    """
    Measure what the WATCHLIST is doing, every poll. Decides nothing.

    Replaces `_breadth_halt` (2026-08-30 - 2026-09-02). That function did two
    jobs: it measured the watchlist's mean move since the open, and it latched
    a hard NO-NEW-ENTRIES halt if the mean was below -0.3% at 09:40. The HALT
    is gone; the MEASUREMENT is why this still exists.

    Why the halt was retired rather than tuned. It had already been dead code
    for days - the halt flag was AND-ed with `not regime_active`, and with
    regime_sizing enabled, so it could never fire. Worse, keeping it meant two
    guards answering the same question ("is today tradeable?") from different
    evidence, where whichever ran first won by ordering accident rather than by
    rule. regime_sizing supersedes it properly: it scales size continuously
    instead of stopping the day, and at bearish_multiplier 0.0 it can still
    refuse everything when that is the right answer.

    What it measures, and why each number is here:

      mean_move   - the average move since the open. The original signal.
      dispersion  - population standard deviation of those moves. This is the
                    CHOP reading, and it is the number breadth_halt never
                    computed. A mean near zero can mean "nothing is happening"
                    or "everything is happening in both directions at once",
                    and those want opposite responses. Dispersion tells them
                    apart.
      crossings   - how many symbols have flipped from above their opening
                    price to below it (or back) more than twice. Chop's most
                    direct signature: a name that has crossed its own open
                    four times is not trending in either direction.
      falling     - kept for the log line and for the regime fallback.

    Deliberately reads the WATCHLIST, not SPY. On 2026-08-28 SPY was flat
    (-0.018%) while the average signal returned -1.045% at 15 minutes - a
    market-level detector would have called that day fine. The signal was in
    these names, not in the index.

    Cheap enough to run every poll: get_latest_bar reads the stream's in-memory
    cache for streamed symbols.
    """
    moves = []
    sides = state.setdefault("side", {})
    crossings = state.setdefault("crossings", {})
    for symbol in symbols:
        try:
            bar = market_data.get_latest_bar(symbol, "1Min")
            if not bar:
                continue
            price = market_data.get_entry_price(symbol, bar)
            open_px = state.get("open_px", {}).get(symbol)
            if not (price and open_px):
                continue
            moves.append((price - open_px) / open_px * 100)

            # A crossing is a change of SIDE relative to the opening price.
            # Counted per symbol and only on an actual flip, so a name sitting
            # a hair above its open does not accumulate crossings from noise in
            # the last decimal place.
            side = 1 if price > open_px else (-1 if price < open_px else 0)
            if side and sides.get(symbol) and side != sides[symbol]:
                crossings[symbol] = crossings.get(symbol, 0) + 1
            if side:
                sides[symbol] = side
        except Exception:
            continue

    min_n = ((config.get("trading") or {}).get("breadth") or {}).get("min_symbols", 5)
    state["breadth_n"] = len(moves)
    if len(moves) < min_n:
        state["mean_move"] = None
        state["dispersion"] = None
        return

    mean_move = sum(moves) / len(moves)
    var = sum((m - mean_move) ** 2 for m in moves) / len(moves)
    state["mean_move"] = mean_move
    state["dispersion"] = math.sqrt(var)
    state["falling"] = sum(1 for m in moves if m < 0)
    state["choppy_symbols"] = sum(1 for c in crossings.values() if c >= 2)

    if not state.get("logged_once"):
        state["logged_once"] = True
        logger.info(
            f"BREADTH at {now:%H:%M} ET: watchlist mean {mean_move:+.3f}%, "
            f"dispersion {state['dispersion']:.3f}%, {state['falling']}/{len(moves)} "
            f"falling. Measurement only - regime_sizing decides what to do with it."
        )


def _chop_reading(config, breadth_state):
    """
    (is_choppy, detail) from the watchlist's own behaviour, or (False, None).

    CHOP IS NOT WEAKNESS. A falling tape has a negative mean; a choppy one has
    a mean near zero with everything scattered around it. 2026-08-28 is the
    case this is built from: 30 positions, 5 winners, 19 peaked under +0.5% and
    lost $484 together while the 5 that cleared +1% made $424. Payoff actually
    IMPROVED to 2.90x so breakeven was only 26% - the hit rate collapsed
    instead. That is not a selection failure, it is a tape where nothing
    follows through, and the correct response is fewer trades taken for
    smaller targets, not better ones.

    Two conditions, both required, because either alone is ordinary:
      - |mean| below max_abs_mean_pct: the tape is going nowhere NET
      - dispersion at or above min_dispersion_pct: but the names ARE moving

    The crossing count is used as a supporting reading rather than a gate; it
    needs time to accumulate and would make this blind early in the session.
    """
    cfg = ((config.get("trading") or {}).get("regime_sizing") or {}).get("chop") or {}
    if not cfg.get("enabled", True):
        return False, None
    mean = breadth_state.get("mean_move")
    disp = breadth_state.get("dispersion")
    if mean is None or disp is None:
        return False, None
    max_abs_mean = float(cfg.get("max_abs_mean_pct", 0.25))
    min_disp = float(cfg.get("min_dispersion_pct", 0.6))
    if abs(mean) <= max_abs_mean and disp >= min_disp:
        crossers = breadth_state.get("choppy_symbols", 0)
        return True, (
            f"watchlist mean {mean:+.3f}% (flat) but dispersion {disp:.3f}% "
            f"(>= {min_disp}%), {crossers} symbol(s) have crossed their own open "
            f"twice or more"
        )
    return False, None


def spy_history_has(hist):
    """True when a benchmark deque holds at least one usable sample."""
    try:
        return bool(hist) and _last_price(hist) is not None
    except Exception:
        return False


def _last_price(hist):
    """Newest price out of a (timestamp, price) deque, or None."""
    try:
        return hist[-1][1] if hist else None
    except (IndexError, TypeError):
        return None


def _pct_vs(price, level):
    """`price` as a % above/below `level`, or None if either is missing.
    None rather than 0.0 - "no VWAP yet" is not "exactly at VWAP"."""
    try:
        if not price or not level or level <= 0:
            return None
        return round((float(price) - float(level)) / float(level) * 100, 4)
    except (TypeError, ValueError):
        return None


def _vs_vwap(vwap_acc, history, symbol):
    """
    An index's latest price as a % above/below its own session VWAP, or None
    when either side is unavailable.

    Positive means price is above VWAP - the session's average buyer is in
    profit. Returns None rather than 0.0 on missing data: "no VWAP yet" and
    "exactly at VWAP" are different states and only one of them is evidence.
    """
    vwap = _vwap(vwap_acc or {}, symbol)
    last = history[-1][1] if history else None
    if not vwap or not last or vwap <= 0:
        return None
    return (last - vwap) / vwap * 100


def _regime_interval(rc, now, et):
    """Seconds between regime evaluations at this time of day, or None before
    the open (nothing to read yet)."""
    cad = rc.get("cadence") or {}
    try:
        open_at = parse_hhmm_today("09:30", et)
        burst_end = parse_hhmm_today(rc.get("check_time", "09:40"), et)
        settle = parse_hhmm_today(cad.get("settle_time", "10:00"), et)
    except Exception:
        return cad.get("default_seconds", 60)
    if now < open_at:
        return None
    if now < burst_end:
        return cad.get("opening_seconds", 0)      # 0 = every poll
    if now < settle:
        return cad.get("morning_seconds", 60)
    return cad.get("afternoon_seconds", 300)


def _regime_due(rc, state, now, et):
    """True when the regime should be re-evaluated on this poll."""
    interval = _regime_interval(rc, now, et)
    if interval is None:
        return False
    last = state.get("last_eval")
    if last is None or not interval:
        return True
    return (now - last).total_seconds() >= interval


def _regime_multiplier(config, state, breadth_state, spy_history, now, et,
                       vwap_acc=None, qqq_history=None):
    """
    Scale new-entry SIZE by how the tape has behaved since the open, instead
    of breadth_halt's binary stop/don't-stop.

    REPLACES breadth_halt when trading.regime_sizing.enabled is true - see the
    call site in run_trading_day, which runs one or the other, never both.
    breadth_halt's own note names the problem this fixes: "there was just
    nothing to select from" on a losing day, and no amount of better picking
    fixes a tape with no upside in it. A hard halt makes that literally true -
    it refuses every remaining signal regardless of quality. Scaling size
    instead means a neutral or bearish reading still takes fewer, smaller
    positions rather than zero, which is strictly more breadth for the same
    bandaid intent (don't commit full size into a weak tape).

    Uses the SAME evidence breadth_halt does - the watchlist's own mean move
    since the open (breadth_state["mean_move"], populated as a side effect of
    the breadth measurement below) - plus SPY's own move since the open as a
    broad-market trend reading. QQQ is deliberately left out of this first cut:
    it is not currently streamed or benchmarked anywhere in this file (only
    SPY and the day's sector ETFs are - see _benchmark_symbols), and SPY is
    already the market proxy used everywhere else in this module
    (market_burst_spy_pct, excess_vs_spy_pct). Adding a second index is a real
    but separate piece of work, not a blocker to shipping this with one.

    Evaluated once, at check_time, and latched for the rest of the day - same
    reasoning as breadth_halt: a mid-morning bounce is not new evidence about
    the SESSION, it is the noise the check_time delay already exists to filter.

    Returns (multiplier, label). label is None before check_time or when
    there isn't enough evidence yet to read a regime (mirrors breadth_halt's
    min_symbols guard) - both cases return a 1.0 multiplier, i.e. "no opinion
    yet" rather than "penalize for missing data".
    """
    rc = config.get("trading", {}).get("regime_sizing") or {}
    if not rc.get("enabled"):
        return 1.0, None

    # ---- CADENCE ------------------------------------------------------
    #
    # Originally this read ONCE, at check_time, and latched for the day. That
    # is too little and too late in equal measure: on 2026-09-02 the session
    # was over at 09:38 and the 09:45 read never happened, and on any other
    # day a 09:45 reading governs 15:45 as well.
    #
    # Re-read continuously instead, at a cadence matched to how fast the tape
    # actually changes:
    #     09:30-09:33   every poll   - opening conditions turn over fast
    #     09:33-10:00   every 60s    - the window this strategy trades
    #     10:00+        every 300s   - drift, not events
    #
    # Cadence alone would let the regime flip on noise, so it is paired with
    # the hysteresis below. See _regime_interval.
    if not _regime_due(rc, state, now, et):
        return state.get("multiplier", 1.0), state.get("label")

    bullish_mult = rc.get("bullish_multiplier", 1.0)
    neutral_mult = rc.get("neutral_multiplier", 0.5)
    bearish_mult = rc.get("bearish_multiplier", 0.0)

    # ---- PRIMARY RULE: SPY and QQQ against their own session VWAP --------
    #
    # Price above its own volume-weighted average price means the session's
    # buyers are, on average, in profit - a cleaner statement of "is the tape
    # supporting longs" than a move measured from a single opening print,
    # which one gap or one bad tick can dominate. VWAP is also what the
    # institutional flow is actually benchmarked against, so it is a level
    # the market itself reacts to rather than one imposed on it.
    #
    #   both above  -> bullish, full size
    #   one above   -> neutral, half size (the indices disagree; that IS the
    #                  neutral case, not a weak bullish one)
    #   both below  -> bearish. Default 0.0 = NO NEW LONGS, on the reasoning
    #                  that a long scalp with both indices under VWAP is
    #                  fighting the tape, and you do not have to trade every
    #                  day. Set bearish_multiplier to 0.25 to take quarter
    #                  size instead of standing down.
    spy_v = _vs_vwap(vwap_acc, spy_history, "SPY")
    qqq_v = _vs_vwap(vwap_acc, qqq_history, "QQQ")

    if spy_v is not None and qqq_v is not None:
        above = sum(1 for v in (spy_v, qqq_v) if v > 0)
        if above == 2:
            mult, label = bullish_mult, "bullish"
        elif above == 1:
            mult, label = neutral_mult, "neutral"
        else:
            mult, label = bearish_mult, "bearish"
        detail = (f"SPY {spy_v:+.3f}% vs VWAP, QQQ {qqq_v:+.3f}% vs VWAP")
    else:
        # ---- FALLBACK: the pre-VWAP rule ----------------------------------
        # Used only when an index has no VWAP yet - it needs bars carrying
        # volume, and a benchmark that fell back to REST may not have them.
        # Falling back to "no opinion" (1.0x) would be worse: that is full
        # size on no evidence, in the one situation the feature exists for.
        breadth_move = breadth_state.get("mean_move")
        spy_open = (breadth_state.get("open_px") or {}).get("SPY")
        spy_now = spy_history[-1][1] if spy_history else None
        spy_move = ((spy_now - spy_open) / spy_open * 100) if spy_open and spy_now else None

        readings = [m for m in (breadth_move, spy_move) if m is not None]
        if not readings:
            # Same "too thin to conclude anything" guard breadth_halt uses.
            return 1.0, None

        bearish_floor = rc.get("bearish_below_pct", -0.3)
        bullish_floor = rc.get("bullish_above_pct", 0.0)
        if any(m < bearish_floor for m in readings):
            mult, label = bearish_mult, "bearish"
        elif all(m >= bullish_floor for m in readings):
            mult, label = bullish_mult, "bullish"
        else:
            mult, label = neutral_mult, "neutral"
        bm = f"{breadth_move:+.3f}%" if breadth_move is not None else "n/a"
        sm = f"{spy_move:+.3f}%" if spy_move is not None else "n/a"
        detail = f"NO VWAP - fell back to breadth {bm}, SPY {sm} since the open"

    # ---- CHOP, the fourth label ----------------------------------------
    #
    # Evaluated AFTER the index rule and allowed to override anything except
    # bearish. Order matters and is deliberate: a falling tape is worse than a
    # chopping one, so bearish keeps precedence, but a flat index reading must
    # not be allowed to call 2026-08-28 a normal day. SPY was flat that
    # session while the watchlist averaged -1.045% at 15 minutes, so the index
    # rule alone would have said neutral or bullish and sized accordingly.
    #
    # This is also why breadth_halt was retired rather than kept alongside:
    # chop lives here, inside the one guard that owns "how much risk is on",
    # instead of in a second guard that could disagree with it.
    if label != "bearish":
        choppy, chop_detail = _chop_reading(config, breadth_state)
        if choppy:
            mult = rc.get("choppy_multiplier", 0.5)
            label = "choppy"
            detail = chop_detail

    state["last_eval"] = now

    # ---- HYSTERESIS ---------------------------------------------------
    #
    # A reading is not a decision. SPY sitting a hundredth of a percent from
    # its VWAP will cross it repeatedly, and a regime that follows each
    # crossing whipsaws between full size and no-new-longs on noise - which is
    # worse than either setting held steadily, because entries get taken at
    # exactly the moments the reading happens to be favourable.
    #
    # So a NEW label must be observed on `confirmations` consecutive
    # evaluations before it replaces the current one. The very first reading
    # of the day is adopted immediately: there is nothing to whipsaw against
    # yet, and standing at 1.0x "pending confirmation" through the opening
    # minutes would be full size on no evidence.
    current = state.get("label")
    if current is None:
        state["multiplier"], state["label"] = mult, label
        state["pending"] = None
        logger.warning(
            f"===== REGIME at {now:%H:%M:%S} ET: {label.upper()} ({detail}) - "
            f"new entries sized at {mult:g}x."
            f"{' NO NEW LONGS.' if mult == 0 else ''} "
            f"Open positions and their exits are untouched. ====="
        )
        return mult, label

    if label == current:
        # The reading agrees with the standing regime; any part-built case
        # for changing it is abandoned rather than left to accumulate across
        # unrelated stretches of the session.
        state["pending"] = None
        return state["multiplier"], current

    need = int(rc.get("confirmations", 2) or 1)
    pending = state.get("pending") or {}
    count = (pending.get("count", 0) + 1) if pending.get("label") == label else 1
    state["pending"] = {"label": label, "count": count}

    if count < need:
        logger.info(
            f"REGIME: reading says {label.upper()} ({detail}) but the standing "
            f"regime is {current.upper()} - {count}/{need} confirmations. "
            f"Holding {state['multiplier']:g}x."
        )
        return state["multiplier"], current

    state["multiplier"], state["label"] = mult, label
    state["pending"] = None
    logger.warning(
        f"===== REGIME CHANGE at {now:%H:%M:%S} ET: {current.upper()} -> "
        f"{label.upper()} ({detail}), confirmed on {need} consecutive readings - "
        f"new entries sized at {mult:g}x."
        f"{' NO NEW LONGS.' if mult == 0 else ''} "
        f"Open positions and their exits are untouched. ====="
    )
    return mult, label


_DYNAMIC_STOPS = {"engine": None}
# The live regime state, so _attempt_entry can read the current label without
# it being threaded through every caller. Set once per session by
# run_trading_day; the dict it points at is mutated in place by
# _regime_multiplier, so this never goes stale.
_REGIME_STATE = {}
# symbol -> (monotonic_ts, tradable_or_None, reason). One asset lookup per
# symbol per TTL rather than one per poll - see the halt check in
# _attempt_entry.
_HALT_CACHE = {}


def _build_dynamic_stops(config, screener=None, history_path="logs/trade_history.csv"):
    """
    Build the session's DynamicStops engine, or None when it is switched off.

    Two inputs, both optional, and the engine degrades to the static stop when
    neither is present:

      ATR  - the screener already computes a real atr_pct per symbol and keeps
             it in _atr_pct. This is the input that needs NO trade history at
             all, which is what makes the feature usable today: it is a
             measurement of how much a name moves, not of how past trades in
             it turned out. A $10 name that travels 3% a day and a $250 name
             that travels 0.8% should not share a stop, and until now they did.

      MAE  - per-symbol drawdown history, used only where a symbol has at
             least min_samples of its own. No symbol does yet, so in practice
             the pooled distribution or nothing is used. It costs nothing to
             wire now and starts contributing as history accumulates.

    The result can only ever be TIGHTER than final_exit_loss_pct - see
    DynamicStops.stop_for. Worst case is exactly today's behaviour.
    """
    try:
        from src.analytics.dynamic_stops import DynamicStops
    except Exception as e:
        logger.warning(f"dynamic stops unavailable: {e}")
        return None
    if not ((config.get("trading") or {}).get("dynamic_stops") or {}).get("enabled"):
        return None

    atr = {}
    try:
        atr = dict(getattr(screener, "_atr_pct", {}) or {})
    except Exception as e:
        logger.debug(f"dynamic stops: no ATR map available ({e})")

    history = {}
    try:
        import csv as _csv
        with open(history_path, newline="") as fh:
            for row in _csv.DictReader(fh):
                sym, mae = row.get("symbol"), row.get("mae_pct")
                if not sym or mae in (None, ""):
                    continue
                try:
                    history.setdefault(sym, []).append(float(mae))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"dynamic stops: could not read {history_path} ({e})")

    engine = DynamicStops(config, history=history, atr_by_symbol=atr)
    logger.info(
        f"DYNAMIC STOPS enabled - ATR for {len(atr)} symbol(s), MAE history for "
        f"{len(history)} symbol(s). Stops are capped at the static "
        f"{config['trading'].get('final_exit_loss_pct')}% and floored at "
        f"{-abs(engine.floor_pct)}%, so they can only ever be TIGHTER."
    )
    return engine


def _chop_exit_config(config, regime_state, base_override=None):
    """
    Lower the take-profit ladder while the regime reads CHOPPY.

    The most useful single response to chop, and the least obvious. On a
    choppy tape +0.5% IS the whole move - 2026-08-28 had 19 of 30 positions
    peak under +0.5% - while the ladder sits at 1.0/1.25/1.5%. So positions
    that were genuinely up got carried back through breakeven waiting for a
    level the day was never going to produce. Taking something off at +0.4%
    converts those from small losses into small wins, which is the entire
    difference between a 26% hit rate and a profitable one at a 2.90x payoff.

    Applied at ENTRY, so a position keeps whatever ladder it opened with for
    its whole life - the same rule the burst profile follows, and for the same
    reason: exit rules changing under an open position make its outcome
    unattributable to either setting.
    """
    if (regime_state or {}).get("label") != "choppy":
        return base_override
    cfg = ((config.get("trading") or {}).get("regime_sizing") or {}).get("chop") or {}
    tiers = cfg.get("take_profit_tiers")
    if not tiers:
        return base_override
    try:
        base = copy.deepcopy(base_override) if base_override else copy.deepcopy(config)
        base["trading"]["take_profit_tiers"] = copy.deepcopy(tiers)
        return base
    except Exception as e:
        logger.debug(f"chop exit profile skipped: {e}")
        return base_override


def _dynamic_exit_config(config, symbol, base_override=None):
    """
    Layer a symbol's dynamic stop onto whatever exit config it would otherwise
    use, or return `base_override` unchanged.

    Applied at ENTRY only. The milestone recalculation DynamicStops supports is
    deliberately not wired yet - moving a live position's stop mid-trade is a
    larger change to TradeManager and wants its own testing pass.
    """
    engine = _DYNAMIC_STOPS.get("engine")
    if engine is None:
        return base_override
    try:
        stop, why = engine.stop_for(symbol, 0.0)
        base = copy.deepcopy(base_override) if base_override else copy.deepcopy(config)
        current = base["trading"].get("final_exit_loss_pct")
        if current is not None and stop >= current:
            # No tightening available for this name - leave it exactly as it
            # would have been rather than logging a change that is not one.
            return base_override
        base["trading"]["final_exit_loss_pct"] = stop
        logger.info(f"{symbol}: DYNAMIC STOP {current}% -> {stop}% ({why})")
        return base
    except Exception as e:
        logger.debug(f"{symbol}: dynamic stop skipped ({e})")
        return base_override


def _benchmark_symbols(config, symbols):
    """
    Context symbols to stream alongside the watchlist: SPY plus whichever sector
    ETFs today's names map to.

    These are never traded. They exist so relative strength is measured against
    a benchmark sampled the same way the symbol is - which was not true before:
    SPY came over REST while streamed symbols came live, so the two sides of
    `signal_pct - spy_pct` were minutes apart.

    Returns [] when the stream is off or benchmarks are disabled, in which case
    everything behaves exactly as it did.
    """
    if not config["trading"].get("stream_benchmarks", True):
        return []
    # QQQ joins SPY as of 2026-09-02: regime_sizing's VWAP rule needs BOTH
    # indices, and a regime read off a REST-delayed QQQ would be answering
    # "was QQQ above its VWAP a quarter of an hour ago". Cheap now that the
    # cap counts unique symbols (~30) rather than channel-subscriptions.
    out = ["SPY", "QQQ"]
    try:
        from src.analytics import sectors as SEC
        out += SEC.sectors_for(symbols)
    except Exception as e:
        logger.warning(f"Could not resolve sector benchmarks: {e}")
    return [s for s in dict.fromkeys(out) if s not in set(symbols)]


def _stream_priority_with_indices(config, ranked_symbols, benchmarks,
                                  indices=("SPY", "QQQ")):
    """
    Guarantee SPY and QQQ a subscription slot, ahead of the TAIL of the
    watchlist but behind its best names.

    Benchmarks were appended last and therefore never actually streamed: on
    2026-09-01 the watchlist filled every slot and SPY fell to REST, which is
    ~15 minutes delayed on this tier. That was tolerable while SPY only fed
    excess_vs_spy_pct. It is not tolerable now that regime_sizing reads SPY
    and QQQ against their own VWAP and can size the whole day to ZERO on what
    it sees - a regime decided from quarter-hour-old index prices is worse
    than no regime rule at all.

    Two slots, not all benchmarks: the sector ETFs stay last, because they
    only inform a journal column. The reserve is also capped at a fraction of
    the budget so a small watchlist is never mostly benchmarks.

    Returns a priority list; the stream keeps the first symbol_budget() of it.
    """
    t = config.get("trading", {})
    if not t.get("stream_reserve_index_slots", True):
        return list(ranked_symbols)

    wanted = [s for s in indices if s in set(benchmarks or ())]
    if not wanted:
        return list(ranked_symbols)

    budget = max(1, int(t.get("stream_max_subscriptions", 30)))
    # Never let the reserve take more than a third of the budget.
    if len(wanted) > max(1, budget // 3):
        return list(ranked_symbols)

    head = [s for s in ranked_symbols if s not in set(wanted)]
    keep = max(0, budget - len(wanted))
    out = head[:keep] + wanted + head[keep:]
    logger.info(
        f"Stream priority: reserving {len(wanted)} slot(s) for {', '.join(wanted)} "
        f"so the regime rule reads live index prices - the last "
        f"{max(0, len(head) - keep)} watchlist name(s) fall back to REST"
    )
    return list(dict.fromkeys(out))


def _refresh_candidate_pool(config, screener):
    """
    Repoint the screener at a dynamically built candidate pool.

    Deliberately mutates screener.candidates rather than adding a parallel path
    through the screener: everything downstream - score_stock, the price band,
    the merit ordering, last_scores, last_details - then works exactly as it did
    on the static list, and the ONLY thing that changed is which symbols arrived.
    A second code path would have meant two ways for a symbol to be selected and
    two places for that to go wrong.

    Silent on failure by design: select_candidates returns the static pool when
    anything goes wrong, so a network problem in the universe build degrades to
    the previous behaviour instead of emptying the watchlist.
    """
    if screener is None:
        return None

    cap = config["trading"].get("max_screen_candidates", 0)

    if not config["trading"].get("use_dynamic_universe", False):
        # Even with the dynamic universe off, the static pool now runs to ~360
        # names and the screener costs ~1.65s each - measured 2026-08-27, 92
        # candidates in 151.8s. Left uncapped that is ten minutes of a
        # twenty-five minute pre-open window, and every minute it overruns is a
        # minute the QQQ list and the stream subscription do not get.
        if cap and len(screener.candidates) > cap:
            logger.info(
                f"Capping the static candidate pool at {cap} "
                f"(of {len(screener.candidates)}) to keep the screener inside its window"
            )
            screener.candidates = screener.candidates[:cap]
        return None

    try:
        from src.screener import universe as U
        static_pool = list(getattr(screener, "candidates", []) or [])
        candidates, info = U.select_candidates(
            screener.broker, config, static_pool=static_pool
        )
        if candidates and info.get("source") == "dynamic":
            if cap and len(candidates) > cap:
                # Safe to cut from the tail: select_candidates returns the merged
                # pool sorted best-first by cheap score, so the names dropped are
                # the ones least likely to move today - not whichever happened to
                # sit late in a hand-written file.
                logger.info(
                    f"Capping the candidate pool at {cap} of {len(candidates)} "
                    f"(shortlist {info.get('shortlist')} + static pool, ranked "
                    f"best-first - the cut takes the lowest-scoring names)"
                )
                candidates = candidates[:cap]
            screener.candidates = candidates
            logger.info(
                f"===== DYNAMIC UNIVERSE: {info['universe']} liquid symbols -> "
                f"top {info['shortlist']} -> {len(candidates)} candidates ====="
            )
            return info
        logger.warning(
            "Dynamic universe produced nothing usable - screening the static pool"
        )
    except Exception as e:
        logger.error(f"Dynamic universe failed ({e}) - screening the static pool",
                     exc_info=True)
    return None


def select_symbols(config, screener, market_data):
    """
    Run the daily screener (if enabled) and return the symbol list to trade,
    ALWAYS merged with the static stock_universe default list - the default
    list is watched every day no matter what the screener does (previously
    this was either/or: screener picks OR the fallback list, never both).
    The screener's picks (if it ran and found any) are added on top of the
    defaults, deduplicated.

    Also computes RSI(rsi_period) once per symbol here, at market-open, and
    returns it alongside the symbol list. This is NOT used to filter which
    symbols get watched - a daily-bar RSI barely moves within a single
    trading day, so a once-at-open computation is exactly as fresh as any
    later recomputation would be. Instead it's checked in run_trading_day as
    one half of the combined entry signal: a rapid price increase AND
    RSI < rsi_max_for_entry at the same time is what actually triggers a buy,
    so every symbol still gets watched for the price signal regardless of its
    RSI, but only enters when both conditions line up together.

    The timeout runs the screener in a background thread and simply stops
    waiting after screener_timeout_seconds, rather than trying to interrupt it
    with a signal. A signal-based interrupt was tried first and doesn't work
    here: get_historical_bars() (and every scoring helper in stock_screener.py)
    wraps its body in a broad `except Exception`, which would silently catch
    and swallow a signal-raised TimeoutError before it could ever reach this
    function - and since signal.alarm() only fires once, that single swallowed
    exception would fully disable the timeout for the rest of the run.
    Abandoning a background thread instead sidesteps that entirely: this
    function just stops waiting, regardless of what the thread does internally.
    """
    screener_ran = False
    screener_timed_out = False
    symbols = []

    if config["trading"].get("use_daily_screener", False) and screener is not None:
        logger.info("===== PRE-MARKET SCREENER =====")
        screener_ran = True
        timeout_seconds = config["trading"].get("screener_timeout_seconds", 420)

        # Build the candidate pool before screening, not after: the expensive
        # per-symbol scoring below runs over whatever this leaves in
        # screener.candidates.
        universe_info.clear()
        info = _refresh_candidate_pool(config, screener)
        if info:
            universe_info.update(info)

        executor = ThreadPoolExecutor(max_workers=1)
        screener_started = time.monotonic()
        future = executor.submit(
            screener.screen,
            top_n=config["trading"]["num_stocks_to_trade"],
            min_score=config["trading"]["min_screener_score"],
        )
        try:
            symbols = future.result(timeout=timeout_seconds)
            logger.info(
                f"Screener finished in {time.monotonic() - screener_started:.1f}s "
                f"(timeout is {timeout_seconds}s)"
            )
            # Built HERE because this is the first moment the screener's
            # per-symbol ATR map exists. A screener timeout leaves the engine
            # unbuilt, which means the static stop - the correct fallback.
            _DYNAMIC_STOPS["engine"] = _build_dynamic_stops(config, screener)
        except FutureTimeoutError:
            screener_timed_out = True
            logger.warning(
                f"Screener did not finish within {timeout_seconds}s - "
                f"aborting and falling back to static stock_universe list"
            )
            symbols = []
        except Exception as e:
            logger.error(
                f"Screener failed after {time.monotonic() - screener_started:.1f}s: {e}"
            )
            symbols = []
        finally:
            # Don't block waiting for an abandoned/still-running screener thread -
            # we've already moved on; let it finish (or not) in the background.
            executor.shutdown(wait=False)

    candidates_evaluated = len(screener.candidates) if screener is not None else 0

    if not symbols and screener_ran and not screener_timed_out:
        logger.warning("Screener produced no candidates")

    # Stream slots are scarce (~14) and must go to the names most likely to
    # produce the move the strategy is built to catch - not to whichever symbol
    # sorts first. screen() already returns picks in descending score order, so
    # that order IS the merit order; fall back to list order if scores are
    # unavailable (screener disabled, crashed, or fell back to the static list).
    screener_details.clear()
    if screener is not None:
        screener_details.update(getattr(screener, "last_details", None) or {})

    by_merit = list(symbols)
    ranked = getattr(screener, "last_scores", None) if screener is not None else None
    if ranked:
        by_merit = sorted(symbols, key=lambda s_: ranked.get(s_, 0), reverse=True)
        logger.info(
            "Stream priority (best first): "
            + ", ".join(f"{s_}={ranked.get(s_, 0):.0f}" for s_ in by_merit[:14])
        )
    stream_priority["symbols"] = by_merit
    default_list = config["trading"]["stock_universe"]
    merge_default = config["trading"].get("merge_default_universe", True)

    if merge_default:
        merged = list(dict.fromkeys(symbols + default_list))  # screener picks first, then defaults, deduped
        if len(merged) != len(symbols):
            logger.info(
                f"Merged screener picks with the default stock_universe list: "
                f"{len(symbols)} screener + {len(default_list)} default -> {len(merged)} total watched"
            )
        symbols = merged
    elif symbols:
        # stock_universe is a CANDIDATE POOL, not an auto-include: every name in
        # it was scored by the screener above (see StockScreener._load_candidates)
        # and only the ones that earned a place are here. Until 2026-08-21 the
        # whole 50-name list was appended unconditionally, so 50 of 56 watched
        # symbols had passed no test at all - and with only ~14 WebSocket slots,
        # most of what could be traded was running on 15-minute-delayed prices.
        logger.info(
            f"Watching the screener's {len(symbols)} picks only "
            f"(merge_default_universe is off - the {len(default_list)} "
            f"stock_universe names were scored as candidates, not auto-included)"
        )
    else:
        # Screener errored, timed out, or found nothing above min_screener_score.
        # Fall back to the static list rather than trading nothing - this is the
        # one case where the unfiltered universe is still better than an empty
        # watchlist.
        logger.warning(
            f"No screener picks to watch - falling back to the {len(default_list)} "
            f"static stock_universe names so the session still has a watchlist"
        )
        symbols = list(dict.fromkeys(default_list))

    symbols = _filter_watchlist_by_price(config, symbols)
    symbols = _filter_watchlist_by_exclusions(config, symbols)
    _warn_if_watchlist_outruns_the_stream(config, symbols)

    logger.info(
        f"Symbol selection: screener_ran={screener_ran}, "
        f"candidates_evaluated={candidates_evaluated}, "
        f"symbols_selected={len(symbols)} ({', '.join(symbols)})"
    )

    rsi_values = {}
    if config["trading"].get("use_rsi_filter", False):
        rsi_period = config["trading"].get("rsi_period", 14)
        rsi_values = {symbol: market_data.get_rsi(symbol, period=rsi_period) for symbol in symbols}
        logger.info(
            "RSI(%d) at market open: %s",
            rsi_period,
            ", ".join(
                f"{s}={rsi_values[s]:.1f}" if rsi_values[s] is not None else f"{s}=N/A"
                for s in symbols
            ),
        )

    return symbols, rsi_values

def _augment_selection(config, screener, market_data, selection, stages=("earnings", "qqq")):
    """
    Run the earnings / QQQ list pass over an existing (symbols, rsi_values)
    selection and return an updated one.

    Kept separate from select_symbols because it runs LATER - the screener goes
    at 09:05 to be safely finished before the bell, but both of these inputs
    need pre-market to have woken up first: earnings reactions need prints to
    have accumulated, and the QQQ read is a read of today's tape. Running them
    at 09:05 would measure an empty book.

    RSI is recomputed for the added names only, when the filter is on - without
    this they would arrive with no RSI value and be silently unfilterable.
    """
    symbols, rsi_values = selection
    try:
        full, added = augment_symbols(config, screener, symbols, stages=stages)
    except Exception as e:
        logger.error(f"List augmentation failed, keeping the screener list as-is: {e}", exc_info=True)
        return selection

    # Earnings / QQQ adds are the day's freshest catalysts - they outrank the
    # static universe for a stream slot.
    stream_priority["symbols"] = list(dict.fromkeys(stream_priority["symbols"] + added))

    # Remember WHERE each symbol came from, so P&L can be attributed to the
    # thing that found it. On 2026-08-27 the earnings and QQQ lists produced
    # $337 of a $535 day from three names, and establishing that took a manual
    # cross-reference against the log. A stage label makes it a column.
    for sym in added:
        symbol_source[sym] = stages[0] if len(stages) == 1 else "list"

    if added and config["trading"].get("use_rsi_filter", False):
        rsi_period = config["trading"].get("rsi_period", 14)
        rsi_values = dict(rsi_values)
        for sym in added:
            try:
                rsi_values[sym] = market_data.get_rsi(sym, period=rsi_period)
            except Exception as e:
                logger.warning(f"RSI unavailable for added symbol {sym}: {e}")
                rsi_values[sym] = None

    return full, rsi_values


# Scheduled report sends. Shared across the session so a report fired by
# finish_day and one fired by the clock can't double-send the same slot.
report_state = {"sent": set(), "seeded": False}

# Symbols that must get a WebSocket slot when the feed's subscription cap forces
# a choice: the screener's picks and the day's earnings/QQQ adds. Those are the
# names a signal is actually likely to fire on, whereas most of the 50-name
# default universe sits untouched all session. Everything not streamed still
# gets prices over REST - the per-symbol fallback was built for exactly this.
stream_priority = {"symbols": []}
# symbol -> which stage put it on the watchlist ("earnings", "qqq"); anything
# absent came from the screener. Reporting only - never read by a trading rule.
symbol_source = {}

# Per-symbol screener detail from this session's run, so the signal journal can
# record what the screener SAW about a symbol next to what the signal did. Held
# at module scope for the same reason as stream_priority: it is produced in
# select_symbols and consumed deep in the trading loop, and threading it through
# every call between the two would touch a lot of well-tested signatures.
screener_details = {}
# Set by _refresh_candidate_pool so the report can say where today's
# candidates came from - a static list and a 1,000-name sweep produce very
# different sessions and the log should not leave that ambiguous.
universe_info = {}


def _update_vwap(acc, symbol, bar):
    """Accumulate session VWAP from a bar. No-op if the bar carries no volume."""
    try:
        vol = float(bar.get("volume") or 0)
        if vol <= 0:
            return
        hi, lo, close = float(bar.get("high") or 0), float(bar.get("low") or 0), float(bar.get("close") or 0)
        typical = (hi + lo + close) / 3 if (hi and lo and close) else close
        if typical <= 0:
            return
        slot = acc.setdefault(symbol, [0.0, 0.0])
        slot[0] += typical * vol
        slot[1] += vol
    except Exception:
        pass


def _vwap(acc, symbol):
    slot = acc.get(symbol)
    if not slot or slot[1] <= 0:
        return None
    return slot[0] / slot[1]


def _continuation_fields(config, symbol, price, signal_pct, spy_pct,
                         prices, volumes, vwap, details, rvol=None, spread_pct=None,
                         sector_returns=None):
    """
    Compute every continuation factor available for this signal.

    Returns a flat dict for the signal journal. Scored only if
    use_continuation_score is on; the factors are recorded either way, which is
    the point - two weeks of these against forward returns is what turns the
    weights from a guess into a fit.
    """
    from src.analytics import continuation as C
    from src.analytics import sectors as SEC

    t = config["trading"]
    atr_pct = (details or {}).get("volatility_percentile")
    atr_pct = None  # volatility_percentile is a rank, not a %; leave unscaled

    factors = {
        "efficiency": C.efficiency_ratio(list(prices) if prices else None),
        "rel_strength": C.relative_strength(signal_pct, spy_pct),
        "vol_accel": C.volume_acceleration(list(volumes) if volumes else None),
        "vwap_pos": C.vwap_position(price, vwap, atr_pct),
        "exhaustion": C.exhaustion(signal_pct, price, vwap, atr_pct),
        "rvol": C.relative_volume(rvol),
        "spread": C.spread_quality(spread_pct),
        "breakout": C.breakout_quality(
            price, (details or {}).get("prior_high"), (details or {}).get("opening_high")
        ),
        # Excess return over the symbol's OWN sector, not the index. A miner up
        # 3% on a day the whole mining complex is up 3% has shown nothing about
        # itself - it scores 50 here while scoring highly against SPY.
        "sector_strength": SEC.sector_strength(symbol, signal_pct, sector_returns),
    }

    out = {f"cf_{k}": (round(v, 1) if v is not None else None) for k, v in factors.items()}
    out["cf_vwap"] = round(vwap, 4) if vwap else None
    # Which benchmark the sector figure was measured against, so a journal row
    # is self-describing: "50" means nothing without knowing 50 against what.
    out["cf_sector_etf"] = SEC.sector_for(symbol)

    # Always scored, regardless of use_continuation_score. The flag controls
    # whether the score is ALLOWED TO DECIDE anything (see _rank_burst), not
    # whether it is computed - the journal needs the column either way, and
    # that is what makes the weights fittable rather than permanent guesses.
    out["cf_score"] = C.continuation_score(factors, t.get("continuation_weights", {}))
    return out


def _rank_burst(config, candidates):
    """
    Order a poll's simultaneous signals best-first by continuation score.

    This is the only place the score is allowed to change a decision, and it
    changes WHICH signals get taken, never HOW MANY. The burst throttle keeps
    the first burst_max of the list; until now "first" meant list order, which
    is the screener's alphabetical-ish ordering - i.e. the bot was picking
    which of twenty correlated signals to buy by an accident of sorting.

    Ranking is a weaker claim than gating, which is why it is what got built.
    A minimum-score floor would REMOVE trades on the strength of weights that
    are still guesses; ranking only reorders a cut that was already being made
    arbitrarily, so the worst case is that the score is uninformative and the
    order is no better than the accident it replaced.

    Candidates whose score could not be computed sort last but are NOT dropped -
    same reasoning as continuation_score renormalising over missing factors:
    "not measurable" is not "bad".

    Returns (ordered_candidates, note) - note is None when ranking is off.
    """
    if not config["trading"].get("use_continuation_score", False):
        return candidates, None
    if len(candidates) < 2:
        return candidates, None

    def key(cand):
        score = (cand.get("cont") or {}).get("cf_score")
        return (0, 0.0) if score is None else (1, score)

    ordered = sorted(candidates, key=key, reverse=True)

    shown = ", ".join(
        f"{c['symbol']}={(c.get('cont') or {}).get('cf_score'):.0f}"
        if (c.get("cont") or {}).get("cf_score") is not None
        else f"{c['symbol']}=n/a"
        for c in ordered
    )
    return ordered, f"ranked by continuation score: {shown}"


def _opening_move_fields(details_by_symbol, symbol):
    """Opening-move stats for the journal row, or blanks if unavailable."""
    d = (details_by_symbol or {}).get(symbol) or {}
    return {
        "opening_hit_rate": d.get("opening_hit_rate"),
        "opening_avg_gain": d.get("opening_avg_gain"),
        "opening_sessions": d.get("opening_sessions"),
        "opening_efficiency": d.get("opening_efficiency"),
        "opening_directional": d.get("opening_directional"),
    }


def _is_excluded_symbol(config, symbol):
    """(True, reason) if this symbol must never be traded. Never raises - a
    failure here must not be able to block an otherwise valid entry."""
    try:
        from src.screener.exclusions import is_excluded
        return is_excluded(symbol, config.get("trading") or {})
    except Exception as e:
        logger.debug(f"exclusion check skipped for {symbol}: {e}")
        return False, None


def _filter_watchlist_by_exclusions(config, symbols):
    """
    Drop symbols this strategy must never trade (leveraged/inverse ETFs,
    index baskets, anything in exclude_symbols) from the WATCHLIST.

    Second of the three layers, mirroring the price band exactly: the
    screener excludes before ranking, this cleans anything that arrives
    through a path the screener did not rank (the static stock_universe, the
    earnings and QQQ lists, a screener fallback), and _attempt_entry keeps a
    backstop at the moment of purchase.

    Evidenced 2026-09-01: SOXL and TQQQ were both watched and SOXL was the
    first opening-burst entry of the day. max_stock_price only blocks QQQ at
    ~$711 by accident; a leveraged fund at $106 sails through.
    """
    from src.screener.exclusions import is_excluded
    trading = config.get("trading") or {}

    kept, dropped = [], []
    for sym in symbols:
        excluded, reason = is_excluded(sym, trading)
        (dropped if excluded else kept).append((sym, reason) if excluded else sym)

    if dropped:
        logger.info(
            f"Watchlist exclusions: dropped {len(dropped)} untradeable symbol(s) - "
            + ", ".join(f"{s} ({r})" for s, r in dropped)
        )
    if symbols and not kept:
        # Watching nothing guarantees a blank day, so keep the list and let
        # the entry-time gate reject individually - the same guard
        # _filter_watchlist_by_price uses for the identical reason.
        logger.warning(
            "Every watchlist symbol is on the exclusion list - keeping the list "
            "intact and letting the entry-time check reject individually"
        )
        return list(symbols)
    return kept


def _filter_watchlist_by_price(config, symbols):
    """
    Drop symbols outside the tradeable price band from the WATCHLIST.

    min_stock_price / max_stock_price were enforced only at entry, so an
    ineligible name stayed on the watchlist all session, was polled every cycle,
    fired signals, and was rejected each time. On 2026-08-24 AMC signalled ten
    separate times at ~$2.70 and was refused ten times; TLRY and PTON did the
    same. Every one of those consumed a stream slot or a REST call and, worse,
    counted toward the burst width that throttles genuine entries.

    Prices come from the screener's own details, gathered minutes earlier. A
    symbol with NO price is KEPT: absent data is not evidence of an out-of-band
    price, and the entry-time check still catches it. The entry check stays in
    place regardless - this reduces noise, it does not replace the gate.
    """
    t = config["trading"]
    min_price = t.get("min_stock_price") or 0
    max_price = t.get("max_stock_price") or 0
    if not min_price and not max_price:
        return symbols

    kept, dropped = [], []
    for sym in symbols:
        price = (screener_details.get(sym) or {}).get("price")
        if not price:
            kept.append(sym)          # unknown price -> keep, entry gate decides
            continue
        if min_price and price < min_price:
            dropped.append(f"{sym} (${price:.2f} < ${min_price})")
            continue
        if max_price and price > max_price:
            dropped.append(f"{sym} (${price:.2f} > ${max_price})")
            continue
        kept.append(sym)

    if dropped:
        logger.info(
            f"Watchlist price filter: dropped {len(dropped)} symbol(s) outside "
            f"${min_price}-${max_price} before the session starts - "
            f"{', '.join(dropped)}"
        )
    if not kept:
        # Every candidate priced out. Watching nothing guarantees a blank day,
        # so keep the list and let the entry gate reject individually.
        logger.warning(
            "Price filter would empty the watchlist - keeping it intact and "
            "letting the entry-time check do the rejecting instead"
        )
        return symbols
    return kept


def _warn_if_watchlist_outruns_the_stream(config, symbols):
    """
    Say plainly how much of the watchlist will run on delayed REST prices.

    The two settings are easy to drift apart: widening the watchlist is a
    one-line config change, while the WebSocket budget is fixed by the data
    plan. On 2026-08-21, 59 symbols were watched against 0 streamed slots and
    the log never once said that 100% of entries were being decided on
    ~15-minute-old prices.
    """
    t = config["trading"]
    if not t.get("use_websocket_stream", False):
        logger.info(
            f"WebSocket stream is OFF - all {len(symbols)} watched symbols use "
            f"REST prices (~15 min delayed)"
        )
        return

    # MUST match PriceStream.symbol_budget() - this used to divide by 2 when
    # trade ticks were on, which stopped being true on 2026-09-02 when the
    # cap became a count of UNIQUE SYMBOLS rather than channel-subscriptions.
    # Left unfixed it would report "44 symbols on REST" for a watchlist where
    # only 29 actually are, i.e. the operator's own pre-flight readout would
    # disagree with what the stream then did.
    cap = t.get("stream_max_subscriptions", 30)
    budget = max(1, cap)

    if len(symbols) <= budget:
        logger.info(
            f"All {len(symbols)} watched symbols fit the stream budget "
            f"({budget}) - every tradeable name gets live prices"
        )
        return

    on_rest = len(symbols) - budget
    logger.warning(
        f"Watchlist ({len(symbols)}) exceeds the stream budget ({budget}): "
        f"{on_rest} symbol(s), {on_rest / len(symbols) * 100:.0f}% of the "
        f"watchlist, will trade on REST prices ~15 min delayed. Entry quality "
        f"on those is the problem measured on 2026-08-20 (losing entries landed "
        f"at the 87th percentile of the surrounding half-hour). Either shrink "
        f"num_stocks_to_trade or raise stream_max_subscriptions."
    )


def _opening_exit_profile_rows(config):
    """
    [(label, normal, opening)] for the report, or [] when nothing is overridden.

    Only rows that actually DIFFER are returned. A table restating settings that
    are identical in both profiles buries the handful that are not, and the
    whole point of showing this beside the results is to make the difference
    legible.
    """
    oc = _opening_exit_config(config)
    if not oc:
        return []
    n, o = config["trading"], oc["trading"]

    def tiers(part, key, field):
        return "/".join(str(t.get(field)) for t in (part.get(key) or [])) + "%"

    rows = [
        ("first exit", f"{n.get('first_exit_loss_pct')}%", f"{o.get('first_exit_loss_pct')}%"),
        ("final exit", f"{n.get('final_exit_loss_pct')}%", f"{o.get('final_exit_loss_pct')}%"),
        ("trailing stop", f"{n.get('trailing_stop_pct')}%", f"{o.get('trailing_stop_pct')}%"),
        ("take-profit", tiers(n, "take_profit_tiers", "gain_pct"),
         tiers(o, "take_profit_tiers", "gain_pct")),
        ("breakeven trigger", tiers(n, "breakeven_tiers", "trigger_pct"),
         tiers(o, "breakeven_tiers", "trigger_pct")),
    ]
    return [r for r in rows if r[1] != r[2]]


def _set_run_context(config, email_notifier, symbols, price_stream):
    """
    Record HOW this session is running, for the band at the top of every report.

    Captured at session start rather than at report time so it reflects what was
    actually configured, and refreshed by _refresh_run_context once the stream
    has had a chance to fail - a session that started on the stream and fell
    back to REST at 09:32 must not report itself as a streamed session.
    """
    _ALERT_CONFIG["config"] = config
    t = config["trading"]
    try:
        streamed = len(price_stream._symbols) if price_stream is not None else 0
        email_notifier.run_context = {
            "symbols_watched": len(symbols),
            "symbols_streamed": streamed,
            "symbols_rest": len(symbols) - streamed,
            "trade_ticks": bool(t.get("use_trade_ticks_for_entry", False)) and streamed > 0,
            "price_source": "stream" if streamed else "REST",
            "feed": t.get("websocket_feed", "iex") if streamed else "",
            "symbols_note": f"cap {t.get('stream_max_subscriptions', 30)} subscriptions",
            "use_resistance_exit": t.get("use_resistance_exit", True),
            "rapid_increase_max_pct": t.get("rapid_increase_max_pct", 0),
            "opening_exits": _opening_exit_profile_rows(config),
            "rapid_increase_pct": t.get("rapid_increase_pct"),
            "reentry_cooldown_minutes": t.get("reentry_cooldown_minutes", 0),
            "reentry_cooldown_after_loss_only": t.get("reentry_cooldown_after_loss_only", True),
        }
        logger.info(f"Run context: {email_notifier.run_context}")
    except Exception as e:
        logger.debug(f"Could not build run context: {e}")


# _refresh_run_context is called from places that do not carry `config`, and
# threading it through every caller for one alert is not worth the churn.
_ALERT_CONFIG = {}


def _refresh_run_context(email_notifier, price_stream):
    """Downgrade the run context to REST if the stream gave up mid-session."""
    try:
        if price_stream is None or not email_notifier.run_context:
            return
        if getattr(price_stream, "_gave_up", False):
            ctx = email_notifier.run_context
            if ctx.get("price_source") != "REST (stream failed)":
                ctx["price_source"] = "REST (stream failed)"
                ctx["symbols_streamed"] = 0
                ctx["symbols_rest"] = ctx.get("symbols_watched", 0)
                ctx["trade_ticks"] = False
                logger.info("Run context downgraded: the stream gave up, this session is REST")
                try:
                    from src.notifications import alerts as _AL
                    _AL.degraded(
                        _ALERT_CONFIG.get("config"), email_notifier,
                        what="price stream failed - running on delayed REST",
                        detail=(
                            "The WebSocket gave up and this session is now reading "
                            "REST prices, which the free tier delays by ~15 minutes.\n\n"
                            "Entries and exits are being decided on stale prices. "
                            "Entry quality measurably degrades in this state "
                            "(2026-08-20: losing entries landed at the 87th percentile "
                            "of the surrounding half-hour).\n\n"
                            "The bot continues to trade. Stop it if you would rather "
                            "it did not."
                        ),
                    )
                except Exception as e:
                    logger.debug(f"degraded alert failed: {e}")
    except Exception as e:
        logger.debug(f"Could not refresh run context: {e}")


def _poll_interval(config, market_data, base_interval, rest_interval, state):
    """
    Poll fast on streamed prices, slowly on REST.

    A 10-second loop is free while the WebSocket is serving: reads come from
    memory. The moment it falls back, that same loop becomes ~6x the REST calls
    and starts pushing at Alpaca's ~200/min ceiling - and being rate-limited
    would degrade the very fallback the bot is depending on.

    Rather than choose one interval for both worlds, choose per poll from what
    the stream is actually doing. Logged once on each transition so the switch
    is visible rather than something to infer from call volume.
    """
    stream = getattr(market_data, "stream", None)
    healthy = False
    try:
        healthy = stream is not None and stream.is_healthy()
    except Exception:
        healthy = False

    interval = base_interval if healthy else rest_interval

    # The opening minutes get a faster loop, but ONLY while the stream is
    # serving (reads come from memory, so the extra polls cost nothing) and
    # ONLY while something is actually open. Exits are evaluated on a poll, not
    # continuously, so the poll interval is a hard floor on how far price can
    # travel past a stop before the bot sees it - and 09:30-09:45 is where it
    # travels fastest. On 2026-09-02 a -1.0% stop realized -1.46%, -1.59% and
    # -1.07% on the three largest losses; a 10-second gap in the fastest tape
    # of the day is most of that.
    # Applies while positions are open (protecting stops) OR while the opening
    # burst is still measuring - that window has no positions by definition and
    # is the one place where a poll interval directly costs entries: the burst
    # decides by 09:33, so a 10-second loop gives it ~18 chances to see a move
    # and a 3-second loop gives it ~60.
    fast = (config.get("trading") or {}).get("opening_fast_poll") or {}
    if healthy and fast.get("enabled", True) and (
            state.get("positions_open") or state.get("burst_open")):
        try:
            import pytz
            _et = pytz.timezone("America/New_York")
            _now = datetime.now(_et)
            _open = _now.replace(hour=9, minute=30, second=0, microsecond=0)
            _until = _open + timedelta(minutes=fast.get("minutes_after_open", 15))
            if _open <= _now < _until:
                interval = min(interval, fast.get("seconds", 3))
        except Exception:
            pass

    if state.get("last") != interval:
        if healthy:
            logger.info(
                f"Polling every {interval}s (stream healthy - reads are free)"
            )
        else:
            logger.warning(
                f"Stream is not serving - slowing the poll to {interval}s so the "
                f"REST fallback stays inside Alpaca's rate limit. Faster polling "
                f"resumes automatically if the stream recovers."
            )
        state["last"] = interval
    return interval


def _flush_journal_safely(journal):
    """
    Last-ditch journal write on an abnormal exit.

    Wrapped because it runs inside except-handlers that are already dealing
    with a failure: losing the journal is bad, but raising here would mask the
    original error and skip the daily report as well.
    """
    try:
        if journal is not None:
            journal.flush(final=True)
    except Exception as e:
        logger.error(f"Could not flush the signal journal on shutdown: {e}")


def _slot_for_finish(et):
    """
    The scheduled slot a finish_day report should count as having covered.

    Only the close matters here: if the session ends at 16:00 by time-stop, the
    16:00 scheduled send is redundant. An all-closed report at 10:14 must NOT
    suppress the 10:35 status, because half an hour of tape happens in between.
    """
    now = datetime.now(et)
    return "16:00" if now.hour >= 16 else None


def _open_position_rows(strategy, market_data, et):
    """
    Snapshot every still-open position for a mid-session report.

    Prices come from the same market_data path the exit logic uses, so the
    report agrees with what the bot is acting on. A symbol that can't be priced
    is still listed - showing it with a stale price is far better than silently
    dropping a live position from a status report.
    """
    rows = []
    for symbol, trade in strategy.get_open_trades().items():
        current = None
        try:
            bar = market_data.get_latest_bar(symbol)
            if bar:
                current = bar.get("close")
        except Exception as e:
            logger.debug(f"Could not price {symbol} for the status report: {e}")
        if not current:
            current = trade.price_history[-1] if trade.price_history else trade.entry_price

        qty = trade.qty_remaining
        pl = (current - trade.entry_price) * qty
        pl_pct = ((current - trade.entry_price) / trade.entry_price * 100) if trade.entry_price else 0

        try:
            mfe, mae = trade.excursions()
        except Exception:
            mfe = mae = None

        held = ""
        try:
            mins = (datetime.now(et) - trade.entry_time).total_seconds() / 60
            held = f"{int(mins)} min"
        except Exception:
            pass

        rows.append({
            "symbol": symbol,
            "entry_price": trade.entry_price,
            "current_price": current,
            "qty_remaining": qty,
            "entry_qty": trade.entry_qty,
            "unrealized_pl": pl,
            "unrealized_pl_pct": pl_pct,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "entry_method": getattr(trade, "entry_method", None),
            "held_for": held,
        })
    return rows


def _maybe_send_scheduled_reports(config, email_notifier, strategy, executor, market_data, et):
    """
    Send the report at each configured wall-clock time.

    Called from BOTH the trading loop and the outer idle loop, because the
    session can finish early - on 2026-08-20 the last position closed at 10:14 -
    and a 16:00 report that only fires while trades are still running would
    simply never arrive on the days it ended before lunch.

    On startup, any slot already more than report_catchup_minutes past is marked
    as sent rather than fired, so restarting the bot at 15:00 does not blast out
    a "midday status" five hours late. A restart shortly after a slot still
    sends it.
    """
    notif = config.get("notifications", {})
    times = notif.get("report_times", []) or []
    if not times:
        return

    grace = timedelta(minutes=notif.get("report_catchup_minutes", 30))
    now = datetime.now(et)
    today = now.date()

    for slot in times:
        try:
            hh, mm = (int(x) for x in str(slot).split(":"))
        except Exception:
            logger.warning(f"Ignoring malformed notifications.report_times entry: {slot!r}")
            continue

        key = (today, slot)
        if key in report_state["sent"]:
            continue

        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < due:
            continue

        if not report_state["seeded"] and now > due + grace:
            # Missed while the process was down. Record it, don't send it.
            report_state["sent"].add(key)
            logger.info(f"Scheduled report for {slot} ET already {int((now-due).total_seconds()/60)} min past at startup - skipping")
            continue

        # Persist the trade log BEFORE reading it back for the report. The
        # scheduled send used to read whatever was on disk from the last save,
        # which on 2026-08-21 was a file it could not parse at all.
        try:
            executor.save_trades_log()
        except Exception as e:
            logger.error(f"Could not save the trade log before the scheduled report: {e}")

        open_rows = _open_position_rows(strategy, market_data, et)
        label = "Midday Status" if hh < 16 else "Closing Report"
        logger.info(
            f"===== SCHEDULED REPORT ({slot} ET): {label}, "
            f"{len(open_rows)} position(s) open ====="
        )
        try:
            email_notifier.send_report(
                burst_summary=executor.day_burst_summary,
                label=label,
                open_positions=open_rows,
            )
        except Exception as e:
            logger.error(f"Scheduled report for {slot} failed: {e}", exc_info=True)
        report_state["sent"].add(key)

    report_state["seeded"] = True


def _effective_stop_pct(config, symbol, trading):
    """
    The stop this symbol will actually be given, as a negative percent.

    The dynamic-stops engine when it is on and has something to say about this
    name, otherwise the static final_exit_loss_pct. Kept separate from
    _dynamic_exit_config so sizing and the exit rule read the SAME number - if
    they disagree, dollar risk per trade silently stops being constant, which
    is the whole property this is here to preserve.
    """
    static = trading.get("final_exit_loss_pct", -1.0)
    engine = _DYNAMIC_STOPS.get("engine")
    if engine is None or not symbol:
        return static
    try:
        stop, _ = engine.stop_for(symbol, 0.0)
        return stop if stop else static
    except Exception:
        return static


def _position_size(config, executor, price, symbol=None, volume_history=None):
    """
    Shares to buy for one entry. Returns 0 when no position should be taken.

    Sizing used to be a flat `max_position_per_stock_usd / price` - a fixed
    $10,000 of notional into every name regardless of account size or how many
    positions were already open. With 10 concurrent slots that is $100,000
    committed against ~$95,000 of equity, i.e. the sizing rule itself assumed
    leverage and left max_total_exposure_fraction to arbitrarily reject
    whichever entry happened to arrive once the book filled up.

    The budget is now the SMALLEST of three independent ceilings, so whichever
    constraint binds first wins:

      1. Even slot share - equity * max_total_exposure_fraction
                           / max_concurrent_positions
         Splits deployable capital evenly across the book, so a full book
         lands at the exposure cap by construction rather than by rejection.
         Scales with the account automatically; no re-tuning as it grows.

      2. Risk budget - equity * max_risk_per_trade_fraction
                       / (final_exit_loss_pct / 100)
         The largest position whose hard stop costs at most
         max_risk_per_trade_fraction of equity. This is the one that ties
         size to the stop: widen final_exit_loss_pct and positions shrink on
         their own. With a 1% stop it sits well above ceiling 1 and stays
         dormant, which is intended - it exists to bind when the stop widens.

      3. Hard per-stock cap - max_position_per_stock_usd, unchanged, now a
         backstop rather than the sizing rule.

    Share COUNT is deliberately not a constraint. 780 shares of a $12 stock
    and 40 shares of a $240 stock are the same $10,000 of risk; only the
    dollar figure means anything, and dividing by price is what converts
    the dollar budget into shares.

    Returns 0 if equity isn't known yet (before the first snapshot refresh),
    so entries fail closed rather than sizing off a zero/stale account.
    """
    trading = config["trading"]
    if price <= 0:
        return 0

    equity = executor.equity
    if equity <= 0:
        return 0

    budgets = []

    hard_cap = trading.get("max_position_per_stock_usd")
    if hard_cap:
        budgets.append(float(hard_cap))

    exposure_fraction = trading.get("max_total_exposure_fraction")
    max_positions = trading.get("max_concurrent_positions")
    if exposure_fraction and max_positions:
        budgets.append(equity * exposure_fraction / max_positions)

    # VOLATILITY-ADJUSTED SIZING.
    #
    # This ceiling is "the largest position whose stop costs at most
    # max_risk_per_trade_fraction of equity", so it is only correct if it uses
    # the stop this position will ACTUALLY get. Since dynamic stops went in,
    # that is a per-symbol number: a 0.4%-ATR name gets a -0.40% stop and a
    # 3%-ATR name gets the -1.0% cap. Reading the static
    # final_exit_loss_pct here would size both identically while stopping them
    # at different distances - which means DIFFERENT DOLLAR RISK PER TRADE, the
    # exact inconsistency dynamic stops introduced.
    #
    # Dividing by the symbol's own stop fixes it in the right direction: a
    # quiet name with a tight stop can carry a LARGER position for the same
    # dollars at risk, and a volatile name with a wide stop carries a smaller
    # one. Every trade then risks the same amount, which is what makes trades
    # comparable to each other at all - and makes P&L attributable to selection
    # rather than to which volatile name happened to be on the list that day.
    risk_fraction = trading.get("max_risk_per_trade_fraction")
    stop_pct = abs(_effective_stop_pct(config, symbol, trading)) / 100
    if risk_fraction and stop_pct > 0:
        budgets.append(equity * risk_fraction / stop_pct)

    if not budgets:
        return 0

    # Set by run_trading_day once per poll (default 1.0 = no change) when
    # trading.regime_sizing is enabled - see _regime_multiplier. Applied here,
    # after the three ceilings, so it composes with EVERY caller's own
    # size_multiplier (burst throttle, the opening-burst mode's 0.5x) rather
    # than needing to be threaded into each one individually.
    regime_mult = getattr(executor, "regime_size_multiplier", 1.0)

    # How deep the day's loss already is. Multiplies with the regime scalar
    # rather than replacing it: a choppy tape and a half-spent day are two
    # independent reasons to be smaller. See Executor.loss_tier_multiplier.
    try:
        regime_mult *= executor.loss_tier_multiplier()
    except Exception as e:
        logger.debug(f"loss tier scaling skipped ({e})")

    # VOLATILITY-ADJUSTED SIZING.
    #
    # Applied as a multiplier rather than through the risk-budget ceiling
    # above, because that ceiling is structurally unable to do this job. It
    # sizes to "the position whose stop costs at most
    # max_risk_per_trade_fraction of equity", and since dynamic stops are
    # CAPPED at final_exit_loss_pct they can only ever be TIGHTER - so the risk
    # budget can only ever come out LARGER, and the even-slot-share ceiling
    # ($9,000 against a $50,000+ risk budget at current settings) wins every
    # time. Routing volatility through it would have changed nothing at all.
    #
    # So scale the slot share directly, and only DOWNWARD. A 3%-ATR name and a
    # 0.9%-ATR name were getting identical dollar exposure while moving three
    # times as far, which is three times the dollar swing on the same bet.
    # Halving the volatile one equalises that. Scaling quiet names UP is
    # deliberately not done: the slot share exists so a full book lands exactly
    # at max_total_exposure_fraction, and exceeding it for one name would break
    # that guarantee for every other position.
    shares = int(min(budgets) / price * regime_mult * _volatility_multiplier(config, symbol))

    # LIQUIDITY CAP, applied last and only ever downward. Every ceiling above
    # is about the ACCOUNT - how much of it to risk here. This one is about the
    # MARKET - how much it can absorb without the fill reflecting the order.
    # They are different questions and the smaller answer has to win.
    cap = _liquidity_cap_shares(config, symbol, price, volume_history)
    if cap is not None and cap < shares:
        logger.info(
            f"{symbol}: liquidity cap {shares} -> {cap} shares "
            f"(${cap * price:,.0f}) - the account could carry more, this "
            f"minute's volume could not absorb it"
        )
        shares = cap
    return shares

def _liquidity_cap_shares(config, symbol, price, volume_history):
    """
    Max shares this symbol can absorb right now, or None for no opinion.

    THE GAP THIS FILLS. The screener already filters on
    universe_min_dollar_volume (3M) and min_avg_volume (1M) - both DAILY
    averages. Neither says anything about the minute you are actually buying
    in, and that is the minute that sets your fill.

    A name averaging $3M a day trades roughly $12k in an average minute, and
    far less than that in a quiet one. A $9,000 slot share into a symbol
    printing $40k that minute is 22% of it; into one printing $12k it is
    most of the minute's volume, and the fill reflects that. This is the
    mechanism behind the entry slippage now being recorded - OLLI filled
    +0.91% past its signal price on 2026-09-02 - so it is a measured cost
    rather than a theoretical one.

    Uses the volume history already maintained per symbol for RVOL, so it
    costs no API calls. Returns None (no opinion) rather than 0 whenever the
    evidence is thin - refusing an entry on a missing measurement would make
    a data gap look like an illiquid stock.
    """
    cfg = (config.get("trading") or {}).get("liquidity_cap") or {}
    if not cfg.get("enabled") or not price:
        return None
    try:
        vols = [float(v) for v in (volume_history or {}).get(symbol, []) if v and float(v) > 0]
        if len(vols) < int(cfg.get("min_samples", 3)):
            return None
        # MEDIAN, not mean. One opening-minute spike is exactly the kind of
        # print that would license a position the next twenty minutes cannot
        # support, and the mean is dominated by it.
        vols_sorted = sorted(vols)
        median_vol = vols_sorted[len(vols_sorted) // 2]
        minute_dollars = median_vol * float(price)
        if minute_dollars <= 0:
            return None
        max_dollars = minute_dollars * float(cfg.get("max_fraction_of_minute", 0.02))
        floor_dollars = float(cfg.get("floor_usd", 1000))
        return int(max(floor_dollars, max_dollars) / float(price))
    except (TypeError, ValueError, ZeroDivisionError) as e:
        logger.debug(f"liquidity cap skipped for {symbol}: {e}")
        return None


def _volatility_multiplier(config, symbol):
    """
    Size scalar in (floor, 1.0] from the symbol's own ATR, or 1.0 when unknown.

    reference_atr_pct is the ATR that gets FULL size. Anything more volatile is
    scaled down in proportion, floored so a single wild name is reduced rather
    than refused. Anything quieter also gets full size and no more - see the
    note in _position_size on why this never scales up.
    """
    cfg = (config.get("trading") or {}).get("volatility_sizing") or {}
    if not cfg.get("enabled") or not symbol:
        return 1.0
    engine = _DYNAMIC_STOPS.get("engine")
    if engine is None:
        return 1.0
    try:
        atr = float((engine.atr_by_symbol or {}).get(symbol) or 0)
        ref = float(cfg.get("reference_atr_pct", 1.5))
        if atr <= 0 or ref <= 0:
            return 1.0
        mult = min(1.0, ref / atr)
        floor = float(cfg.get("min_multiplier", 0.35))
        mult = max(floor, mult)
        if mult < 1.0:
            logger.info(
                f"{symbol}: volatility sizing {mult:.2f}x - ATR {atr:.2f}% against "
                f"a {ref:g}% reference (it travels further, so the same dollar "
                f"exposure is a larger dollar swing)"
            )
        return mult
    except Exception as e:
        logger.debug(f"volatility sizing skipped for {symbol}: {e}")
        return 1.0


def _summarise_burst_notes(config, notes):
    """
    One precise sentence describing what burst logic actually ran today, for
    the daily report. Reports the settings AND how often the throttle really
    engaged, so a day where it never triggered is distinguishable from a day
    where it shaped every entry.
    """
    trading = config["trading"]
    if not trading.get("use_burst_throttle", False):
        return "Burst throttle disabled - every qualifying signal sized normally."
    threshold = trading.get("burst_width_threshold", 5)
    max_entries = trading.get("burst_max_entries", 3)
    multiplier = trading.get("burst_size_multiplier", 0.5)
    throttled = sum(1 for n in notes if n and n.startswith("THROTTLED"))
    polls = len(notes)
    return (
        f"Burst throttle ON (>= {threshold} simultaneous signals -> take at most "
        f"{max_entries} at {multiplier:g}x size). Engaged on {throttled} of {polls} "
        f"entry-window polls."
    )


def _window_pct_change(history):
    """% change from the oldest to newest sample in a lookback deque, or None."""
    if not history or len(history) < 2:
        return None
    first, last = history[0][1], history[-1][1]
    if not first:
        return None
    return round((last - first) / first * 100, 3)


def _spread_pct(market_data, symbol, price):
    """
    Bid-ask spread as a % of price, for the signal journal only.

    This is the quantity min_stock_price only approximates: a one-cent spread
    is a fixed dollar cost, so as a percentage it explodes at low prices -
    PLUG at $2.19 quotes ~0.45%, against a first exit that triggers at -0.5%.
    Measuring it directly is what would eventually allow keeping a cheap stock
    that happens to be tight while rejecting an expensive one that is wide.

    Fully guarded: returns None on any failure. Journal-only everywhere it is
    called from the normal entry path (after the decision, never gates it) -
    the one exception is _run_opening_burst's spread gate, which reads this
    BEFORE deciding, because that mode's own min_move_pct is tight enough for
    a wide spread to fake a real move (see that gate's comment for the
    HOOD example this is built from).
    """
    try:
        quote = market_data.broker.get_latest_quote(symbol)
        if not quote or not price:
            return None
        return round(quote["spread"] / price * 100, 4)
    except Exception:
        return None


# symbol -> recent PLAUSIBLE spread readings, for _usable_spread_pct.
_SPREAD_SAMPLES = {}


def _usable_spread_pct(config, market_data, symbol, price, now=None):
    """
    A spread reading fit to make a decision on, or None meaning "unknown".

    _spread_pct above returns whatever one REST call to IEX says. That is fine
    for a journal column and wrong for a gate, and 2026-09-02 is the proof:
    the opening burst refused EVERY candidate it measured, all of them on the
    spread gate, using numbers that cannot be real. WDAY at ~$230 quoted an
    "11.2% spread" - a $26-wide market. AI read 0.290%, then 15.703%, then
    16.058%, then 0.29% again, all within ninety seconds. The gate was working
    perfectly on garbage.

    The cause is what IEX is. It carries ~2% of US volume, so at 09:31 its own
    book for a name is often one stale side and one fresh side - a pre-market
    bid against a live ask. The DIFFERENCE between those is not a spread
    anybody could trade against; it is an artifact of a thin venue.

    Two defences:

      1. A plausibility ceiling. Above max_plausible_spread_pct the reading is
         discarded as a bad quote and this returns None. None means UNKNOWN,
         and callers must treat unknown as "no information" - NOT as a
         refusal. Refusing on an unreadable quote is what cost the burst five
         of its seven entries.
      2. A median of recent plausible readings. One bad tick cannot move a
         median, and the samples are per-symbol and per-session.

    Returns None when there is no usable reading. Never raises.
    """
    raw = _spread_pct(market_data, symbol, price)
    cfg = config.get("trading") or {}
    ceiling = cfg.get("max_plausible_spread_pct", 2.0)

    if raw is not None and (ceiling and raw > ceiling):
        logger.debug(
            f"{symbol}: spread reading {raw:.3f}% discarded as implausible "
            f"(above max_plausible_spread_pct {ceiling}%) - treating as unknown"
        )
        raw = None

    samples = _SPREAD_SAMPLES.setdefault(symbol, deque(maxlen=5))
    if raw is not None:
        samples.append(raw)
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def _burst_policy(config, burst_width, spy_pct=None):
    """
    Decide how many of this poll's simultaneous signals to act on, and at what
    size. Returns (max_entries, size_multiplier, description).

    When many symbols fire in the same poll they are almost never independent
    ideas - they are one market move showing up in many tickers at once. On
    2026-08-19 twenty names triggered inside nine seconds; the book was not
    twenty bets, it was one bet held twenty times, and when the move went the
    wrong way every position lost together (1 winner out of 23, -$1,307).

    Note what this does and does not fix. Taking the first N of a burst is NOT
    diversification - it is the same bet held N times instead of twenty, which
    reduces the size of the loss without changing its nature. Ranking (buying
    the "best" of a burst) is a separate improvement and needs the evidence
    the signal journal is being built to collect; this only controls how much
    is committed to a single market move.

    Below burst_width_threshold nothing changes: a couple of signals in one
    poll is normal, uncorrelated behavior and gets full size.
    """
    trading = config["trading"]
    if not trading.get("use_burst_throttle", False):
        return None, 1.0, f"disabled (burst={burst_width})"

    # The market's own move is a better burst detector than counting signals.
    # If SPY has lurched, you already know WHY twenty names fired - you do not
    # need to count them, and the count is only a proxy for this anyway. It is
    # free: SPY is sampled every poll for excess-return already.
    #
    # It also catches the case counting misses: three names firing during a
    # sharp market move are just as correlated as twenty, and burst width alone
    # would wave them through at full size.
    market_move = trading.get("market_burst_spy_pct", 0)
    market_event = bool(
        market_move and spy_pct is not None and abs(spy_pct) >= market_move
    )

    threshold = trading.get("burst_width_threshold", 5)
    if burst_width < threshold and not market_event:
        return None, 1.0, f"normal: burst={burst_width} < threshold {threshold}, full size"

    if market_event and burst_width < threshold:
        return (
            trading.get("burst_max_entries", 3),
            trading.get("burst_size_multiplier", 0.5),
            f"MARKET MOVE: SPY {spy_pct:+.2f}% >= {market_move}% - these are one bet, "
            f"not {burst_width} (burst width alone would have allowed full size)",
        )

    max_entries = trading.get("burst_max_entries", 3)
    size_multiplier = trading.get("burst_size_multiplier", 0.5)
    return (
        max_entries,
        size_multiplier,
        f"THROTTLED: burst={burst_width} >= {threshold}"
        + (f" AND SPY {spy_pct:+.2f}%" if market_event else "")
        + f", took <= {max_entries} at {size_multiplier:g}x size",
    )


def _compute_rvol(bar, volume_history):
    """
    This bar's volume against the average of this symbol's own recent bars.
    Returns None until there is enough history to compare against. Journal
    only - never gates an entry.
    """
    try:
        vol = float(bar.get("volume") or 0)
        prior = [v for v in volume_history if v > 0]
        if vol <= 0 or len(prior) < 3:
            return None
        avg = sum(prior) / len(prior)
        return round(vol / avg, 3) if avg > 0 else None
    except Exception:
        return None


OPENING_METHOD = "OPENING_MOVE"


def _opening_burst_config(config):
    """The opening_burst block, or None when it is off."""
    ob = (config.get("trading") or {}).get("opening_burst") or {}
    return ob if ob.get("enabled") else None


def _opening_exit_config(config):
    """
    A full config whose trading section carries the opening mode's exit
    overrides, or None when it declares none.

    Built by COPYING the live config and overlaying only the keys under
    opening_burst.exits, so anything not overridden behaves exactly as the
    normal session does. That direction matters: an exit profile written from
    scratch would silently drop every setting nobody remembered to restate -
    resistance, momentum fade, the time stop - and those omissions would look
    like strategy decisions rather than oversights.
    """
    ob = _opening_burst_config(config)
    if not ob:
        return None
    overrides = ob.get("exits") or {}
    if not overrides:
        return None
    import copy as _copy
    out = _copy.deepcopy(config)
    out["trading"].update(overrides)
    return out


def _burst_rank_multifactor(config, measured, market_data, spy_pct, price_history,
                            volume_history, vwap_acc, sector_returns):
    """
    Re-order the opening burst's qualifying candidates by continuation score
    instead of raw move size. Returns (ordered_measured, note).

    PENDING_WORK backlog item 4. The burst has always sorted by % move alone;
    this feeds the same cf_score machinery the normal entry path uses.

    WHAT IS AND IS NOT MEASURABLE AT 09:31. This runs 1-3 minutes after the
    bell, and several continuation factors are structurally blind that early:
    opening_range_minutes is 5, so there is no opening high to break out of;
    VWAP has about a minute of prints behind it; sector returns need a window
    the session has not accumulated. Those come back None, and
    continuation_score already renormalises over missing factors rather than
    scoring them zero - "not measurable" is not "bad".

    So this is honest about being a PARTIAL score early on: it ranks on
    whatever is genuinely computable (efficiency, volume acceleration, rvol,
    spread, and relative strength once SPY has a window), and falls back to
    move-order when nothing scores. Ranking, never gating - it changes WHICH
    of the qualifying candidates get the budget, never how many, which is the
    same weaker-claim argument _rank_burst was built on.
    """
    if not config["trading"].get("use_continuation_score", False):
        return measured, None
    if not (config["trading"].get("opening_burst") or {}).get("multifactor_rank", False):
        return measured, None
    if len(measured) < 2:
        return measured, None

    scored = []
    for move, symbol, price, base in measured:
        cont = {}
        try:
            cont = _continuation_fields(
                config, symbol, price, move, spy_pct,
                [p for _, p in (price_history or {}).get(symbol, [])],
                list((volume_history or {}).get(symbol, [])),
                _vwap(vwap_acc, symbol) if vwap_acc is not None else None,
                # No opening_high: opening_range_minutes is 5 and this mode
                # decides by 09:33, so the opening range does not exist yet.
                # Omitted rather than passed as None-from-a-local that only
                # exists inside run_trading_day - cf_breakout scores None and
                # continuation_score renormalises over it.
                dict(screener_details.get(symbol) or {}),
                rvol=None,
                spread_pct=_spread_pct(market_data, symbol, price),
                sector_returns=sector_returns,
            )
        except Exception as e:
            logger.debug(f"burst cf_score unavailable for {symbol}: {e}")
        scored.append(((move, symbol, price, base), cont.get("cf_score")))

    # A score being NON-NULL is not the same as it being INFORMATIVE, and
    # this is where that distinction bites. At 09:31 continuation_score
    # renormalises over whatever factors exist, so a candidate with one
    # minute of history still returns a number - computed from almost
    # nothing. Trusting it reorders the burst on noise: the first live-shaped
    # test of this ranked a +0.2% mover ABOVE a +3.0% one, which is the exact
    # inversion of the only factor with real evidence behind it.
    #
    # So require a MAJORITY of candidates to have produced a score before
    # letting it reorder anything. Below that, move order stands - it is the
    # better-evidenced ranking and the one this mode has always used.
    have = [s for _, s in scored if s is not None]
    need = max(2, int(len(scored) * float(
        (config["trading"].get("opening_burst") or {}).get("min_scored_fraction", 0.5))))
    if len(have) < need:
        return measured, (
            f"multi-factor rank SKIPPED - only {len(have)}/{len(scored)} candidates "
            f"produced a continuation score (need {need}); keeping move order"
        )

    # Unscored candidates sort last but are NOT dropped, matching _rank_burst.
    scored.sort(key=lambda kv: (0, 0.0) if kv[1] is None else (1, kv[1]), reverse=True)
    ordered = [row for row, _ in scored]
    shown = ", ".join(
        f"{row[1]}={score:.0f}" if score is not None else f"{row[1]}=n/a"
        for row, score in scored
    )
    return ordered, f"burst ranked by continuation score: {shown}"


def _run_opening_burst(config, market_data, strategy, executor, symbols, rsi_values,
                       state, now, et, signal_journal=None, spy_pct=None,
                       spy_history=None,
                       price_history=None, volume_history=None, vwap_acc=None,
                       sector_returns=None, qqq_history=None, breadth_state=None,
                       regime_state=None):
    """
    The opening-move experiment: measure each streamed symbol from the 09:30
    baseline and buy the ones that are up, deciding by 09:32.

    Separate from the normal entry path on purpose. Widening entry_window_start
    to 09:30 instead would run the standard 0.3%/3min rule at the open, where
    nearly every high-beta name clears it simultaneously - max_concurrent_positions
    fills in seconds and the rest of the session has no capacity. This mode
    carries its own budget so both can run on the same day.

    `state` persists across polls: {"baseline": {sym: price}, "taken": [...],
    "done": bool, "skipped": {...}}.
    """
    ob = _opening_burst_config(config)
    if not ob or state.get("done"):
        return 0

    baseline_at = parse_hhmm_today(ob.get("baseline_time", "09:30"), et)
    decide_by = parse_hhmm_today(ob.get("decide_by", "09:32"), et)

    if now < baseline_at:
        return 0
    if now >= decide_by:
        if not state.get("done"):
            state["done"] = True
            # Journal every symbol that was MEASURED but not taken, once, with
            # the move it finished the window on. These refused symbols are the
            # control group: without them "buy what went up at 09:31" is an
            # untestable claim, because nothing records what the ones you passed
            # on went on to do. Written at window close rather than per poll so
            # each symbol contributes one row instead of twelve.
            if signal_journal is not None:
                for sym, base in (state.get("baseline") or {}).items():
                    if sym in state.get("taken", []):
                        continue
                    last = state.get("last_price", {}).get(sym)
                    move = ((last - base) / base * 100) if (last and base) else None
                    try:
                        signal_journal.record(
                            symbol=sym, entry_method=OPENING_METHOD, price=last,
                            signal_pct=round(move, 3) if move is not None else None,
                            spy_pct=spy_pct, taken=False,
                            skip_reason="opening_burst_not_taken",
                            size_multiplier=ob.get("size_multiplier", 0.5),
                        )
                    except Exception:
                        pass
            # The move DISTRIBUTION, not just the count. "0 entered" on its own
            # is ambiguous between a threshold set too high and a mechanism that
            # never ran, and those need opposite fixes. Printing what every
            # measured symbol actually did says which it was, and says what
            # threshold would have caught what - so the next setting is read off
            # data rather than guessed at.
            moves = []
            for sym, base in (state.get("baseline") or {}).items():
                last = state.get("last_price", {}).get(sym)
                if last and base:
                    moves.append((round((last - base) / base * 100, 3), sym))
            moves.sort(reverse=True)
            thresh = ob.get("min_move_pct", 0.0)
            logger.info(
                f"===== OPENING BURST CLOSED at {now:%H:%M:%S} ET - "
                f"{len(state.get('taken', []))} entered, "
                f"{len(state.get('baseline', {}))} symbols measured ====="
            )

            # ---- EARLY REGIME READ, at the burst close ---------------------
            #
            # regime_sizing latches at check_time. That is the right place for
            # the AUTHORITATIVE read - a few more minutes of tape is a better
            # read - but it leaves the busiest stretch of the session
            # ungoverned. On 2026-09-02 the account hit the daily loss limit at
            # 09:38:19, before the 09:45 check had run at all: nine longs were
            # opened between 09:31 and 09:38 into a tape that had already been
            # flagged "QQQ is not trending up" at 09:10, and the feature built
            # to prevent exactly that never got a turn.
            #
            # So take a PROVISIONAL read here, at the moment the burst closes
            # and the normal entry window opens. Deliberately narrower than the
            # real check: it only ever acts on the unambiguous case (BOTH
            # indices below their own VWAP), it never scales UP, and the
            # check_time read replaces it wholesale when it arrives. A weaker
            # signal from less data is allowed to say "not now"; it is not
            # allowed to say "full size".
            try:
                rc = (config.get("trading") or {}).get("regime_sizing") or {}
                if (rc.get("enabled") and rc.get("burst_close_gate", True)
                        and regime_state is not None and "multiplier" not in regime_state):
                    _spy_v = _vs_vwap(vwap_acc, spy_history, "SPY")
                    _qqq_v = _vs_vwap(vwap_acc, qqq_history, "QQQ")
                    if _spy_v is not None and _qqq_v is not None:
                        if _spy_v <= 0 and _qqq_v <= 0:
                            _m = rc.get("bearish_multiplier", 0.0)
                            executor.regime_size_multiplier = _m
                            regime_state["provisional"] = _m
                            logger.warning(
                                f"EARLY REGIME (burst close, provisional): BEARISH - "
                                f"SPY {_spy_v:+.3f}% and QQQ {_qqq_v:+.3f}% are BOTH below "
                                f"their own VWAP. New-entry size multiplier set to {_m}x "
                                f"until the {rc.get('check_time', '09:40')} check confirms or "
                                f"replaces it."
                            )
                        else:
                            logger.info(
                                f"EARLY REGIME (burst close, provisional): not bearish - "
                                f"SPY {_spy_v:+.3f}%, QQQ {_qqq_v:+.3f}% vs VWAP. No early "
                                f"restriction; the {rc.get('check_time', '09:40')} check decides."
                            )
                    else:
                        logger.info(
                            "EARLY REGIME (burst close): skipped - SPY/QQQ VWAP not "
                            "available yet. The check_time read still applies."
                        )
            except Exception as e:
                logger.debug(f"early regime read skipped: {e}")
            if moves:
                logger.info(
                    "OPENING MOVES (best first): "
                    + ", ".join(f"{sym} {mv:+.3f}%" for mv, sym in moves[:20])
                )
                # Hand the normal entry window what the burst learned. Without
                # this the two modes look at the same symbols with no shared
                # memory: on 2026-09-02 the burst measured RBLX +1.575%,
                # WDAY +1.344% and CRM +1.105%, refused all three, and the
                # normal window then bought WDAY and CRM 60-90 seconds later
                # off a 3-minute RAPID_INCREASE - chasing the tail of the very
                # move the burst had already measured and declined, but with
                # the session's loose -1.0% stop instead of the burst's -0.35%.
                # CRM and WDAY were 2 of the day's 3 largest losses.
                state["closed_moves"] = {sym: mv for mv, sym in moves}
                state["refused"] = sorted(
                    sym for mv, sym in moves
                    if mv >= thresh and sym not in (state.get("taken") or [])
                )
                if state["refused"]:
                    logger.info(
                        f"BURST REFUSED (cleared {thresh}% but not taken): "
                        + ", ".join(state["refused"])
                        + " - suppressed in the normal window until "
                        + str((config["trading"].get("opening_burst") or {}).get(
                            "refused_suppressed_until", "09:45"))
                    )

                qualified = sum(1 for mv, _ in moves if mv >= thresh)
                logger.info(
                    f"OPENING THRESHOLD REVIEW: {qualified} of {len(moves)} cleared "
                    f"{thresh}%. Best {moves[0][0]:+.3f}% ({moves[0][1]}), "
                    f"median {moves[len(moves) // 2][0]:+.3f}%, "
                    f"worst {moves[-1][0]:+.3f}% ({moves[-1][1]})."
                )
                if not state.get("taken"):
                    logger.warning(
                        f"OPENING BURST TOOK NOTHING. The mechanism ran and measured "
                        f"{len(moves)} symbols, so this is a THRESHOLD result, not a "
                        f"failure: the best move was {moves[0][0]:+.3f}% against a "
                        f"{thresh}% requirement. Lower min_move_pct to about "
                        f"{max(0.1, round(moves[0][0] - 0.05, 2))} to have caught the best one."
                    )
            elif not state.get("baseline"):
                logger.error(
                    "OPENING BURST MEASURED NOTHING - no symbol produced a baseline "
                    "price. This is NOT a threshold result: either the stream was not "
                    "up at the baseline instant, or every symbol fell back to REST and "
                    "was skipped by streamed_only. Check for the PRE-OPEN subscribe line."
                )
        return 0

    # Resolved once per session, not per poll.
    if "exit_config" not in state:
        state["exit_config"] = _opening_exit_config(config)

    streamed_only = ob.get("streamed_only", True)
    baseline = state.setdefault("baseline", {})

    # How many symbols the stream is actually serving, logged once as the
    # window opens. If this is 0 the experiment cannot work, and knowing that at
    # 09:30:10 is worth far more than deducing it from an empty report later.
    # Logged once as the window opens, then every 30s until it closes.
    #
    # One reading told us 3/27 on 2026-08-28 and 0/28 on 2026-08-27, which says
    # the mode cannot work but not what to DO about it. The missing number is
    # the RAMP: IEX carries ~2% of US volume, so symbols appear as they happen
    # to print there, and how fast that count climbs is what decides where
    # decide_by belongs. If it reaches 12/27 by 09:31 the window can come back
    # in; if it is still 4/27 at 09:34 then no amount of waiting fixes it and
    # the honest answer is the paid SIP feed.
    #
    # Costs one is_streamed() sweep per 30s over a list of ~27 - nothing.
    last_ready = state.get("readiness_at")
    if last_ready is None or (now - last_ready).total_seconds() >= 30:
        first = last_ready is None
        state["readiness_at"] = now
        try:
            live = sum(1 for s_ in symbols if market_data.is_streamed(s_))
            based = len(state.get("baseline", {}))
            msg = (f"OPENING BURST: stream is serving {live}/{len(symbols)} watched "
                   f"symbols at {now:%H:%M:%S} ET ({based} with a baseline)")
            if live == 0:
                logger.error(msg + " - NOTHING can be measured; the mode will take no trades")
            elif first and live < len(symbols) / 2:
                logger.warning(msg + " - most symbols are on REST and will be skipped")
            else:
                logger.info(msg)
        except Exception:
            pass
    taken = state.setdefault("taken", [])
    max_positions = ob.get("max_positions", 4)
    entries = 0

    # PASS 1 - measure everything, decide nothing.
    #
    # The move has to be known for every symbol before any of them is bought,
    # because the budget is 7 and more than 7 can qualify. Taking them in
    # watchlist order would fill those slots by an accident of sorting - the
    # same flaw the burst throttle had before _rank_burst, arriving here through
    # a different door.
    measured = []
    for symbol in symbols:
        try:
            if streamed_only and not market_data.is_streamed(symbol):
                continue
            bar = market_data.get_latest_bar(symbol, "1Min")
            if not bar:
                continue
            price = market_data.get_entry_price(symbol, bar)
            if not price:
                continue
            if symbol not in baseline:
                # Seed from the BAR'S OWN OPEN when the bar carries one.
                #
                # Taking the baseline from the first price this loop happens to
                # observe costs a whole poll before anything can be measured -
                # the first poll sets the baseline and `continue`s, so the
                # earliest possible measurement is one interval later. Worse,
                # it makes the baseline an accident of when the stream came up:
                # on 2026-09-02 the socket was killed at 09:30:01 and the first
                # observation was 09:31:07, so "the move from the open" was
                # really "the move since 09:31:07" and the first minute of the
                # session was invisible.
                #
                # The streamed 1-minute bar already carries the OPEN of its
                # minute. At the first poll after the bell that is the 09:30
                # bar, whose open is the opening print itself - the correct
                # baseline, available immediately, with no extra API call. A
                # symbol can therefore be measured on the FIRST poll it appears
                # in rather than the second.
                seed = None
                try:
                    if isinstance(bar, dict):
                        seed = bar.get("open")
                    else:
                        seed = getattr(bar, "open", None)
                    seed = float(seed) if seed else None
                except (TypeError, ValueError):
                    seed = None
                baseline[symbol] = seed or price
                if not seed:
                    # No open on the bar - fall back to the old behaviour and
                    # spend the poll establishing the baseline.
                    continue
            state.setdefault("last_price", {})[symbol] = price
            base = baseline[symbol]
            measured.append((
                ((price - base) / base * 100) if base else 0.0, symbol, price, base,
            ))
        except Exception as e:
            logger.error(f"Opening burst measurement failed for {symbol}: {e}")
            continue

    # PASS 2 - act, biggest move first.
    #
    # Ranking is WITHIN a poll, not across the whole window. Waiting until 09:32
    # to pick the best would select better and enter two minutes later, which on
    # a momentum trade gives back most of what it was selecting for. So a strong
    # early mover can still take a slot a stronger later one would have wanted -
    # that is the deliberate trade: an early fill in a real move beats a perfect
    # fill in a spent one.
    measured.sort(reverse=True)
    if measured:
        logger.debug(
            "OPENING BURST poll: "
            + ", ".join(f"{sym} {mv:+.2f}%" for mv, sym, _, _ in measured[:8])
        )

    # %MOVE AS A CANDIDATE FILTER, THEN RANK THE SURVIVORS.
    #
    # The threshold is applied HERE, before any re-ranking, rather than by
    # the `break` that used to sit in the loop below. That break relied on
    # the list being move-sorted - true when move was the only ordering, and
    # false the moment a continuation score reorders it, at which point it
    # would stop at the first sub-threshold row and silently skip qualifying
    # candidates ranked after it.
    _floor = ob.get("min_move_pct", 0.0)
    measured = [row for row in measured if row[0] >= _floor]

    # Multi-factor re-rank, if enabled. With the flag off (or nothing
    # scoreable this early) the order stays exactly the move-ranked one this
    # mode has always used.
    measured, _mf_note = _burst_rank_multifactor(
        config, measured, market_data, spy_pct, price_history,
        volume_history, vwap_acc, sector_returns,
    )
    if _mf_note:
        logger.info(f"OPENING BURST {_mf_note}")

    for move, symbol, price, base in measured:
        try:
            if symbol in taken or symbol in strategy.get_open_trades():
                continue
            if len(taken) >= max_positions:
                break          # budget spent; the rest are ranked below these
            # min_move_pct is applied above, before ranking - see the
            # candidate-filter comment. Nothing reaching this loop is below it.

            # Multi-factor gate, added 2026-09-02: refuse a move that is not
            # comfortably bigger than the symbol's OWN spread. min_move_pct
            # alone is a single fixed floor for every symbol, but the config
            # for this mode already documents the failure this misses - HOOD
            # quoted a 0.593% median spread on 2026-08-26, WIDER than the
            # 0.3-0.5% thresholds tried here, so "+0.3% from the open" can be
            # one print crossing the spread rather than a real move. Ratio,
            # not a flat spread cap, because the risk scales with the move
            # size being chased, not with spread alone. min_move_pct still
            # does the primary filtering (this loop is sorted by move and
            # breaks above); this only removes candidates whose move the
            # spread itself could have produced. rel-strength-vs-SPY and
            # volume were considered and left out for now - vwap/exhaustion
            # need a window this mode does not have by 09:32-09:33, spy_pct
            # is one value per poll so it cannot reorder candidates within a
            # poll, and volume history is not threaded into this function.
            # See PENDING_WORK.md item 4 for the fuller cf_score version.
            # ONE quote per candidate, not two. The spread gate and the
            # context row both want this number, and both used to fetch it -
            # doubling the broker quote calls inside the 09:30-09:33 window,
            # which is the most latency-sensitive three minutes of the day and
            # the one where the burst is racing a decide_by deadline.
            spread_pct = _usable_spread_pct(config, market_data, symbol, price, now)

            ratio = ob.get("min_move_to_spread_ratio", 0)
            if ratio:
                # spread_pct is None when no PLAUSIBLE quote exists. Unknown is
                # not the same as wide, and conflating them is what emptied
                # this mode on 2026-09-02 - 12 symbols measured, 7 clearing the
                # move threshold, and every refusal in the whole window was
                # this gate firing on an unreadable IEX quote. An unknown
                # spread now lets the move decide, which is the evidence this
                # mode has always been built on.
                if spread_pct is None:
                    logger.debug(
                        f"{symbol}: spread unknown - the gate abstains, "
                        f"the +{move:.3f}% move decides"
                    )
                elif move < spread_pct * ratio:
                    logger.info(
                        f"{symbol}: opening move +{move:.3f}% refused - spread "
                        f"gate ({spread_pct:.3f}% spread x {ratio:g} = "
                        f"{spread_pct * ratio:.3f}% required)"
                    )
                    continue

            # The signal ceiling, only if this mode is told to honour it.
            # rapid_increase_max_pct exists to refuse a move that has already
            # spent itself over a 3-minute window; this mode is explicitly
            # buying the strongest openers, so by default it does not apply and
            # ignore_max_pct ships true. The flag is READ rather than merely
            # documented - a config switch that nothing consults is worse than
            # no switch, which use_continuation_score demonstrated by sitting
            # inert for five sessions while appearing to gate entries.
            if not ob.get("ignore_max_pct", True):
                ceiling = config["trading"].get("rapid_increase_max_pct") or 0
                if ceiling and move > ceiling:
                    logger.info(
                        f"{symbol}: opening move +{move:.3f}% refused - above "
                        f"rapid_increase_max_pct {ceiling}%"
                    )
                    continue

            note = (f"OPENING_BURST: +{move:.3f}% from the {ob.get('baseline_time','09:30')} "
                    f"baseline {base:.4f}, decided {now:%H:%M:%S}")
            # Market/stock state at the entry instant, same row shape the
            # normal path records. Without this the burst's trades - which
            # take up to 7 of the 10 position slots - land in
            # trade_context.csv with an empty MARKET and STOCK block, and the
            # replay's whole point (filter on "SPY above VWAP AND rvol > 3x")
            # cannot be applied to the majority of the day's trades.
            #
            # Several fields are legitimately blank this early (there is no
            # opening range yet, VWAP has a minute of prints). Blank is the
            # honest record of that; a zero would be a false reading.
            burst_ctx = _entry_context(
                {"signal_pct": round(move, 3),
                 "spread_pct": spread_pct,
                 "cont": {}},
                spy_pct,
                _window_pct_change(qqq_history) if qqq_history else None,
                _vs_vwap(vwap_acc, [(0, _last_price(spy_history))], "SPY")
                if spy_history_has(spy_history) else None,
                _vs_vwap(vwap_acc, [(0, _last_price(qqq_history))], "QQQ")
                if spy_history_has(qqq_history) else None,
                breadth_state, regime_state,
                _pct_vs(price, _vwap(vwap_acc or {}, symbol)),
                ob.get("size_multiplier", 0.5),
            )
            entered = _attempt_entry(
                config, strategy, executor, symbol, price, OPENING_METHOD,
                rsi_values.get(symbol),
                size_multiplier=ob.get("size_multiplier", 0.5),
                burst_note=note, signal_pct=round(move, 3),
                skip_cooldown=ob.get("skip_reentry_cooldown", True),
                exit_config=state.get("exit_config"),
                context=burst_ctx,
                market_data=market_data,
                volume_history=volume_history,
            )
            if entered:
                taken.append(symbol)
                entries += 1
                logger.info(
                    f"{symbol}: OPENING MOVE entry - {move:+.3f}% from the open "
                    f"({base:.2f} -> {price:.2f}), {len(taken)}/{max_positions} used"
                )
            if entered and signal_journal is not None:
                # Only the TAKEN ones here. Refusals are journalled once at
                # window close with their final move, so a symbol that is down
                # at 09:30:10 and up at 09:31:50 is recorded as what it ended
                # the window at rather than twelve times as it wavered.
                signal_journal.record(
                    symbol=symbol, entry_method=OPENING_METHOD, price=price,
                    signal_pct=round(move, 3), spy_pct=spy_pct, taken=True,
                    size_multiplier=ob.get("size_multiplier", 0.5),
                )
        except Exception as e:
            logger.error(f"Opening burst check failed for {symbol}: {e}")
            continue

    return entries


def _entry_context(cand, spy_pct, qqq_pct, spy_vs_vwap, qqq_vs_vwap,
                   breadth_state, regime_state, stock_vs_vwap, size_multiplier):
    """
    The market and stock state AT THE ENTRY INSTANT, for trade_context.csv.

    Captured here rather than reconstructed later because most of it is
    genuinely unrecoverable after the fact: VWAP position, breadth and the
    regime label are all session-state that no post-hoc bar fetch can rebuild
    faithfully. Everything is None-tolerant - a reading that could not be
    taken is recorded as blank, never as zero, since these become filter
    conditions ("SPY above VWAP AND rvol > 3x") where a false zero silently
    moves trades into the wrong bucket.
    """
    cont = (cand or {}).get("cont") or {}
    return {
        "size_multiplier": size_multiplier,
        "spy_return": spy_pct,
        "qqq_return": qqq_pct,
        "spy_vs_vwap": spy_vs_vwap,
        "qqq_vs_vwap": qqq_vs_vwap,
        "market_breadth": (breadth_state or {}).get("mean_move"),
        "regime": (regime_state or {}).get("label"),
        "stock_vs_vwap": stock_vs_vwap,
        "relative_volume": (cand or {}).get("rvol"),
        "momentum": (cand or {}).get("signal_pct"),
        "continuation_score": cont.get("cf_score"),
        "sector_strength": cont.get("cf_sector_strength"),
        "spread_pct": (cand or {}).get("spread_pct"),
    }


def _attempt_entry(config, strategy, executor, symbol, price, entry_method, symbol_rsi,
                   size_multiplier=1.0, burst_note=None, signal_pct=None,
                   skip_cooldown=False, exit_config=None, context=None,
                   market_data=None, volume_history=None):
    """
    Shared entry path for all three entry signals (three-bar momentum, rapid
    increase immediate, pullback resumption). Returns True if a position was
    actually opened.

    Order matters, and is the fix for the 2026-08-18 phantom-entry bug:
      1. strategy.can_enter - pure check, no state mutated.
      2. executor.pre_entry_check - funds/exposure check, no broker call made.
      3. executor.submit_entry_order - the ONLY step that touches the broker.
      4. strategy.confirm_entry - commits to strategy.trades, and ONLY runs
         if step 3 actually succeeded.
    Previously, step 4's equivalent (the old enter_trade()) ran BEFORE step 3,
    so a failed broker order (e.g. insufficient buying power) left a phantom
    position in the bot's memory. When exit logic later fired against that
    phantom long, the resulting sell - for shares never actually bought -
    was accepted by Alpaca's margin account as opening a real, completely
    untracked short position. 16 of them happened this way in one session.
    """
    qty = int(_position_size(config, executor, price, symbol=symbol,
                             volume_history=volume_history) * size_multiplier)
    if qty <= 0:
        # Distinguish "the regime says stand down" from "the arithmetic came
        # out at zero shares". Both refuse the entry, but only one of them is
        # a decision - and reading a deliberate stand-down as a sizing
        # rounding error is how a working risk control gets tuned away.
        if getattr(executor, "regime_size_multiplier", 1.0) == 0:
            logger.info(
                f"{symbol}: entry skipped - regime is bearish (both indices below "
                f"VWAP), no new longs today. This is regime_sizing standing down, "
                f"not a sizing failure."
            )
        else:
            logger.info(f"{symbol}: entry skipped - position size worked out to 0 shares at {price:.2f}")
        return False
    if not strategy.can_enter(symbol, qty):
        return False

    # min_stock_price / max_stock_price were dead config until 2026-08-20 -
    # declared, documented, and read by nothing. stock_screener.score_stock even
    # carries a "# Price check (filter only)" comment above code that records
    # the price and never filters on it. So the whole cheap-stock cohort stayed
    # tradeable: PLUG, BMBL, OPEN, GRAB, NIO, LCID and SOUN all sit in the
    # static stock_universe and were watched every day regardless of the setting.
    #
    # That matters because a one-cent spread is a fixed dollar cost, so as a
    # PERCENT it explodes at low prices while every stop here is a percent. PLUG
    # at $2.19 quotes ~0.45% (measured live), against a first exit that fires at
    # -0.5% - the position is at its stop the moment it fills. Sub-$10 names went
    # 16 trades / 1 winner / -$623 on 2026-08-19, 36% of the day's loss from 23%
    # of its trades.
    #
    # Enforced here rather than by pruning the watchlist at selection time: this
    # is the price actually being paid at the moment of entry, and it costs no
    # extra API calls.
    # Exclusion backstop, third of the three layers (screener pre-ranking,
    # watchlist filter, here). Enforced at the moment of purchase for the same
    # reason the price band is: this is the last point before real money moves,
    # and a symbol can reach here through a path neither earlier layer saw.
    _excluded, _reason = _is_excluded_symbol(config, symbol)
    if _excluded:
        logger.info(f"{symbol}: entry skipped - {_reason}")
        return False

    min_price = config["trading"].get("min_stock_price")
    max_price = config["trading"].get("max_stock_price")
    if min_price and price < min_price:
        logger.info(
            f"{symbol}: entry skipped - price ${price:.2f} below min_stock_price ${min_price}"
        )
        return False
    if max_price and price > max_price:
        logger.info(
            f"{symbol}: entry skipped - price ${price:.2f} above max_stock_price ${max_price}"
        )
        return False

    # skip_cooldown is for the opening-burst mode. An opening trade must not
    # block the normal session from re-entering the same symbol later - that
    # would let the experiment change the control it is being measured against.
    # The phantom cooldown is checked FIRST and is never skippable - see
    # Executor.phantom_cooldown_remaining. skip_cooldown is about not letting
    # an opening trade block the normal session from re-entering a name; it was
    # never meant to permit re-submitting an order the broker just declined.
    # STALE / DEAD FEED -> NO NEW ENTRIES.
    #
    # The bot's default when the stream dies is to fall back to REST and keep
    # trading, which on the free tier means ~15-minute delayed prices. That was
    # a deliberate choice - a degraded session beat no session while the stream
    # was unreliable - but it is the wrong default for entries specifically,
    # and 2026-08-20 measured the cost: losing entries landed at the 87th
    # percentile of the surrounding half-hour, i.e. the bot was systematically
    # buying the top of a move it could not see had already happened.
    #
    # EXITS ARE DELIBERATELY UNAFFECTED. A stale price is a bad reason to open
    # a position and a terrible reason to hold one - refusing to exit on
    # degraded data would strand exactly the positions that most need closing.
    # market_data is optional so every existing caller keeps working; without
    # it there is no stream to judge and the guard abstains rather than
    # guessing. Absence of a stream is not evidence of stale data.
    _fresh = (config["trading"].get("require_fresh_data_for_entry") or {})
    if _fresh.get("enabled") and market_data is not None:
        _stream = getattr(market_data, "stream", None)
        _healthy = False
        try:
            _healthy = _stream is not None and _stream.is_healthy()
        except Exception:
            _healthy = False
        if _stream is not None and not _healthy:
            if not _fresh.get("_warned"):
                _fresh["_warned"] = True
                logger.warning(
                    f"{symbol}: entry refused - the price stream is not serving, so "
                    f"this decision would be made on REST prices the free tier delays "
                    f"by ~15 minutes. Entries are blocked until it recovers; exits and "
                    f"open positions continue as normal."
                )
            return False

    # getattr rather than a direct call: this runs on every entry, and an
    # executor without the method (an older one, a stub) must degrade to "no
    # phantom cooldown" instead of raising into the entry path and killing the
    # trade. This file's history is full of one-thing-fails-everything-stops.
    try:
        phantom_left = getattr(executor, "phantom_cooldown_remaining", lambda _s: 0.0)(symbol)
    except Exception as e:
        logger.debug(f"{symbol}: phantom cooldown check skipped ({e})")
        phantom_left = 0.0
    if phantom_left > 0:
        logger.info(
            f"{symbol}: entry skipped - phantom cooldown, {phantom_left / 60:.1f} min left "
            f"since its last entry order failed to fill"
        )
        return False

    cooldown_left = 0 if skip_cooldown else executor.reentry_cooldown_remaining(symbol)
    if cooldown_left > 0:
        logger.info(
            f"{symbol}: entry skipped - re-entry cooldown, {cooldown_left / 60:.1f} min left "
            f"since it was last stopped out"
        )
        return False

    # HALT / NOT-TRADABLE CHECK, immediately before the order.
    #
    # A stop is not a promise about execution. If a symbol halts while held and
    # reopens 3% lower, a -0.5% stop fills at -3% - nothing prevents that. What
    # CAN be prevented is opening a position into a name that is already
    # halted or suspended, which is both cheap to check and unambiguous.
    #
    # Checked here rather than at selection time because a halt happens
    # intraday, minutes after the watchlist was built. Cached per symbol per
    # session with a short TTL so it is one API call per name, not one per
    # poll. An UNKNOWN answer (lookup failed) never blocks: a network blip is
    # not evidence of a halt, and treating it as one would stop the whole
    # watchlist the first time the endpoint hiccups.
    if (config["trading"].get("halt_check") or {}).get("enabled"):
        try:
            _cached = _HALT_CACHE.get(symbol)
            _ttl = float((config["trading"]["halt_check"]).get("cache_seconds", 60))
            if _cached is None or (time.monotonic() - _cached[0]) > _ttl:
                _tradable, _why = executor.broker.is_symbol_tradable(symbol)
                _HALT_CACHE[symbol] = (time.monotonic(), _tradable, _why)
            else:
                _, _tradable, _why = _cached
            if _tradable is False:
                logger.warning(f"{symbol}: entry refused - {_why}. Not opening into a halted name.")
                return False
        except Exception as e:
            logger.debug(f"{symbol}: halt check skipped ({e})")

    ok, reason = executor.pre_entry_check(qty, price, symbol=symbol)
    if not ok:
        logger.info(f"{symbol}: entry skipped - {reason}")
        return False

    order = executor.submit_entry_order(symbol, qty, price, entry_method=entry_method, entry_rsi=symbol_rsi)
    if order is None:
        return False  # broker rejected/failed - already logged by submit_entry_order, nothing committed

    # Layered: the burst profile (if any) is the base, chop lowers its
    # take-profit ladder, dynamic stops tighten its stop. Each returns its
    # input unchanged when it has nothing to say, so a plain session entry on a
    # normal day comes through as None and behaves exactly as before.
    _exit_cfg = _chop_exit_config(config, _REGIME_STATE.get("state"), exit_config)
    strategy.confirm_entry(
        symbol, price, qty,
        config_override=_dynamic_exit_config(config, symbol, _exit_cfg),
    )
    executor.entry_meta.setdefault(symbol, {})["list_source"] = symbol_source.get(symbol, "screener")
    # Market/stock state at the entry instant, for trade_context.csv. Stored
    # on entry_meta because that is what survives to the exit, where the row
    # is finally written with its outcome attached.
    if context:
        executor.entry_meta.setdefault(symbol, {})["context"] = dict(context)
    # Where this symbol placed in the dynamic universe's merit ranking. Absent
    # on a static-pool session, which is the correct reading of "there was no
    # ranking" rather than a rank of zero.
    _rank = (universe_info.get("rank") or {}).get(symbol)
    if _rank:
        executor.entry_meta[symbol]["universe_rank"] = _rank
    if burst_note:
        executor.entry_meta.setdefault(symbol, {})["burst_logic"] = burst_note
        if signal_pct is not None:
            # How big the move was when this entry fired - the number
            # rapid_increase_max_pct gates on. On the report it shows where each
            # trade sat relative to the ceiling.
            executor.entry_meta[symbol]["signal_pct"] = round(signal_pct, 3)
    rsi_note = f", RSI={symbol_rsi:.1f}" if symbol_rsi is not None else ""
    size_note = f", {size_multiplier:g}x size" if size_multiplier != 1.0 else ""
    logger.info(
        f"{symbol}: {entry_method} entry confirmed - {qty} shares @ {price:.2f}{rsi_note}{size_note}"
    )
    return True

def run_trading_day(config, market_data, strategy, executor, symbols, rsi_values, email_notifier, et, signal_journal=None):
    """
    Runs the entire trading day as ONE continuous loop, from entry_window_start
    until either all positions have closed after the entry window ends, or the
    4 PM time stop is hit.

    This replaces the old run_entry_window -> run_exit_monitoring design,
    which ran the full 9:30-9:55 entry window as one blocking phase before
    exit-checking ever started - meaning a position opened at, say, 9:31 got
    ZERO stop-loss protection until 9:55. On 2026-08-18, FCEL ran to -7.42%
    (well past both the -0.5% and -1.0% stop thresholds) before the bot ever
    checked it, purely because exit monitoring hadn't started yet. Now, every
    poll cycle checks exits for everything already open AND checks entries
    for everything not yet open (while still inside the entry window) in the
    same pass - stop-losses are live from the moment a position opens.

    If use_pullback_entry is on: a detected rapid increase does NOT buy
    immediately. Instead it starts tracking that symbol for a pullback off
    the post-thrust peak followed by a resumption higher, and only buys on
    the resumption - see _advance_pullback_state for the state machine. If
    off, a rapid increase buys immediately via _attempt_entry.

    If use_three_bar_momentum is on: a separate, faster signal checked ahead
    of everything else. The instant 3 consecutive 1-minute bars are all green
    with each bar's close higher than the last, it buys immediately - no
    waiting for rapid_increase_pct's % threshold, no pullback-wait either.
    """
    entry_start = parse_hhmm_today(config["trading"]["entry_window_start"], et)
    entry_end = parse_hhmm_today(config["trading"]["entry_window_end"], et)
    time_stop_hour = config["trading"]["time_stop_hour"]
    # Fire this many minutes BEFORE time_stop_hour, so a market order still
    # has real liquidity to fill in. Expressed as a lead rather than as a
    # minute-past, because "16:00 minus 5" is the intent and (16, 55) would
    # read as 16:55 - later than the bug it was meant to fix.
    _lead = int(config["trading"].get("time_stop_lead_minutes", 5) or 0)
    _stop_at = (datetime.now(et).replace(hour=time_stop_hour, minute=0,
                                         second=0, microsecond=0)
                - timedelta(minutes=_lead))
    check_interval = config["trading"]["entry_check_interval_seconds"]
    rest_interval = config["trading"].get("entry_check_interval_seconds_rest", 60)
    lookback = timedelta(minutes=config["trading"]["rapid_increase_lookback_minutes"])
    use_rsi_filter = config["trading"].get("use_rsi_filter", False)
    rsi_max = config["trading"].get("rsi_max_for_entry", 50)
    use_pullback_entry = config["trading"].get("use_pullback_entry", False)
    use_three_bar_momentum = config["trading"].get("use_three_bar_momentum", False)
    three_bar_require_acceleration = config["trading"].get("three_bar_require_acceleration", True)
    max_daily_entries = config["trading"].get("max_daily_entries")  # None/0 = unlimited
    daily_entry_cap_logged = False  # so the "cap reached" line is logged once, not every poll
    use_opening_reversal_entry = config["trading"].get("use_opening_reversal_entry", False)
    opening_reversal_window = timedelta(minutes=config["trading"].get("opening_reversal_window_minutes", 5))
    opening_reversal_drop_bars = config["trading"].get("opening_reversal_drop_bars", 5)
    opening_reversal_confirm_bars = config["trading"].get("opening_reversal_confirm_bars", 5)
    rsi_period = config["trading"].get("rsi_period", 14)

    # NOTE the entry gate below is `entry_start <= now < entry_end`, not just
    # `now < entry_end`. It used to be the latter, which was safe only because
    # this function SLEPT until entry_start - the loop could not run early, so
    # the lower bound was implicit. Starting the loop at 09:30 for the opening
    # burst removed that guarantee and let normal entries fire three minutes
    # early. On 2026-08-27 OKTA and CRWD were bought at 09:32:13, both peaked at
    # MFE 0.00%, both hit FINAL_EXIT -1.0%, and cost $195.52 in 25 seconds -
    # exactly the open-chasing that moving entry_window_start to 09:33 was
    # measured to avoid (2026-08-19: the 9:34 and 9:46 bursts went 1 win in 30
    # for -$1,884, while entries from 9:51 went 6 in 12 for +$356).
    #
    # The loop must be running before the NORMAL entry window opens whenever the
    # opening-burst experiment is on: it measures from its baseline instant
    # (09:30) and must decide by 09:32, so sleeping until entry_start (09:33)
    # would mean the mode never ran at all and would fail silently - the loop
    # would simply wake up after its whole window had passed.
    loop_start = entry_start
    ob = _opening_burst_config(config)
    if ob:
        loop_start = min(entry_start, parse_hhmm_today(ob.get("baseline_time", "09:30"), et))

    now = datetime.now(et)
    while now < loop_start:
        time.sleep(min(5, (loop_start - now).total_seconds()))
        now = datetime.now(et)

    if ob:
        logger.info(
            f"===== OPENING BURST ARMED: measuring from "
            f"{ob.get('baseline_time', '09:30')}, deciding by {ob.get('decide_by', '09:32')} ET, "
            f"max {ob.get('max_positions', 4)} positions at "
            f"{ob.get('size_multiplier', 0.5)}x size, streamed symbols only ====="
        )
    logger.info(
        f"===== TRADING DAY START: entries watched {entry_start.strftime('%H:%M')}-"
        f"{entry_end.strftime('%H:%M')} ET, exits watched continuously from the moment "
        f"a position opens ====="
    )

    price_history = {symbol: deque() for symbol in symbols}
    bar_history = {symbol: deque(maxlen=3) for symbol in symbols}  # last 3 1min bars, for use_three_bar_momentum
    pending_pullbacks = {}  # symbol -> state dict, only used when use_pullback_entry is on
    pending_reversals = {}  # symbol -> state dict, only used when use_opening_reversal_entry is on
    reversal_drop_bar_history = {symbol: deque(maxlen=opening_reversal_drop_bars) for symbol in symbols}  # only used when use_opening_reversal_entry is on
    symbol_open_price = {}  # symbol -> first observed price this window, purely for the log message - only tracked when use_opening_reversal_entry is on
    symbol_price_log = {symbol: [] for symbol in symbols}  # full (untrimmed) history, for _write_price_log
    entries_triggered = 0
    poll_state = {"last": None}   # remembers the interval, to log transitions only
    # Session VWAP per symbol, accumulated from bars the loop already reads.
    # symbol -> [sum(typical_price * volume), sum(volume)]. Costs nothing extra:
    # every streamed bar already carries volume.
    vwap_acc = {}
    # First N minutes' high per symbol, for breakout quality.
    opening_high = {}
    # symbol -> signal % that was refused for being too extended, so the report
    # can show what the ceiling actually turned away.
    rejected_for_extension = {}
    had_any_trades = bool(strategy.get_open_trades())  # true if reconciliation adopted positions on startup
    starting_cash = None
    try:
        starting_cash = float(market_data.broker.get_account().cash)
    except Exception as e:
        logger.debug(f"Could not read starting cash for daily summary: {e}")

    def finish_day(reason):
        signal_journal.flush()
        executor.save_trades_log()
        # Compute the burst summary BEFORE the report that displays it. This
        # used to run three lines lower, so every report ever sent carried the
        # PREVIOUS value of day_burst_summary - on the first session of a
        # process, the empty default.
        executor.day_burst_summary = _summarise_burst_notes(config, day_burst_notes)
        email_notifier.send_daily_summary(burst_summary=executor.day_burst_summary)
        report_state["sent"].add((datetime.now(et).date(), _slot_for_finish(et)))
        _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et)
        _write_price_log(symbol_price_log, et)
        logger.info(f"Burst logic for the day: {executor.day_burst_summary}")
        logger.info(f"Signal journal: {signal_journal.stats()}")
        logger.info(f"Daily session complete: entries_triggered={entries_triggered}, reason={reason}")

        # The alert layer's whole point: EmailNotifier.send_alert has existed,
        # wired to both channels and tested, with zero call sites - so no alert
        # has ever been delivered. On 2026-09-02 the session ended at 09:38 on
        # the loss limit and nothing said so until the journal was read hours
        # later. Fail-safe by construction (see alerts._send): a channel that
        # is down can never affect the shutdown path it is reporting on.
        try:
            from src.notifications import alerts as _AL
            _still_open = sorted(strategy.get_open_trades().keys())
            _AL.session_ended(
                config, email_notifier,
                reason=reason,
                realized=getattr(executor, "_realized_pnl_today", 0.0),
                unrealized=(getattr(executor, "daily_pnl", 0.0)
                            - getattr(executor, "_realized_pnl_today", 0.0)),
                entries=entries_triggered,
                trades=len(getattr(executor, "trade_log", []) or []),
                open_positions=len(_still_open),
                equity=getattr(executor, "equity", None),
                ended_at=f"{datetime.now(et):%Y-%m-%d %H:%M:%S} ET",
            )
            if _still_open:
                _AL.positions_left_open(
                    config, email_notifier, symbols=_still_open, when=reason)
        except Exception as e:
            logger.error(f"session-end alert failed: {type(e).__name__}: {e}")

    if signal_journal is None:
        signal_journal = SignalJournal(config)
    stream_warned = False
    volume_history = {symbol: deque(maxlen=20) for symbol in symbols}  # for intraday RVOL
    spy_history = deque()          # SPY samples, for excess-return-vs-market
    qqq_history = deque()          # QQQ samples, for the two-index regime rule
    # {etf: deque} - one benchmark per sector represented on the watchlist, so a
    # symbol can be compared to what it actually moves with rather than only to
    # the index. Built from the watchlist, so an all-semis day fetches one ETF
    # and never touches the other eleven.
    # Session peak signal, for the ceiling report - see the update below.
    day_peak_signal = {"value": 0.0, "symbol": None, "at": None}
    # Opening-burst state, persisting across polls within the session.
    opening_state = {"baseline": {}, "taken": [], "done": False}
    breadth_state = {"open_px": {}}
    reconcile_state = {}
    regime_state = {}
    # Published so _attempt_entry can read the current label. Same dict object,
    # mutated in place by _regime_multiplier, so it cannot go stale.
    _REGIME_STATE["state"] = regime_state
    # RESET THE CARRY-OVER, explicitly. This process reuses one Executor for
    # every session it ever runs, and regime_size_multiplier lives on it. A
    # session that ended bearish leaves 0.0 there, and the opening burst runs
    # EARLIER in the poll than the regime check does - so the next morning's
    # burst would size every entry to zero shares and take nothing, silently,
    # with no bearish tape to explain it. Same class of bug as strategy.trades
    # surviving a day, which this file already guards against elsewhere.
    executor.regime_size_multiplier = 1.0
    market_open_dt = parse_hhmm_today("09:30", et)
    sector_history = {}
    # Only the sectors this watchlist actually needs. Computed once here rather
    # than per poll: the watchlist does not change during a session.
    try:
        from src.analytics import sectors as SEC
        sector_etfs = SEC.sectors_for(symbols)
        concentration = SEC.sector_concentration(symbols)
        if sector_etfs:
            logger.info(f"Sector benchmarks for today: {', '.join(sector_etfs)}")
        unmapped = [s_ for s_ in symbols if not SEC.sector_for(s_)]
        if unmapped:
            logger.info(
                f"No sector mapping for {len(unmapped)} symbol(s): "
                f"{', '.join(sorted(unmapped))} - they score None on sector "
                f"strength, which the continuation score drops rather than "
                f"treating as weak"
            )
        for _etf, _members in concentration.items():
            logger.warning(
                f"SECTOR CONCENTRATION: {len(_members)} of {len(symbols)} watched "
                f"symbols map to {_etf} ({', '.join(_members)}) - a burst across "
                f"these is one bet held {len(_members)} times"
            )
    except Exception as e:
        logger.error(f"Sector setup failed, continuing without it: {e}")
        sector_etfs = []
    day_burst_notes = []           # one note per poll, summarised into the daily report

    while True:
        now = datetime.now(et)
        executor.refresh_account_snapshot()

        # A stream that is nominally connected but delivering no bars is the
        # dangerous case: every read silently falls back to REST, so the bot
        # keeps trading on ~15-minute-delayed prices while the logs show a
        # connected stream. Warn once per session rather than let that pass
        # unnoticed. Trading is unaffected either way - the fallback is what
        # keeps prices flowing - so this is a heads-up, not a halt.
        # Fill in forward returns for journalled signals whose horizon has
        # elapsed. Uses the price source already in use, so no extra API cost
        # for symbols already being watched.
        signal_journal.update_forward_returns(
            lambda sym: (market_data.get_latest_bar(sym, "1Min") or {}).get("close")
        )
        # Persist anything whose forward horizons have all elapsed. Append-only,
        # so this is cheap and cannot damage rows already written. Without it the
        # journal existed purely in memory until finish_day, and any session that
        # ended by crash, OOM or restart contributed nothing at all - which is
        # fatal for a dataset whose entire value is accumulating day over day.
        signal_journal.flush(final=False)
        # What happened AFTER each exit. Same price source the loop already
        # reads, so no extra API calls for symbols still on the watchlist.
        executor.note_post_exit_prices(
            lambda sym: (market_data.get_latest_bar(sym, "1Min") or {}).get("close"),
            minutes=config["trading"].get("post_exit_track_minutes", 15),
        )
        _refresh_run_context(email_notifier, getattr(market_data, "stream", None))

        stream = getattr(market_data, "stream", None)
        if stream is not None and not stream_warned:
            stats = stream.stats()
            if stats["connected"] and not stats["healthy"] and stats["bars_received"] == 0:
                logger.warning(
                    "Price stream is connected but has delivered ZERO bars - "
                    "every price read is falling back to REST (~15 min delayed). "
                    "Check for an Alpaca stream connection limit (only one data "
                    "websocket per account) or a network the stream endpoint refuses."
                )
                stream_warned = True

        # Soft warning on the way down, checked BEFORE the hard limit so a day
        # that crosses several thresholds in one poll still reports where it
        # got to before it stopped. Warning only - it never halts anything.
        try:
            executor.check_loss_velocity()
        except Exception as e:
            logger.debug(f"loss-velocity check skipped: {e}")

        if executor.check_daily_loss_limit():
            logger.warning("Daily loss limit hit, flattening all positions")
            try:
                from src.notifications import alerts as _AL
                _open_at = getattr(executor, "_loss_watch_started_at", None)
                _AL.loss_limit_hit(
                    config, email_notifier,
                    daily_pnl=getattr(executor, "daily_pnl", None),
                    limit=-abs(config["trading"].get("max_daily_loss_usd") or 0),
                    entries=entries_triggered,
                    elapsed_minutes=((datetime.now() - _open_at).total_seconds() / 60
                                     if _open_at else None),
                )
            except Exception as e:
                logger.error(f"loss-limit alert failed: {type(e).__name__}: {e}")
            flattened = executor.flatten_all_positions()
            for symbol in flattened:
                strategy.trades.pop(symbol, None)
            finish_day("daily_loss_limit")
            return entries_triggered

        # ---- EXIT CHECKS: every open position, every cycle, from the moment it opens ----
        # Flushed once at the end of the exit sweep rather than per symbol -
        # one file open per poll instead of one per open position.
        _path_buffer = []
        for symbol in list(strategy.get_open_trades().keys()):
            try:
                current_bar = market_data.get_latest_bar(symbol, "1Min")
                if not current_bar:
                    continue

                # INTRA-TRADE PRICE PATH, sampled every poll a position is
                # open. This is the file exit-rule replay actually runs on:
                # mfe_pct/mae_pct give the extremes but NOT their order, and
                # every stop question ("did it reach +1% BEFORE -0.5%?") is a
                # question about order. Appended per sample rather than
                # buffered to the exit, so a crash costs the tail of one path
                # rather than the whole day's.
                try:
                    _tr = strategy.trades.get(symbol)
                    _px = market_data.get_entry_price(symbol, current_bar)
                    if _tr is not None and _px:
                        _meta = executor.entry_meta.get(symbol) or {}
                        _entry_px = getattr(_tr, "entry_price", None)
                        _path_buffer.append({
                            "trade_id": TR.make_trade_id(symbol, _meta.get("entry_time")),
                            "symbol": symbol,
                            "date": now.strftime("%Y-%m-%d"),
                            "timestamp": now.isoformat(),
                            "price": _px,
                            "gain_pct": (round((_px - _entry_px) / _entry_px * 100, 4)
                                         if _entry_px else ""),
                        })
                except Exception as _pe:
                    logger.debug(f"path sample skipped for {symbol}: {_pe}")

                exit_info = strategy.check_exit(symbol, current_bar)
                if exit_info:
                    # RSI is purely for the daily report - a failure here (API
                    # hiccup, insufficient history) must never block the
                    # actual exit order.
                    try:
                        exit_rsi = market_data.get_rsi(symbol, period=rsi_period)
                    except Exception as rsi_err:
                        logger.debug(f"Could not fetch exit RSI for {symbol}: {rsi_err}")
                        exit_rsi = None

                    # Read excursions BEFORE confirm_exit - a full exit deletes
                    # the TradeManager, taking the peak/trough with it.
                    trade = strategy.trades.get(symbol)
                    mfe_pct, mae_pct = trade.excursions() if trade else (None, None)

                    order = executor.submit_exit_order(
                        symbol, exit_info["qty"], exit_info["reason"], exit_info["price"],
                        exit_rsi=exit_rsi, mfe_pct=mfe_pct, mae_pct=mae_pct,
                        # So the executor can tell a partial sale from a full
                        # one by the numbers rather than by the reason string.
                        qty_before=(trade.qty_remaining if trade else None),
                    )
                    if order is PHANTOM_EXIT:
                        # The broker held zero shares - the entry never
                        # filled. Nothing was bought or sold, so there is no
                        # trade to commit; confirm_exit's P&L math assumes a
                        # real fill happened and would be wrong here. Drop the
                        # phantom directly instead of retrying a sell against
                        # nothing every poll for the rest of the session (the
                        # 2026-09-01 shape: NOW/PLTR/MSTR/RGTI/SOXL, 45+
                        # minutes of identical retries).
                        strategy.drop_phantom(symbol)
                    elif order is not None:
                        # The executor may have corrected the qty down from
                        # what was requested (a broker-side partial fill it
                        # caught before submitting) - commit whatever it
                        # actually sold, not what was originally computed.
                        actual_qty = executor.exit_qty_actually_submitted(symbol, exit_info["qty"])
                        strategy.confirm_exit(symbol, actual_qty, exit_info["reason"], exit_info["price"])
                        had_any_trades = True
                    else:
                        logger.error(
                            f"{symbol}: exit order FAILED to submit ({exit_info['reason']}) - "
                            f"position remains tracked, will retry next check"
                        )

            except Exception as e:
                logger.error(f"Error checking exits for {symbol}: {e}")
                continue

        if _path_buffer:
            TR.record_path_samples(_path_buffer)

        # ---- OPENING BURST: runs BEFORE and independently of the normal window ----
        # Its own budget, its own settings, its own entry-method tag. Kept
        # separate so a normal testing session and this experiment can share a
        # day without the experiment consuming the day's position capacity at
        # the bell - which is what widening entry_window_start to 09:30 would
        # have done.
        try:
            opened = _run_opening_burst(
                config, market_data, strategy, executor, symbols, rsi_values,
                opening_state, now, et, signal_journal=signal_journal,
                spy_pct=_window_pct_change(spy_history),
                # For the multi-factor rank. Passed rather than recomputed:
                # these are the same series the normal entry path scores on.
                price_history=price_history,
                volume_history=volume_history,
                vwap_acc=vwap_acc,
                sector_returns={etf: _window_pct_change(hist)
                                for etf, hist in sector_history.items()},
                spy_history=spy_history,
                qqq_history=qqq_history,
                breadth_state=breadth_state,
                regime_state=regime_state,
            )
            if opened:
                entries_triggered += opened
                had_any_trades = True
        except Exception as e:
            logger.error(f"Opening burst failed, continuing with the normal session: {e}",
                         exc_info=True)

        # ---- ENTRY CHECKS: only within the window, only for symbols not already open ----
        # max_daily_entries is a separate limit from max_concurrent_positions
        # and caps a different thing: the TOTAL number of positions opened over
        # the whole day, however many are held at once. On 2026-08-19 the
        # concurrent cap of 10 was respected, yet 54 buys still went through in
        # ~21 minutes, because positions kept closing and new ones opening in
        # their place - the concurrent cap has nothing to say about that churn.
        #
        # Deliberately gates ONLY this entry block. Exit checks above run every
        # cycle regardless, so hitting the cap stops the bot opening anything
        # new but never leaves an already-open position unprotected.
        #
        # entries_triggered is local to run_trading_day, so it resets on its own
        # each trading day rather than accumulating across days in this
        # long-running process.
        # Open-price snapshot for the breadth check. Taken on every poll but
        # only ever WRITTEN once per symbol, so each symbol's reference is the
        # first price seen after the bell rather than a moving target. Symbols
        # on REST arrive with a stale price and are simply the ones the check
        # cannot see - min_symbols is what stops it concluding anything from too
        # few.
        if now >= market_open_dt:
            for _sym in symbols:
                if _sym in breadth_state["open_px"]:
                    continue
                try:
                    _bar = market_data.get_latest_bar(_sym, "1Min")
                    if _bar:
                        _px = market_data.get_entry_price(_sym, _bar)
                        if _px:
                            breadth_state["open_px"][_sym] = _px
                except Exception:
                    continue

        # BENCHMARK SAMPLING - from the market OPEN, not from entry_window_start.
        #
        # This used to sit inside the entry-window branch, which meant SPY and
        # QQQ were not sampled until 09:33. Two things broke as a result: the
        # opening burst (09:30-09:33) had no market context to record against
        # its trades, and the 09:45 regime check had only twelve minutes of
        # VWAP instead of fifteen. Sampling from the bell costs two REST reads
        # per poll and fixes both.
        if now >= market_open_dt:
            for _bench, _hist in (("SPY", spy_history), ("QQQ", qqq_history)):
                try:
                    _bar = market_data.get_latest_bar(_bench, "1Min")
                    if not _bar:
                        continue
                    _ts = _bar.get("timestamp", now)
                    _close = _bar.get("close", 0)
                    _hist.append((_ts, _close))
                    _cut = _ts - lookback
                    while _hist and _hist[0][0] < _cut:
                        _hist.popleft()
                    # First print of the day only - _regime_multiplier reads
                    # this as the index's "since the open" reference, the same
                    # role breadth_state["open_px"] plays per watchlist symbol.
                    if _close and _bench not in breadth_state["open_px"]:
                        breadth_state["open_px"][_bench] = _close
                    # Session VWAP for the index itself - the primary regime
                    # input. Same accumulator the watchlist uses, so a
                    # benchmark stuck on REST simply never accumulates and the
                    # regime read falls back rather than reading a wrong one.
                    _update_vwap(vwap_acc, _bench, _bar)
                except Exception as e:
                    logger.debug(f"{_bench} benchmark unavailable this poll: {e}")

        # The breadth MEASUREMENT (mean move since the open, into
        # breadth_state) runs regardless - regime_sizing reuses it, both as the
        # fallback when an index has no VWAP and as the CHOP reading. The halt
        # itself was retired 2026-09-02: it had been dead code since regime
        # sizing shipped, and two guards answering "is today tradeable?" from
        # different evidence meant whichever ran first won by ordering accident
        # rather than by rule. See _measure_breadth.
        regime_cfg = config.get("trading", {}).get("regime_sizing") or {}
        regime_active = regime_cfg.get("enabled", False)
        _measure_breadth(config, market_data, symbols, breadth_state, now, et)

        # PERIODIC RECONCILE. The broker is truth; this reports divergence and
        # alerts, it does not silently repair - see
        # Executor.reconcile_against_broker for why repair belongs at the
        # specific call sites that know the cause.
        _rc = config["trading"].get("reconcile") or {}
        if _rc.get("enabled", True):
            _iv = float(_rc.get("interval_seconds", 300))
            if (now - reconcile_state.get("last", now - timedelta(seconds=_iv + 1))
                    ).total_seconds() >= _iv:
                reconcile_state["last"] = now
                try:
                    _mm, _ = executor.reconcile_against_broker(
                        list(strategy.get_open_trades().keys()))
                    # Alert only on a CHANGE. A divergence that persists across
                    # polls is one problem, not one per five minutes, and a
                    # channel that repeats itself is one that gets muted.
                    _sig = tuple(sorted(_mm))
                    if _mm and _sig != reconcile_state.get("last_signature"):
                        reconcile_state["last_signature"] = _sig
                        from src.notifications import alerts as _AL
                        _AL.degraded(
                            config, email_notifier,
                            what=f"{len(_mm)} position mismatch(es) vs the broker",
                            detail="The bot's view of what it holds disagrees with "
                                   "the broker:\n\n"
                                   + "\n".join(f"  {s}: {w}" for s, w in _mm)
                                   + "\n\nThe broker is authoritative. A SHORT here "
                                     "means the phantom-entry path fired; close it "
                                     "by hand if the 16:00 flatten has not.",
                        )
                    elif not _mm:
                        reconcile_state["last_signature"] = None
                except Exception as e:
                    logger.debug(f"reconcile skipped: {e}")
        if regime_active:
            _mult, _label = _regime_multiplier(
                config, regime_state, breadth_state, spy_history, now, et,
                vwap_acc=vwap_acc, qqq_history=qqq_history,
            )
            # Before check_time _regime_multiplier returns a bare 1.0 meaning
            # "no opinion yet". Assigning that would STOMP the provisional
            # bearish read the burst close may have set, restoring full size on
            # the next poll and undoing the early gate entirely. So the
            # provisional stands until the real check actually produces a
            # label, at which point it is replaced wholesale.
            # REGIME-DRIVEN STOP TIGHTENING. The regime read governs new
            # entries; open positions were left to find their own way to a
            # -1.0% stop one at a time. On 2026-09-02 nine longs did exactly
            # that, independently, for one shared reason. Fired ONCE per
            # transition into a bearish label - latched on the label itself,
            # so a regime that oscillates cannot re-tighten every poll.
            if _label == "bearish" and regime_state.get("tightened_at") != _label:
                regime_state["tightened_at"] = _label
                _rt = (regime_cfg.get("bearish_exits") or {})
                if _rt.get("enabled", True) and strategy.get_open_trades():
                    _changes = strategy.tighten_all_for_regime(
                        final_pct=_rt.get("final_exit_loss_pct", -0.5),
                        trail_pct=_rt.get("trailing_stop_pct", 0.4),
                        breakeven_trigger=_rt.get("breakeven_trigger_pct", 0.1),
                    )
                    if _changes:
                        logger.warning(
                            f"===== REGIME BEARISH: tightening {len(_changes)} open "
                            f"position(s) rather than letting each find its own stop "
                            f"=====")
                        for _s, _note in _changes.items():
                            logger.warning(f"  {_s}: {_note}")
            elif _label and _label != "bearish":
                # Clear the latch so a LATER return to bearish can tighten
                # again. Nothing is loosened by this - tighten_for_regime is
                # monotonic and never re-widens a live stop.
                regime_state["tightened_at"] = None

            _prov = regime_state.get("provisional")
            if _label is None and _prov is not None:
                executor.regime_size_multiplier = _prov
            else:
                executor.regime_size_multiplier = _mult
                if _label is not None and _prov is not None:
                    regime_state.pop("provisional", None)
                    logger.info(
                        f"REGIME: the {regime_cfg.get('check_time', '09:40')} read "
                        f"({_label}, {_mult}x) replaces the provisional burst-close "
                        f"read ({_prov}x)."
                    )

        # Sector scoreboard, logged with the breadth check and again at the halt
        # decision. sector_strength already feeds the signal journal per signal
        # (rho +0.483 against the 15-min forward return on 2026-08-28, one
        # session, sign unconfirmed) - what was missing is a plain human-readable
        # note of which complexes were working WHILE the session ran, rather
        # than only in the next morning's table.
        if breadth_state.get("checked") and not breadth_state.get("sector_logged"):
            breadth_state["sector_logged"] = True
            try:
                import src.analytics.sectors as _SEC
                by_sector = {}
                for _sym, _open in (breadth_state.get("open_px") or {}).items():
                    _sec = _SEC.sector_for(_sym)
                    if not _sec:
                        continue
                    _bar = market_data.get_latest_bar(_sym, "1Min")
                    _px = market_data.get_entry_price(_sym, _bar) if _bar else None
                    if _px and _open:
                        by_sector.setdefault(_sec, []).append((_px - _open) / _open * 100)
                if by_sector:
                    ranked = sorted(
                        ((sum(v) / len(v), k, len(v)) for k, v in by_sector.items()),
                        reverse=True)
                    logger.info(
                        f"SECTOR SCOREBOARD at {now:%H:%M} ET (mean move since the open): "
                        + ", ".join(f"{k} {m:+.2f}% (n={n})" for m, k, n in ranked)
                    )
            except Exception as e:
                logger.debug(f"sector scoreboard skipped: {e}")

        if max_daily_entries and entries_triggered >= max_daily_entries:
            if not daily_entry_cap_logged:
                logger.info(
                    f"Reached max_daily_entries ({entries_triggered}/{max_daily_entries}) - "
                    f"no new entries for the rest of the day; exits continue as normal"
                )
                daily_entry_cap_logged = True
        elif entry_start <= now < entry_end:
            # SPY is tracked purely as a market benchmark - never traded. It is
            # what separates "this stock is strong" from "the market went up":
            # during a burst every name's raw move looks alike, but excess
            # return over the index collapses toward zero for the ones that are
            # only beta.
            # Sector ETFs, sampled exactly like SPY and for the same reason one
            # step further in. Excess return over SPY separates "this stock is
            # strong" from "the market went up"; excess over the SECTOR
            # separates it from "the whole complex went up", which SPY cannot
            # see. On 2026-08-26 seven of fourteen watched symbols mapped to the
            # crypto complex - a burst across those is one bet held seven times,
            # and against SPY every one of them looked independently strong.
            #
            # REST, not the stream: these cost no subscription slots, and the
            # watchlist already wants every one of the 28 available.
            for _etf in sector_etfs:
                try:
                    _bar = market_data.get_latest_bar(_etf, "1Min")
                    if not _bar:
                        continue
                    _ts = _bar.get("timestamp", now)
                    _hist = sector_history.setdefault(_etf, deque())
                    _hist.append((_ts, _bar.get("close", 0)))
                    _cut = _ts - lookback
                    while _hist and _hist[0][0] < _cut:
                        _hist.popleft()
                except Exception as e:
                    logger.debug(f"sector benchmark {_etf} unavailable: {e}")

            # PASS 1 - detect only. Signals are collected, not acted on, so
            # the number firing in THIS poll is known before any order is
            # placed. That count (the burst width) is what _burst_policy needs
            # in order to distinguish a couple of independent ideas from one
            # market move showing up in twenty tickers at once.
            #
            # Only the two live entry paths are deferred this way. The
            # pullback and opening-reversal state machines are both toggled
            # off and keep their existing immediate behavior rather than being
            # restructured while unproven.
            burst_candidates = []
            for symbol in symbols:
                # Re-checked per SYMBOL, not just once per poll. The outer
                # check above only runs at the top of a cycle, and this inner
                # loop can open many positions within that one cycle - on
                # 2026-08-19 twenty entries landed inside a single 9-second
                # pass - so a per-poll-only check would let an entire burst
                # through before the cap was ever consulted again. Same
                # failure shape as the exposure-cache race.
                if max_daily_entries and entries_triggered >= max_daily_entries:
                    if not daily_entry_cap_logged:
                        logger.info(
                            f"Reached max_daily_entries ({entries_triggered}/{max_daily_entries}) - "
                            f"no new entries for the rest of the day; exits continue as normal"
                        )
                        daily_entry_cap_logged = True
                    break

                if symbol in strategy.get_open_trades():
                    continue

                try:
                    bar = market_data.get_latest_bar(symbol, "1Min")
                    if not bar:
                        continue

                    # ENTRY price only. The exit loop above keeps using the
                    # bar close it already reads - see get_entry_price.
                    price = market_data.get_entry_price(symbol, bar)
                    # A bar can exist while its entry price does not - a bar
                    # with no trades, a stream frame missing the field. Letting
                    # None into price_history poisons every consumer that
                    # reads it back as a number: check_rapid_increase_entry
                    # compares `price_then <= 0` and raises, which the
                    # surrounding except turns into "Error checking entry for
                    # X" once per symbol per poll for the rest of the session -
                    # and that symbol is then never evaluated again. Skip the
                    # poll instead; the next one will have a price.
                    if price is None:
                        continue
                    ts = bar.get("timestamp", now)
                    history = price_history[symbol]
                    history.append((ts, price))
                    bar_history[symbol].append(bar)
                    symbol_price_log[symbol].append((ts, price))
                    volume_history[symbol].append(float(bar.get("volume") or 0))
                    _update_vwap(vwap_acc, symbol, bar)
                    # Opening-range high: the session's first
                    # opening_range_minutes of bars, for breakout quality.
                    try:
                        or_end = entry_start + timedelta(
                            minutes=config["trading"].get("opening_range_minutes", 5))
                        if now <= or_end:
                            hi = float(bar.get("high") or price)
                            opening_high[symbol] = max(opening_high.get(symbol, 0), hi)
                    except Exception:
                        pass

                    # Drop samples older than the lookback window, measured from the latest
                    # BAR's own timestamp (not wall-clock `now`) - the feed can lag wall-clock
                    # by a few minutes, and anchoring to `now` would evict every sample before
                    # two ever accumulate whenever that lag exceeds `lookback`.
                    cutoff = ts - lookback
                    while history and history[0][0] < cutoff:
                        history.popleft()

                    symbol_rsi = rsi_values.get(symbol)
                    if use_rsi_filter and (symbol_rsi is None or symbol_rsi >= rsi_max):
                        continue  # doesn't qualify on RSI at all - don't bother tracking it

                    if use_three_bar_momentum and _check_three_bar_momentum(
                        bar_history[symbol], require_acceleration=three_bar_require_acceleration
                    ):
                        closes = [b.get("close", 0) for b in bar_history[symbol]]
                        gaps = [closes[1] - closes[0], closes[2] - closes[1]]
                        logger.info(
                            f"{symbol}: THREE-BAR MOMENTUM signal - 3 consecutive green 1min "
                            f"bars, closes {closes[0]:.2f} -> {closes[1]:.2f} -> {closes[2]:.2f} "
                            f"(gaps +{gaps[0]:.2f}, +{gaps[1]:.2f}"
                            f"{', accelerating' if three_bar_require_acceleration else ''})"
                        )
                        burst_candidates.append({
                            "symbol": symbol, "price": price, "method": "THREE_BAR_MOMENTUM",
                            "rsi": symbol_rsi, "signal_pct": None, "bar": bar,
                        })
                        continue

                    if use_pullback_entry and symbol in pending_pullbacks:
                        entered = _advance_pullback_state(
                            config, strategy, executor, symbol, price, pending_pullbacks,
                            symbol_rsi, market_data=market_data,
                        )
                        if entered:
                            entries_triggered += 1
                            had_any_trades = True
                            pending_pullbacks.pop(symbol, None)
                        continue

                    if use_opening_reversal_entry:
                        if symbol not in symbol_open_price:
                            symbol_open_price[symbol] = price

                        if symbol in pending_reversals:
                            entered = _advance_reversal_state(
                                config, strategy, executor, symbol, bar, pending_reversals,
                                symbol_rsi, market_data=market_data,
                            )
                            if entered:
                                entries_triggered += 1
                                had_any_trades = True
                                pending_reversals.pop(symbol, None)
                            continue

                        elif now - entry_start <= opening_reversal_window:
                            reversal_drop_bar_history[symbol].append(bar)
                            if _check_reversal_bar_pattern(
                                reversal_drop_bar_history[symbol], opening_reversal_drop_bars, direction="down"
                            ):
                                pending_reversals[symbol] = {
                                    "base_price": symbol_open_price[symbol],
                                    "low": price,
                                    "bounce_bar_history": deque(maxlen=opening_reversal_confirm_bars),
                                }
                                logger.info(
                                    f"{symbol}: OPENING DECLINE detected - {opening_reversal_drop_bars} "
                                    f"consecutive red bars from open {symbol_open_price[symbol]:.2f}, now "
                                    f"{price:.2f} - watching for a reversal bounce"
                                )
                                continue

                    if len(history) < 2:
                        continue

                    price_then = history[0][1]
                    qty, pct_change = strategy.check_rapid_increase_entry(symbol, price, price_then)

                    if qty > 0:
                        # CEILING on the signal, not just a floor.
                        #
                        # rapid_increase_pct says how big a move must be to
                        # qualify; this says how big is TOO big. A stock up 2%
                        # in three minutes has already made its move, and the
                        # -1.0% stop then sits exactly where the natural
                        # pullback lands. Counterintuitive but measured: on
                        # 2026-08-19 signals of 1.0%+ produced 1 winner in 6
                        # for -$531, while signals under 1.0% produced 4
                        # winners in 14 for -$181. The strongest-looking
                        # signals were the worst.
                        #
                        # The skip is still recorded in the signal journal with
                        # its forward returns, so skipping does not cost the
                        # data needed to tell whether this threshold is right.
                        max_signal = config["trading"].get("rapid_increase_max_pct", 0)
                        # Optionally applied only to STREAMED symbols. On a REST
                        # symbol the price is ~15 minutes delayed, so "+2% over
                        # 3 minutes" describes a window that closed a quarter of
                        # an hour ago - gating on a number that stale is acting
                        # on noise in either direction. Streamed symbols carry a
                        # signal % that is actually current, which is what makes
                        # the ceiling meaningful.
                        if max_signal and config["trading"].get(
                                "rapid_increase_max_pct_streamed_only", False):
                            stream = getattr(market_data, "stream", None)
                            streamed = bool(
                                stream is not None
                                and symbol in getattr(stream, "_symbols", [])
                            )
                            if not streamed:
                                max_signal = 0

                        if max_signal and pct_change > max_signal:
                            logger.info(
                                f"{symbol}: signal skipped - +{pct_change:.2f}% is above "
                                f"rapid_increase_max_pct {max_signal}% (the move is already made)"
                            )
                            rejected_for_extension[symbol] = round(pct_change, 3)
                            continue

                        # ---- EXTENSION FROM THE OPEN -------------------
                        #
                        # rapid_increase_max_pct above measures the 3-MINUTE
                        # delta. That is not the same quantity as how far the
                        # name has already travelled today, and the gap is
                        # where 2026-09-02's losses lived: WDAY's 3-minute
                        # change was +0.95% (under the 1.25% ceiling, so it
                        # passed) while its move since the open was +1.344%.
                        # It was bought at 09:34:56 near the top of a run that
                        # started at the bell, and closed -1.59%.
                        #
                        # Two independent refusals here, both reading the
                        # burst's own measurements:
                        #   1. already extended more than
                        #      max_extension_from_open_pct off the open, or
                        #   2. the burst MEASURED this symbol, saw it clear
                        #      its threshold, and did not take it.
                        # Both lapse at refused_suppressed_until, after which
                        # a fresh move is a genuinely new signal rather than
                        # the tail of the opening one.
                        _ext_note = None
                        try:
                            _ob = (config["trading"].get("opening_burst") or {})
                            _until = parse_hhmm_today(
                                _ob.get("refused_suppressed_until", "09:45"), et)
                            if now < _until:
                                _base = (opening_state.get("baseline") or {}).get(symbol)
                                _cap = config["trading"].get("max_extension_from_open_pct")
                                if _cap and _base:
                                    _ext = (price - _base) / _base * 100
                                    if _ext > _cap:
                                        _ext_note = (
                                            f"already +{_ext:.3f}% from the open, above "
                                            f"max_extension_from_open_pct {_cap}% - this is "
                                            f"the tail of the opening move, not a new one"
                                        )
                                if _ext_note is None and symbol in (opening_state.get("refused") or []):
                                    _mv = (opening_state.get("closed_moves") or {}).get(symbol)
                                    _ext_note = (
                                        f"the opening burst measured it at "
                                        f"{_mv:+.3f}% and did not take it; suppressed until "
                                        f"{_ob.get('refused_suppressed_until', '09:45')}"
                                    )
                        except Exception as e:
                            logger.debug(f"extension gate skipped for {symbol}: {e}")

                        if _ext_note:
                            logger.info(f"{symbol}: signal skipped - {_ext_note}")
                            rejected_for_extension[symbol] = round(pct_change, 3)
                            continue

                        if use_pullback_entry:
                            pending_pullbacks[symbol] = {
                                "qty": qty,
                                "base_price": price_then,
                                "thrust_price": price,
                                "peak": price,
                                "pullback_low": None,
                                "pct_change": pct_change,
                            }
                            logger.info(
                                f"{symbol}: RAPID INCREASE detected (+{pct_change:.2f}% over "
                                f"{lookback.total_seconds()/60:.0f}min) - watching for a pullback "
                                f"and resumption before entering"
                            )
                            continue

                        logger.info(
                            f"{symbol}: RAPID INCREASE signal - +{pct_change:.2f}% over "
                            f"{lookback.total_seconds()/60:.0f}min (threshold "
                            f"{config['trading']['rapid_increase_pct']}%)"
                        )
                        burst_candidates.append({
                            "symbol": symbol, "price": price, "method": "RAPID_INCREASE_IMMEDIATE",
                            "rsi": symbol_rsi, "signal_pct": round(pct_change, 3), "bar": bar,
                        })

                except Exception as e:
                    logger.error(f"Error checking entry for {symbol}: {e}")
                    continue

            # PASS 2 - act. Burst width is now known for the whole poll.
            burst_width = len(burst_candidates)
            # Computed here, before the burst policy - the market's own move is
            # now an input to that decision, not just a journal column.
            spy_pct = _window_pct_change(spy_history)
            # Highest signal of the session, recorded whether or not the ceiling
            # cut it. A ceiling that never binds is indistinguishable in the
            # logs from one that is working, and on 2026-08-26 the 2.0% setting
            # had refused nothing since it shipped - the largest signal all day
            # was 1.452%. Reporting the peak beside the threshold makes that
            # visible on the day rather than on a later audit.
            for _c in burst_candidates:
                _sp = _c.get("signal_pct")
                if _sp is not None and _sp > day_peak_signal["value"]:
                    day_peak_signal.update(value=_sp, symbol=_c["symbol"],
                                           at=now.strftime("%H:%M:%S"))
            # Same window as spy_pct and as the symbol's own signal_pct - a
            # relative-strength number computed over a different window than the
            # thing it is relative to is not a comparison.
            sector_returns = {etf: _window_pct_change(hist)
                              for etf, hist in sector_history.items()}
            burst_max, burst_size, burst_note = _burst_policy(config, burst_width, spy_pct)

            # Continuation factors, computed ONCE per candidate and BEFORE any
            # entry decision. Two reasons for the move: ranking needs the score
            # in hand before the throttle cuts the list, and the journal used to
            # recompute the same factors (plus rvol and spread) a second time
            # per signal further down.
            for cand in burst_candidates:
                cand["spread_pct"] = _spread_pct(market_data, cand["symbol"], cand["price"])
                cand["rvol"] = _compute_rvol(cand["bar"], volume_history[cand["symbol"]])
                cand["cont"] = _continuation_fields(
                    config, cand["symbol"], cand["price"], cand["signal_pct"], spy_pct,
                    [p for _, p in price_history[cand["symbol"]]],
                    list(volume_history[cand["symbol"]]),
                    _vwap(vwap_acc, cand["symbol"]),
                    {**(screener_details.get(cand["symbol"]) or {}),
                     "opening_high": opening_high.get(cand["symbol"])},
                    rvol=cand["rvol"],
                    spread_pct=cand["spread_pct"],
                    sector_returns=sector_returns,
                )

            # Best-first, so the throttle keeps the best of a burst rather than
            # whichever names happened to sort earliest. No-op unless
            # use_continuation_score is on.
            burst_candidates, rank_note = _rank_burst(config, burst_candidates)
            if rank_note and burst_max is not None and burst_width > burst_max:
                logger.info(f"BURST {rank_note} - keeping top {burst_max}")
            if burst_max is not None and burst_width >= config["trading"].get("burst_width_threshold", 5):
                logger.warning(
                    f"BURST DETECTED: {burst_width} symbols signalled in one poll "
                    f"({', '.join(c['symbol'] for c in burst_candidates)}) - {burst_note}"
                )
            day_burst_notes.append(burst_note)

            for index, cand in enumerate(burst_candidates):
                symbol, price = cand["symbol"], cand["price"]
                taken, skip_reason = False, None

                # Correlation limiter, checked here rather than inside
                # _attempt_entry because this is where price_history is in
                # scope - and recorded as a skip_reason so the journal can
                # price what it costs: refused signals still carry forward
                # returns, so "did the limiter block winners" stays an
                # answerable question rather than an invisible one.
                _corr_blocked, _corr_reason = (False, None)
                try:
                    from src.analytics.correlation import correlation_block
                    _corr_blocked, _corr_reason = correlation_block(
                        config, symbol,
                        {s: [p for _, p in price_history[s]] for s in price_history},
                        list(strategy.get_open_trades().keys()),
                    )
                except Exception as e:
                    logger.debug(f"correlation check skipped for {symbol}: {e}")

                if max_daily_entries and entries_triggered >= max_daily_entries:
                    skip_reason = "max_daily_entries"
                elif burst_max is not None and index >= burst_max:
                    skip_reason = "burst_throttle"
                elif _corr_blocked:
                    skip_reason = "correlation_limit"
                    logger.info(f"{symbol}: entry skipped - {_corr_reason}")
                else:
                    taken = _attempt_entry(
                        config, strategy, executor, symbol, price, cand["method"], cand["rsi"],
                        size_multiplier=burst_size, burst_note=burst_note,
                        context=_entry_context(
                            cand, spy_pct,
                            _window_pct_change(qqq_history),
                            _vs_vwap(vwap_acc, spy_history, "SPY"),
                            _vs_vwap(vwap_acc, qqq_history, "QQQ"),
                            breadth_state, regime_state,
                            _pct_vs(price, _vwap(vwap_acc, symbol)),
                            burst_size,
                        ),
                        market_data=market_data,
                        volume_history=volume_history,
                    )
                    if taken:
                        entries_triggered += 1
                        had_any_trades = True
                        pending_pullbacks.pop(symbol, None)
                    else:
                        skip_reason = "rejected_by_pre_entry_checks"

                # Journal EVERY signal, taken or not - the refused ones are
                # the control group any future ranking has to be judged
                # against. Recorded after the decision so it cannot affect it.
                sig_pct = cand["signal_pct"]
                signal_journal.record(
                    symbol=symbol, entry_method=cand["method"], price=price,
                    signal_pct=sig_pct,
                    spy_pct=spy_pct,
                    excess_vs_spy_pct=(round(sig_pct - spy_pct, 3)
                                       if sig_pct is not None and spy_pct is not None else None),
                    rvol=cand["rvol"],
                    spread_pct=cand["spread_pct"],
                    burst_width=burst_width,
                    **_opening_move_fields(screener_details, symbol),
                    **cand["cont"],
                    taken=taken, skip_reason=skip_reason,
                    qty=None, size_multiplier=burst_size,
                )

        if email_notifier is not None and getattr(email_notifier, "run_context", None):
            email_notifier.run_context["peak_signal_pct"] = day_peak_signal["value"]
            email_notifier.run_context["peak_signal_symbol"] = day_peak_signal["symbol"]
            # Always report the experiment's OUTCOME, including "it ran and took
            # nothing" and "it could not measure anything". On 2026-08-27 the
            # section rendered as nothing at all because it keyed off the trade
            # list, so a mode that had been starved looked identical to a mode
            # that was switched off.
            ob_cfg = _opening_burst_config(config)
            if ob_cfg:
                base = opening_state.get("baseline") or {}
                last = opening_state.get("last_price") or {}
                moves = sorted(
                    ((last[k] - base[k]) / base[k] * 100
                     for k in base if base.get(k) and last.get(k)),
                    reverse=True,
                )
                email_notifier.run_context["opening_burst_summary"] = {
                    "enabled": True,
                    "closed": bool(opening_state.get("done")),
                    "measured": len(base),
                    "taken": len(opening_state.get("taken") or []),
                    "threshold": ob_cfg.get("min_move_pct"),
                    "window": f"{ob_cfg.get('baseline_time')}-{ob_cfg.get('decide_by')}",
                    "best_move": moves[0] if moves else None,
                    "qualified": sum(1 for m in moves if m >= ob_cfg.get("min_move_pct", 0)),
                }

        # ---- scheduled reports (10:35 status, 16:00 close) ----
        _maybe_send_scheduled_reports(config, email_notifier, strategy, executor, market_data, et)

        # ---- day-completion checks ----
        open_trades = strategy.get_open_trades()
        if not open_trades and now >= entry_end and had_any_trades:
            # The BROKER is the authority on what is held, not strategy.trades.
            #
            # Ending the day here returns from run_trading_day, and the 16:00
            # time stop lives INSIDE this function - so anything the broker
            # still holds that tracking has lost sits untouched until the next
            # startup adopts it. On 2026-08-28 six positions were adopted at
            # 03:16, occupying six of ten concurrent slots before the bell.
            #
            # A position can fall out of tracking while the broker keeps shares:
            # a sell that partially fills, a rejected exit, or a restart between
            # the strategy popping the symbol and the order confirming. Trusting
            # the local view here is what lets that become an overnight hold.
            #
            # This costs one API call per session - it only runs at the moment
            # the day would otherwise end.
            try:
                still_held = executor.broker.get_positions() or {}
            except Exception as e:
                logger.warning(f"Could not verify positions with the broker ({e}) - "
                               f"not ending the day on an unverified view")
                still_held = {}

            if still_held:
                logger.warning(
                    f"NOT ending the day: tracking shows no open trades but the "
                    f"broker still holds {len(still_held)} position(s) "
                    f"({', '.join(sorted(still_held))}). Staying in the loop so "
                    f"the {time_stop_hour}:00 time stop can flatten them - "
                    f"otherwise they are held overnight and occupy concurrent "
                    f"slots at tomorrow's open."
                )
            else:
                logger.info("All trades closed. Sending daily summary...")
                finish_day("all_closed")
                return entries_triggered

        # Fire BEFORE the bell, not at it. This was `now.hour >= time_stop_hour`
        # with time_stop_hour 16, so the flatten ran at 16:00:00 or later - the
        # close itself. Market orders submitted then cannot fill in regular
        # hours, so they sat as status=new and queued for the NEXT session:
        # on 2026-09-02 CRM/CTSH/DKS were still open at the next morning's
        # startup with their unfilled 16:00 sells still pending, occupying
        # three of ten slots on a day that wanted them.
        #
        # time_stop_lead_minutes (default 5) moves it to 15:55, which leaves
        # five minutes of real liquidity for a market order to execute in.
        if now >= _stop_at:
            logger.info("Market closing, flattening all positions...")
            flattened = executor.flatten_all_positions()
            for symbol in flattened:
                strategy.trades.pop(symbol, None)
            finish_day("time_stop")
            return entries_triggered

        # The fast opening poll only applies while something is actually at
        # risk - an empty book has nothing to protect and does not need a
        # 3-second loop.
        poll_state["positions_open"] = bool(strategy.get_open_trades())
        poll_state["burst_open"] = bool(ob) and not opening_state.get("done")
        time.sleep(_poll_interval(
            config, market_data, check_interval, rest_interval, poll_state
        ))

def _write_price_log(symbol_price_log, et):
    """
    Dump this entry window's minute-by-minute price samples for every
    watched symbol (screener picks + default watchlist) to a dedicated,
    per-day log file - one block per symbol, chronological within each -
    so the actual price action behind a day's entries (or non-entries) can
    be eyeballed directly instead of dug out of trading.log. Separate file,
    doesn't touch trading.log.
    """
    date_str = datetime.now(et).strftime("%Y-%m-%d")
    path = os.path.join("logs", f"price_log_{date_str}.txt")
    try:
        os.makedirs("logs", exist_ok=True)
        with open(path, "a") as f:
            f.write(f"\n===== entry window price log - written {datetime.now(et)} =====\n")
            for symbol, samples in symbol_price_log.items():
                for ts, price in samples:
                    f.write(f"{symbol} {ts} {price}\n")
        logger.info(f"Wrote entry-window price log to {path}")
    except Exception as e:
        logger.error(f"Error writing price log: {e}")

def _check_three_bar_momentum(bars, require_acceleration=True):
    """
    True if `bars` holds 3 1-minute bars forming a clean upward thrust:
    all green (close > open), each close strictly higher than the last, and
    - when require_acceleration is on - each close-to-close gap strictly
    LARGER than the one before it.

    The acceleration requirement is the whole point of the signal. Without
    it, "3 rising closes" happily matches a move that is running out of
    steam, because a decelerating series still rises:

        9.51 -> 9.55 -> 9.56    gaps +0.04, +0.01   rising, but stalling
        9.51 -> 9.53 -> 9.58    gaps +0.02, +0.05   rising AND accelerating

    Both pass a plain "each close higher" test; only the second is a thrust
    still gaining speed. This signal exists to buy the second shape, and
    buying the first means buying into the top of a move that has already
    largely played out.

    Gaps are compared in absolute dollars rather than percent. Over three
    consecutive 1-minute bars of a single symbol the denominator barely
    moves, so the two orderings are equivalent in practice, and absolute
    keeps the log line directly comparable to the printed closes.
    """
    if len(bars) < 3:
        return False
    bars = list(bars)
    if any(b.get("close", 0) <= b.get("open", 0) for b in bars):
        return False

    closes = [b.get("close", 0) for b in bars]
    gaps = [curr - prev for prev, curr in zip(closes, closes[1:])]

    if any(gap <= 0 for gap in gaps):
        return False

    if require_acceleration:
        return all(nxt > cur for cur, nxt in zip(gaps, gaps[1:]))

    return True

def _check_reversal_bar_pattern(bars, count, direction):
    """
    Standalone bar-run check used only by the opening-reversal (U-shape)
    feature - deliberately NOT shared with _check_three_bar_momentum above,
    even though the underlying logic is similar, so this new/unproven
    feature can never touch or risk that already-live production check.

    True if the most recent `count` bars in `bars` are all the same color in
    `direction`, with each bar's close strictly further in that direction
    than the previous bar's close - i.e. a clean N-bar run, not just N
    same-colored bars chopping sideways.

    direction="down": all red (close < open), closes strictly falling - used
    to detect a "drastic drop" worth watching for use_opening_reversal_entry.
    direction="up": all green (close > open), closes strictly rising - used
    to confirm the reversal bounce off the tracked low is real.
    """
    if len(bars) < count:
        return False
    recent = list(bars)[-count:]
    if direction == "up":
        if any(b.get("close", 0) <= b.get("open", 0) for b in recent):
            return False
        return all(curr.get("close", 0) > prev.get("close", 0) for prev, curr in zip(recent, recent[1:]))
    else:
        if any(b.get("close", 0) >= b.get("open", 0) for b in recent):
            return False
        return all(curr.get("close", 0) < prev.get("close", 0) for prev, curr in zip(recent, recent[1:]))

def _advance_pullback_state(config, strategy, executor, symbol, price, pending_pullbacks,
                            symbol_rsi, market_data=None):
    """
    Advance one symbol's pullback-entry state machine by one price sample.
    Only called (from run_trading_day) once a rapid-increase thrust has
    already been detected for `symbol` and it hasn't been bought yet.
    Returns True if a position was opened this call (caller should then pop
    pending_pullbacks[symbol] - this function does NOT pop it on a successful
    entry, only on invalidation, so the caller has one consistent place to
    decide when tracking for this symbol is done).

    Resumption is checked BEFORE peak-tracking/invalidation: if price has
    bounced far enough off a tracked pullback low, that's a buy regardless of
    whether it also happens to exceed the prior peak - checking peak-update
    first would mean a resumption strong enough to blow straight past the old
    peak in one tick could skip the buy entirely, which defeats the point.
    """
    setup = pending_pullbacks[symbol]
    min_pullback_pct = config["trading"].get("pullback_min_pct", 0.1) / 100
    max_giveback_fraction = config["trading"].get("pullback_max_giveback_fraction", 0.75)
    resumption_confirm_pct = config["trading"].get("resumption_confirm_pct", 0.05) / 100

    if setup["pullback_low"] is not None:
        if price < setup["pullback_low"]:
            setup["pullback_low"] = price
        else:
            bounce_pct = (price - setup["pullback_low"]) / setup["pullback_low"]
            if bounce_pct >= resumption_confirm_pct:
                logger.info(
                    f"{symbol}: PULLBACK RESUMPTION signal - thrust +{setup['pct_change']:.2f}%, "
                    f"peak {setup['peak']:.2f}, pulled back to {setup['pullback_low']:.2f}, "
                    f"resumed to {price:.2f}"
                )
                return _attempt_entry(config, strategy, executor, symbol, price, "PULLBACK_RESUMPTION",
                              symbol_rsi, market_data=market_data)

    if price > setup["peak"]:
        setup["peak"] = price
        setup["pullback_low"] = None
        return False

    thrust_gain = setup["peak"] - setup["base_price"]
    if thrust_gain <= 0:
        del pending_pullbacks[symbol]
        return False

    giveback = setup["peak"] - price
    if giveback / thrust_gain >= max_giveback_fraction:
        logger.info(
            f"{symbol}: pullback gave back {giveback / thrust_gain * 100:.0f}% of the thrust's "
            f"gain (peak {setup['peak']:.2f} -> {price:.2f}) - setup invalidated"
        )
        del pending_pullbacks[symbol]
        return False

    if setup["pullback_low"] is None:
        retracement_pct = giveback / setup["peak"]
        if retracement_pct >= min_pullback_pct:
            setup["pullback_low"] = price
    return False

def _advance_reversal_state(config, strategy, executor, symbol, bar, pending_reversals,
                            symbol_rsi, market_data=None):
    """
    Advance one symbol's opening-reversal (U-shape) state machine by one bar.
    Only called once a "drastic drop" (opening_reversal_drop_bars consecutive
    red bars) has already been detected for `symbol` within
    opening_reversal_window_minutes of market open, and it hasn't been
    bought yet. Returns True if a position was opened this call.

    Confirmation mirrors use_three_bar_momentum's own logic (via the
    separate _check_reversal_bar_pattern helper) rather than a simple
    %-bounce threshold: requires opening_reversal_confirm_bars consecutive
    GREEN bars off the tracked low before buying - the same "don't chase a
    single tick, wait for a real run" discipline already used for the
    up-momentum signal,
    applied to confirming the reversal itself is real rather than a
    single-bar fakeout.

    Structurally mirrors _advance_pullback_state - tracking a trough instead
    of a peak - but deliberately simpler: pullback exists to avoid buying
    the TOP of an ALREADY-ESTABLISHED up-thrust, so it needs the extra
    giveback/max_giveback_fraction invalidation logic to tell "a normal
    pullback within a real breakout" apart from "a failed breakout that
    shouldn't be bought at all." That distinction doesn't have a clean
    equivalent here: catching the bounce off the trough IS the entire
    signal - there's no pre-existing trend being resumed to protect against
    overpaying for, so there's nothing analogous to invalidate against. A
    red bar breaking a forming bounce just falls out of the rolling
    confirm-bar window on its own - no separate invalidation branch needed.
    """
    setup = pending_reversals[symbol]
    price = bar.get("close", 0)
    confirm_bars = config["trading"].get("opening_reversal_confirm_bars", 5)

    if price < setup["low"]:
        setup["low"] = price

    setup["bounce_bar_history"].append(bar)

    if _check_reversal_bar_pattern(setup["bounce_bar_history"], confirm_bars, direction="up"):
        logger.info(
            f"{symbol}: OPENING REVERSAL signal - {confirm_bars} consecutive green bars off a "
            f"low of {setup['low']:.2f} (dropped from open {setup['base_price']:.2f}), now {price:.2f}"
        )
        return _attempt_entry(config, strategy, executor, symbol, price, "OPENING_REVERSAL",
                              symbol_rsi, market_data=market_data)

    return False

def _write_daily_summary_csv(config, executor, symbols, entries_triggered, starting_cash, market_data, et, filepath="logs/daily_summary.csv"):
    """
    Append one row to a running, never-overwritten master CSV - one row per
    trading DAY (as opposed to trade_history.csv's one row per trade) - for
    the "how did each day's settings/results compare" view: date, P&L,
    win rate, which entry method(s) were active, symbols watched, etc.
    """
    try:
        today_trades = [t for t in executor.trades_log if (t.get("exit_time") or "").startswith(datetime.now(et).strftime("%Y-%m-%d"))]
        total_pl = sum(t.get("pl", 0) for t in today_trades)
        wins = [t for t in today_trades if t.get("pl", 0) > 0]
        losses = [t for t in today_trades if t.get("pl", 0) < 0]
        win_rate = (len(wins) / len(today_trades) * 100) if today_trades else 0.0

        ending_cash = None
        try:
            ending_cash = float(market_data.broker.get_account().cash)
        except Exception as e:
            logger.debug(f"Could not read ending cash for daily summary: {e}")

        row = {
            "date": datetime.now(et).strftime("%Y-%m-%d"),
            "total_pl": round(total_pl, 2),
            "starting_cash": starting_cash,
            "ending_cash": ending_cash,
            "trades_count": len(today_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "entries_triggered": entries_triggered,
            "symbols_watched_count": len(symbols) if symbols else None,
            "symbols_watched": "|".join(symbols) if symbols else "",
            "symbols_traded": "|".join(sorted({t["symbol"] for t in today_trades})),
            "use_pullback_entry": config["trading"].get("use_pullback_entry"),
            "use_three_bar_momentum": config["trading"].get("use_three_bar_momentum"),
            "use_rsi_filter": config["trading"].get("use_rsi_filter"),
            "rapid_increase_config": f"{config['trading'].get('rapid_increase_pct')}% / {config['trading'].get('rapid_increase_lookback_minutes')}min",
            "final_stop_loss_pct": config["trading"].get("final_exit_loss_pct"),
            "first_scale_out_config": f"{config['trading'].get('first_exit_loss_pct')}% / {config['trading'].get('first_exit_pct', 0) * 100:.0f}%",
            "trailing_stop_pct": config["trading"].get("trailing_stop_pct"),
            "entry_window": f"{config['trading'].get('entry_window_start')}-{config['trading'].get('entry_window_end')}",
        }

        fieldnames = list(row.keys())
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        logger.info(f"Wrote daily summary row to {filepath}")
    except Exception as e:
        logger.error(f"Error writing daily summary CSV: {e}")

def reconcile_existing_positions(broker, strategy, executor):
    """
    Adopt any positions that already exist in the broker account but aren't
    tracked in strategy.trades. This happens whenever the process restarts
    (e.g. after the hosting environment reclaims the container) while
    positions are still open - a fresh Strategy() starts with empty in-memory
    tracking, so without this, the tiered stop-loss/trailing-stop logic in
    Strategy.process_bar never runs again for those positions until they're
    closed or the 4pm time-stop flatten sweeps everything.

    Uses the broker's own avg_entry_price, mirroring the same fallback
    executor.flatten_all_positions() already uses for positions it didn't
    itself open. The trailing-stop high-water mark is seeded with
    max(entry_price, current_price) rather than entry_price alone - a partial
    correction for not knowing the position's true peak since its original
    entry, since the broker doesn't expose intraday high-since-entry.

    Also seeds executor.open_entries[symbol] with the same entry price -
    Executor.submit_exit_order() reads P&L from that dict (populated
    separately from strategy.trades, only on a normal submit_entry_order()
    call), so without this a reconciled position's eventual exit would log
    an accurate STOP DECISION but a wrong "entry was None, P&L: $0.00" record.

    Also checks the broker's own order history for a filled SELL on this
    symbol since midnight ET today, and marks first_exit_done accordingly -
    a fresh TradeManager always starts with first_exit_done=False, so
    without this check, a position that already had its -0.5% scale-out
    fire before a restart would be eligible to fire ANOTHER first-exit
    tranche after reconciliation, over-trimming the position.
    """
    try:
        positions = broker.get_positions()
    except Exception as e:
        logger.error(f"Error reconciling existing positions: {e}")
        return

    et = pytz.timezone("America/New_York")
    today_start = datetime.now(et).replace(hour=0, minute=0, second=0, microsecond=0)

    for symbol, position in positions.items():
        if symbol in strategy.trades:
            continue
        try:
            raw_qty = float(position.qty)
            qty = int(abs(raw_qty))
            if qty <= 0:
                continue

            # REFUSE to adopt a short. This used to read
            # int(abs(float(position.qty))), which silently turned a short into
            # a long of the same size - and then every downstream rule ran
            # backwards on it. On 2026-08-28 CRWD at -39 @ 212.74 against a
            # ~228 market was read as +7.2% PROFIT and would have fired a
            # take-profit SELL, which shorts 39 more; worse, submit_exit_order
            # cancels working orders for the symbol first, so that sell would
            # also have cancelled the buy-to-cover queued against it.
            #
            # Refusing rather than managing it: this bot has no intent to be
            # short, so a short in the account is already evidence of a
            # different bug, and adopting it would mean silently running
            # long-only exit logic against a position it does not fit. The
            # 16:00 flatten is now side-aware (Executor.flatten_all_positions)
            # and will close it correctly, and no_shorting on the account
            # blocks new ones - so this is loud, visible, and still safe.
            if raw_qty < 0:
                logger.error(
                    f"{symbol}: REFUSING to adopt a SHORT position ({raw_qty:g} shares) "
                    f"on startup - this bot is long-only and its exit rules would run "
                    f"backwards on it. Left untracked and unmanaged; the 16:00 flatten "
                    f"closes it side-correctly. Investigate how a short was opened at all "
                    f"(phantom-entry path is the known cause), and close it by hand from "
                    f"the Alpaca dashboard if it should not be held."
                )
                continue

            avg_entry = getattr(position, "avg_entry_price", None)
            current_price_attr = getattr(position, "current_price", None)
            current_price = float(current_price_attr) if current_price_attr else None
            entry_price = float(avg_entry) if avg_entry else current_price

            if not entry_price or entry_price <= 0:
                logger.warning(
                    f"{symbol}: found an open position on startup but couldn't "
                    f"determine an entry price - leaving unmanaged, will still be "
                    f"caught by the time-stop/daily-loss-limit safety nets"
                )
                continue

            trade = TradeManager(symbol, entry_price, qty, strategy.config)
            if current_price and current_price > trade.highest_price:
                trade.highest_price = current_price
                trade.highest_since_entry = current_price
                trade.price_history = [entry_price, current_price]

            prior_sells = broker.get_filled_sell_orders_since(symbol, today_start)
            if prior_sells:
                trade.first_exit_done = True

            strategy.trades[symbol] = trade
            executor.open_entries[symbol] = entry_price
            executor.record_entry_meta(symbol, method="RECONCILED", rsi=None, entry_time=None)
            logger.info(
                f"{symbol}: adopted pre-existing position on startup - {qty} shares "
                f"@ {entry_price:.2f} (broker avg_entry_price) - resuming stop-loss/"
                f"trailing-stop management"
                + (f" (first_exit already fired today, {len(prior_sells)} prior sell(s) - won't re-arm it)" if prior_sells else "")
            )
        except Exception as e:
            logger.error(f"Error reconciling position for {symbol}: {e}")

def main():
    """Main trading loop"""
    try:
        # Load config
        config = load_config()
        logger.info("Config loaded")

        # Initialize broker
        broker = AlpacaBroker(paper=config["broker"]["paper_trading"])
        account = broker.get_account()
        logger.info(f"Connected to broker. Cash: ${account.cash}")

        # Real-time price stream. Alpaca's free tier delays the REST
        # historical endpoint ~15 minutes but carries live IEX data over the
        # WebSocket, so this is purely a delivery-mechanism change - no plan
        # upgrade involved. MarketDataManager falls back to REST per-symbol
        # whenever the stream has no fresh bar, so a failure here costs
        # freshness, never availability.
        price_stream = None
        if config["trading"].get("use_websocket_stream", True):
            price_stream = PriceStream(
                broker.api_key,
                broker.api_secret,
                feed=config["trading"].get("websocket_feed", "iex"),
                subscribe_trades=config["trading"].get("use_trade_ticks_for_entry", True),
                max_subscriptions=config["trading"].get("stream_max_subscriptions", 30),
            )

        # Initialize components
        market_data = MarketDataManager(broker, stream=price_stream)
        strategy = Strategy(config)
        executor = Executor(broker, config)
        # So a reconciled fill price reaches the exit rules, not just the
        # executor's own bookkeeping.
        executor.on_entry_price_corrected = strategy.correct_entry_price
        executor.entry_price_source = market_data.entry_price_source
        executor.on_entry_qty_corrected = strategy.correct_entry_qty
        reconcile_existing_positions(broker, strategy, executor)
        email_notifier = EmailNotifier(config)
        signal_journal = SignalJournal(config)

        logger.info("Paper trading bot started")
        logger.info(f"Trading hours: 9:30 AM - {config['trading']['time_stop_hour']}:00 ET")
        logger.info(
            f"Entry window: {config['trading']['entry_window_start']} - "
            f"{config['trading']['entry_window_end']} ET"
        )

        et = pytz.timezone("America/New_York")

        if config["trading"].get("use_daily_screener", False):
            # When the default universe is no longer auto-included, its names
            # must still be SCORED - otherwise turning the merge off would just
            # delete 50 candidates instead of making them compete.
            extra = ([] if config["trading"].get("merge_default_universe", True)
                     else config["trading"].get("stock_universe", []))
            screener = StockScreener(
                broker, config["trading"]["candidates_file"],
                extra_candidates=extra, config=config,
            )
        else:
            screener = None

        # Screener runs BEFORE the open, so the entry window starts with the
        # symbol list already in hand. Previously the loop waited on
        # is_market_open() and only then screened, which on 2026-08-19 meant
        # the screener started at 09:30:51 and finished at 09:32:15 - burning
        # the first 2.5 minutes of a 25-minute entry window before the bot
        # could take a single trade.
        screener_start = config["trading"].get("screener_start_time", "09:05")
        screener_hour, screener_minute = (int(x) for x in screener_start.split(":"))

        # Second pre-market stage: the earnings and QQQ lists, run in the buffer
        # between the screener finishing and the bell.
        augment_start = config["trading"].get("list_builder_start_time", "09:20")
        augment_hour, augment_minute = (int(x) for x in augment_start.split(":"))
        # The QQQ list gets its own, EARLIER slot. It scores every constituent
        # serially - 98 of them took 3m17s on 2026-08-27 - and needs nothing
        # that only exists late, unlike the earnings surprise.
        qqq_start = config["trading"].get("qqq_list_start_time", "09:10")
        qqq_hour, qqq_minute = (int(x) for x in qqq_start.split(":"))
        pending_qqq_done = False

        # Holds the pre-market screener result until the open consumes it.
        pending_selection = None
        # Whether the augmentation pass has already run against pending_selection.
        pending_augmented = False
        # Date of the last completed session. run_trading_day RETURNS once every
        # position is closed ("all_closed"), which on 2026-08-20 happened at
        # 10:14 ET - and the loop immediately re-screened and started a second
        # trading day for the same date. Harmless that day only because the
        # entry window had already passed, but it burned a screener run and
        # would re-arm entries entirely if a session ever finished early enough.
        last_session_date = None

        while True:
            now = datetime.now(et)

            if market_data.is_market_open():
                if last_session_date == now.date():
                    # Already traded today - wait for tomorrow rather than
                    # starting a second session on the same date. Scheduled
                    # reports still fire: the session finishing at 10:14 is
                    # exactly when a 16:00 report is easiest to lose.
                    _maybe_send_scheduled_reports(
                        config, email_notifier, strategy, executor, market_data, et
                    )
                    time.sleep(60)
                    continue

                if pending_selection is None:
                    # No pre-market run happened - the process started late,
                    # or was restarted mid-session. Screen now rather than
                    # trade a stale/empty list; this is the old behavior and
                    # costs entry-window time, hence the warning.
                    logger.warning(
                        "Market already open with no pre-market screener result "
                        "(late start or mid-session restart) - screening now, "
                        "which eats into the entry window"
                    )
                    pending_selection = select_symbols(config, screener, market_data)
                    pending_augmented = False

                if not pending_augmented:
                    # Either a late start, or the process came up after the
                    # list_builder_start_time slot and it never came round. Do it now: it
                    # costs entry-window time but an unaugmented list is a
                    # silently different strategy from the configured one.
                    # Earnings only. The QQQ stage takes minutes and this path
                    # runs with the market already open, where minutes are the
                    # scarcest thing there is.
                    pending_selection = _augment_selection(
                        config, screener, market_data, pending_selection, stages=("earnings",)
                    )
                    pending_augmented = True

                symbols, rsi_values = pending_selection
                pending_selection = None
                pending_augmented = False

                # Subscribe only once the day's symbol list is known. Started
                # here rather than at construction because the watchlist isn't
                # decided until the screener has run.
                # One retry at the bell if the stream wrote itself off before it.
                #
                # A pre-market give-up is a verdict reached on pre-market
                # evidence, and pre-market silence is not evidence of a broken
                # socket. On 2026-08-31 the stream gave up at 09:30:39 and the
                # whole session ran on REST ~15 min delayed, including every
                # entry decision, because nothing ever reconsidered it.
                if price_stream is not None and price_stream.clear_give_up():
                    logger.warning(
                        "Stream had given up before the open - retrying once now "
                        "that the market is live, since a pre-market verdict was "
                        "reached on pre-market silence. If it fails again the "
                        "session runs on REST as before."
                    )

                if price_stream is not None and not price_stream.is_running():
                    # Benchmarks go on the stream too, and LAST in priority.
                    #
                    # SPY was never subscribed, so get_latest_bar("SPY") always
                    # fell through to REST - which on the free tier is ~15
                    # minutes delayed, per market_data.get_latest_bar's own
                    # docstring. Every excess_vs_spy_pct therefore compared a
                    # LIVE symbol move against a DELAYED market move, and
                    # cf_rel_strength is built on that comparison. On 2026-08-26
                    # cf_rel_strength measured rho -0.344 against forward
                    # returns - the opposite sign to its +0.20 weight. A stale
                    # benchmark is a strong candidate for why.
                    #
                    # The sector ETFs have the identical problem, so they are
                    # subscribed for the identical reason.
                    #
                    # Last in priority on purpose: a benchmark must never
                    # displace a symbol the bot can actually trade. With
                    # num_stocks_to_trade at 15 against a 28-subscription
                    # budget there is room, but if there ever is not, the
                    # tradeable names win and the benchmarks fall back to REST
                    # exactly as they do today.
                    benchmarks = _benchmark_symbols(config, symbols)
                    price_stream.start(
                        list(dict.fromkeys(list(symbols) + benchmarks)),
                        priority=_stream_priority_with_indices(
                            config, stream_priority["symbols"], benchmarks),
                    )

                _set_run_context(config, email_notifier, symbols, price_stream)

                logger.info("Market is open, monitoring for signals...")
                try:
                    run_trading_day(
                        config, market_data, strategy, executor, symbols, rsi_values,
                        email_notifier, et, signal_journal,
                    )
                    last_session_date = datetime.now(et).date()
                finally:
                    if price_stream is not None:
                        logger.info(f"Price stream for the session: {price_stream.stats()}")
                        logger.info(f"Bar reads by source: {market_data.data_source_stats()}")
                        price_stream.stop()
                continue

            # Market closed. Run the screener once, inside the pre-market
            # window that starts at screener_start_time and ends at the open.
            market_open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
            screener_time_today = now.replace(
                hour=screener_hour, minute=screener_minute, second=0, microsecond=0
            )

            if (
                pending_selection is None
                and market_data.is_trading_day(now)
                and screener_time_today <= now < market_open_today
            ):
                logger.info(
                    f"===== PRE-MARKET: screening at {now:%H:%M:%S} ET, "
                    f"{(market_open_today - now).total_seconds() / 60:.0f} min ahead of the open ====="
                )
                pending_selection = select_symbols(config, screener, market_data)
                pending_augmented = False
                finished = datetime.now(et)
                if finished >= market_open_today:
                    logger.warning(
                        f"Screener ran past the open (finished {finished:%H:%M:%S} ET) - "
                        f"move screener_start_time earlier than {screener_start}"
                    )
                continue

            # Subscribe the stream BEFORE the open.
            #
            # It used to start only once is_market_open() went true, and this
            # loop sleeps 30s per iteration - so the socket could open up to 30
            # seconds after the bell and then still had to connect,
            # authenticate, subscribe and receive its first bars. The first
            # usable price realistically landed 09:30:35-09:31:00. That is
            # harmless for an entry window opening at 09:33 and fatal for the
            # opening-burst mode, which measures from 09:30:00 and must decide
            # by 09:32. Alpaca accepts pre-market subscriptions, so connecting
            # early costs nothing.
            prestart = config["trading"].get("stream_prestart_minutes", 0)
            # Deliberately NOT gated on pending_augmented.
            #
            # It was, on 2026-08-27, and that starved the opening-move
            # experiment completely. The QQQ list scores its constituents one at
            # a time; that morning it took 3m17s for 98 of them and the whole
            # augmentation finished at 09:31:50. The stream, waiting on it,
            # subscribed 110 seconds AFTER the 09:30 baseline instant with zero
            # bars delivered, so the burst measured 0 of 28 symbols and took
            # nothing.
            #
            # Waiting bought nothing even in principle. Stream slots are handed
            # out by screener rank, and the screener's own picks already fill the
            # budget - so an augmented symbol can never win a slot anyway. That
            # morning all 13 augmented names went to REST regardless. The gate
            # cost the entire experiment to protect an outcome that could not
            # happen.
            # Called HERE and again after every blocking pre-market stage.
            #
            # It used to be a single check per loop iteration, which is not the
            # same thing. On 2026-08-31 the iteration that would have subscribed
            # evaluated this at 09:10:22 - too early, the window opens at 09:26 -
            # and then entered the QQQ build IN THE SAME ITERATION, which ran for
            # 17m44s. By the next iteration it was 09:28 going on 09:30 and the
            # window had been slept through from the inside. The stream started
            # late at the bell, delivered nothing, and the burst measured 0 of 25.
            #
            # A window checked once per iteration is only as reliable as the
            # slowest thing that can run inside one. Checking after each stage
            # makes the subscribe happen the moment it becomes possible.
            def _try_prestart_stream():
                _now = datetime.now(et)
                if not (price_stream is not None and prestart
                        and pending_selection is not None
                        and market_data.is_trading_day(_now)
                        and market_open_today - timedelta(minutes=prestart) <= _now < market_open_today
                        and not price_stream.is_running()):
                    return
                symbols_now = pending_selection[0]
                bench = _benchmark_symbols(config, symbols_now)
                logger.info(
                    f"===== PRE-OPEN: subscribing the stream "
                    f"{(market_open_today - _now).total_seconds():.0f}s before the bell "
                    f"({len(symbols_now)} symbols + {len(bench)} benchmarks) ====="
                )
                try:
                    price_stream.start(
                        list(dict.fromkeys(list(symbols_now) + bench)),
                        priority=stream_priority["symbols"],
                    )
                except Exception as e:
                    logger.error(f"Pre-open stream start failed ({e}) - it will start at the open instead")

            # Every iteration, unconditionally. The calls after the QQQ and
            # earnings builds are EXTRA - they exist because those stages can
            # block for minutes - but this one is what runs on a normal morning
            # where both lists are already done and neither branch is entered.
            # Without it the subscribe would depend on a list build happening to
            # be in flight, which is not a schedule, it is a coincidence.
            _try_prestart_stream()

            # --- QQQ list, early slot ---
            qqq_time_today = now.replace(
                hour=qqq_hour, minute=qqq_minute, second=0, microsecond=0
            )
            if (
                pending_selection is not None
                and not pending_qqq_done
                and config["trading"].get("use_qqq_list", False)
                and market_data.is_trading_day(now)
                and qqq_time_today <= now < market_open_today
            ):
                logger.info(
                    f"===== PRE-MARKET: building the QQQ list at {now:%H:%M:%S} ET, "
                    f"{(market_open_today - now).total_seconds() / 60:.0f} min ahead of the open ====="
                )
                _t0 = time.monotonic()
                # Hard deadline, which this branch did not have and the
                # earnings branch did. On 2026-08-31 it ran for 17m44s - it
                # scores QQQ constituents one at a time and nothing capped it -
                # blocking this single-threaded loop from 09:10 to 09:28 and
                # taking the stream's pre-open subscribe window with it.
                #
                # Budgeted to leave the stream its full prestart window, not
                # merely to finish before the open: a list that lands at 09:29
                # is worthless if the socket it starved cannot then come up in
                # time. The screener's picks are the fallback, same as a
                # screener timeout.
                _stream_needs = market_open_today - timedelta(minutes=prestart or 0)
                _qqq_deadline = max(
                    5.0,
                    (_stream_needs - now).total_seconds()
                    - config["trading"].get("augment_deadline_buffer_seconds", 20),
                )
                _qqq_pool = ThreadPoolExecutor(max_workers=1)
                _qqq_future = _qqq_pool.submit(
                    _augment_selection, config, screener, market_data,
                    pending_selection, ("qqq",),
                )
                try:
                    pending_selection = _qqq_future.result(timeout=_qqq_deadline)
                    logger.info(
                        f"QQQ list finished in {time.monotonic() - _t0:.1f}s "
                        f"(deadline was {_qqq_deadline:.0f}s)"
                    )
                except FutureTimeoutError:
                    logger.warning(
                        f"QQQ list did not finish within {_qqq_deadline:.0f}s - abandoning it "
                        f"and keeping the screener's {len(pending_selection[0])} picks. "
                        f"A list that lands after the stream's subscribe window costs "
                        f"more than it adds."
                    )
                except Exception as e:
                    logger.error(f"QQQ list failed ({e}) - keeping the screener's picks")
                finally:
                    _qqq_pool.shutdown(wait=False)
                pending_qqq_done = True
                # The window may have opened while that ran.
                _try_prestart_stream()
                continue

            augment_time_today = now.replace(
                hour=augment_hour, minute=augment_minute, second=0, microsecond=0
            )

            if (
                pending_selection is not None
                and not pending_augmented
                and market_data.is_trading_day(now)
                and augment_time_today <= now < market_open_today
            ):
                logger.info(
                    f"===== PRE-MARKET: building the earnings list at {now:%H:%M:%S} ET, "
                    f"{(market_open_today - now).total_seconds() / 60:.0f} min ahead of the open ====="
                )
                # Hard deadline, for the same reason the screener has a
                # timeout. On 2026-08-27 this ran for 3m17s and finished at
                # 09:31:50, which did not merely delay the list - it blocked
                # this whole loop, so run_trading_day (and with it the
                # opening-move experiment, whose window is 09:30-09:32) could
                # not even START until the window had almost passed.
                #
                # An augmented list that arrives after the open is worth less
                # than the minutes it costs. Abandoning it keeps the screener's
                # picks, which is the same fallback a screener timeout uses.
                deadline = max(
                    5.0,
                    (market_open_today - now).total_seconds()
                    - config["trading"].get("augment_deadline_buffer_seconds", 20),
                )
                aug_pool = ThreadPoolExecutor(max_workers=1)
                aug_started = time.monotonic()
                aug_future = aug_pool.submit(
                    _augment_selection, config, screener, market_data, pending_selection,
                    ("earnings",),
                )
                try:
                    pending_selection = aug_future.result(timeout=deadline)
                    logger.info(
                        f"List build finished in {time.monotonic() - aug_started:.1f}s "
                        f"(deadline was {deadline:.0f}s)"
                    )
                except FutureTimeoutError:
                    logger.warning(
                        f"List build did not finish within {deadline:.0f}s - abandoning it "
                        f"and trading the screener's {len(pending_selection[0])} picks. "
                        f"The earnings/QQQ adds are dropped for today; a list that lands "
                        f"after the open costs more than it adds."
                    )
                except Exception as e:
                    logger.error(f"List build failed ({e}) - keeping the screener's picks")
                finally:
                    aug_pool.shutdown(wait=False)
                pending_augmented = True
                # Same reason as after the QQQ build: this stage blocks the loop,
                # and the subscribe window can open and close inside it.
                _try_prestart_stream()

                # The slot is deliberately tight (09:28, two minutes ahead of
                # the bell) so the earnings surprise has had time to publish.
                # That leaves little room for a slow Nasdaq fetch, so say so
                # when the build overruns rather than letting it be invisible:
                # entries begin at entry_window_start, and a list that lands
                # after that has already cost the first trades of the day.
                finished = datetime.now(et)
                if finished >= market_open_today:
                    logger.warning(
                        f"List build ran past the open (finished {finished:%H:%M:%S} ET) - "
                        f"move list_builder_start_time earlier than {augment_start}"
                    )
                continue

            time.sleep(30)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        executor.flatten_all_positions()
        executor.save_trades_log()
        _flush_journal_safely(signal_journal)
        email_notifier.send_daily_summary()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        # Alert FIRST. The cleanup below can itself fail (that is why it has a
        # bare except), and a crash nobody is told about is the worst of all
        # the failure modes here - the process is gone, the report never
        # sends, and positions may still be open at the broker.
        try:
            from src.notifications import alerts as _AL
            _AL.crashed(config, email_notifier, error=e)
        except Exception:
            pass
        try:
            executor.flatten_all_positions()
            executor.save_trades_log()
            _flush_journal_safely(signal_journal)
            email_notifier.send_daily_summary()
        except:
            pass
        raise

if __name__ == "__main__":
    main()
