"""
The two data files the exit-rule replay depends on.

WHY THIS EXISTS. Testing stop/breakeven/take-profit combinations by running
one config per live week does not work: 2026-08-27 made +$534 and 2026-08-31
lost -$546 on nearly identical settings, so the tape moves results by ~$1,000
a week while a breakeven tweak moves them by a few dollars a trade. Week-over
week comparison measures the weather. And a 4x4x4 grid at one week per cell
is fifteen months.

Replay removes both problems by scoring EVERY config against the SAME trades.
For that, two things have to be recorded that currently are not:

  trade_context.csv   one row per trade - the entry, the market state at
                      that moment, the stock's state, and the outcome. This
                      is the feature matrix behind questions like "when
                      cf_score is 85+, SPY is above VWAP and RVOL > 3x, what
                      happened?"

  trade_paths.csv     the price path from entry to exit, timestamped. THIS
                      IS THE ONE THAT MATTERS MOST, and the one thing no
                      existing file carries.

WHY THE PATH IS NOT OPTIONAL. mfe_pct and mae_pct record the best and worst
excursions but NOT WHICH CAME FIRST. "Peaked +1.2%, dipped -0.4%" and
"dipped -0.4%, then peaked +1.2%" produce identical rows and opposite
answers under any stop rule. The question the whole strategy turns on -
"probability of reaching +1% BEFORE -0.5%" - is a statement about ordering,
and it is unanswerable from extremes alone. With the path, every exit rule
can be re-run exactly as it would have fired.

WHAT REPLAY STILL CANNOT TELL YOU, stated up front so the numbers are not
over-trusted:
  - fills. A counterfactual exit is scored at the recorded price; a real one
    would have crossed a spread and taken slippage.
  - knock-on effects. A different stop frees a position slot and starts a
    re-entry cooldown earlier, which changes which LATER trades happen at
    all. Replay holds the entry set fixed.
  - entry rules. Only exits are replayable this way. Testing a new entry
    signal (pullbacks, say) needs a live session.
Within those limits it is exact, and it is enormously better than comparing
two weeks of different weather.
"""

import csv
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

CONTEXT_FILE = "logs/trade_context.csv"
PATHS_FILE = "logs/trade_paths.csv"

# One row per trade. Grouped the way the questions get asked, not the way the
# code happens to produce them.
CONTEXT_FIELDS = [
    # -- identity
    "trade_id", "date",
    # -- ENTRY
    "symbol", "entry_time", "entry_price", "position_size", "entry_method",
    "size_multiplier",
    # -- MARKET, as of the entry instant
    "spy_return", "qqq_return", "spy_vs_vwap", "qqq_vs_vwap",
    "market_breadth", "regime",
    # -- STOCK, as of the entry instant
    "stock_vs_vwap", "relative_volume", "momentum", "continuation_score",
    "sector_strength", "spread_pct",
    # -- TRADE outcome
    "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
    "realized_pnl", "realized_pnl_pct",
]

PATH_FIELDS = ["trade_id", "symbol", "date", "timestamp", "price", "gain_pct"]


def make_trade_id(symbol, entry_time):
    """Stable join key between the two files. Entry time is unique per
    symbol per day in practice - the re-entry cooldown guarantees a gap."""
    stamp = ""
    if entry_time:
        stamp = str(entry_time).replace(":", "").replace("-", "").replace(".", "")[:20]
    return f"{symbol}-{stamp}"


def _append(path, fields, rows):
    """Append rows, writing the header only when creating the file. Never
    raises into the trading loop - losing a research row must never be able
    to interrupt a session."""
    if not rows:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow(r)
    except Exception as e:
        logger.debug(f"trade_recorder: could not write {path}: {e}")


def record_path_samples(samples, path=PATHS_FILE):
    """
    Append price-path samples. Written as they are taken rather than
    buffered to the end of the trade, so a crash or a container reclaim
    costs the tail of one path instead of every path of the day.
    """
    _append(path, PATH_FIELDS, samples)


def record_context(row, path=CONTEXT_FILE):
    """Append one completed trade's context+outcome row."""
    _append(path, CONTEXT_FIELDS, [row])


def build_context_row(symbol, entry_meta, trade_record):
    """
    Assemble the one-row-per-trade record from what the executor already
    holds at exit: the context captured at ENTRY (stashed in entry_meta by
    main.py) plus the OUTCOME computed at exit.

    Missing values are left blank rather than defaulted to zero - a market
    reading that could not be taken is not a reading of zero, and the
    difference matters when these columns become filter conditions.
    """
    meta = entry_meta or {}
    ctx = meta.get("context") or {}
    entry_time = meta.get("entry_time") or trade_record.get("entry_time")

    def g(key):
        v = ctx.get(key)
        return "" if v is None else v

    return {
        "trade_id": make_trade_id(symbol, entry_time),
        "date": (str(trade_record.get("exit_time") or "")[:10]
                 or datetime.now().strftime("%Y-%m-%d")),
        "symbol": symbol,
        "entry_time": entry_time or "",
        "entry_price": trade_record.get("entry_price") if trade_record.get("entry_price") is not None else "",
        "position_size": trade_record.get("qty") if trade_record.get("qty") is not None else "",
        "entry_method": meta.get("method") or "",
        "size_multiplier": g("size_multiplier"),
        "spy_return": g("spy_return"),
        "qqq_return": g("qqq_return"),
        "spy_vs_vwap": g("spy_vs_vwap"),
        "qqq_vs_vwap": g("qqq_vs_vwap"),
        "market_breadth": g("market_breadth"),
        "regime": g("regime"),
        "stock_vs_vwap": g("stock_vs_vwap"),
        "relative_volume": g("relative_volume"),
        "momentum": g("momentum"),
        "continuation_score": g("continuation_score"),
        "sector_strength": g("sector_strength"),
        "spread_pct": g("spread_pct"),
        "mfe_pct": trade_record.get("mfe_pct") if trade_record.get("mfe_pct") is not None else "",
        "mae_pct": trade_record.get("mae_pct") if trade_record.get("mae_pct") is not None else "",
        "exit_time": trade_record.get("exit_time") or "",
        "exit_price": trade_record.get("exit_price") if trade_record.get("exit_price") is not None else "",
        "exit_reason": trade_record.get("exit_reason") or "",
        "realized_pnl": trade_record.get("pl") if trade_record.get("pl") is not None else "",
        "realized_pnl_pct": trade_record.get("pl_pct") if trade_record.get("pl_pct") is not None else "",
    }
