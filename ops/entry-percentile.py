#!/usr/bin/env python3
"""
Where did each fill sit inside the price range around it?

The question this settles
-------------------------
On 2026-08-20, with REST prices only (~1.5 min stale), losing entries landed at
the 87th percentile of the surrounding half-hour and winning entries at the 43rd
- losers were systematically buying near the local top. The WebSocket has been
running since; this re-runs the same measurement so the two are comparable.

What each answer means, decided in advance so the result cannot be rationalised:

  losers moved 87% -> ~60%   latency was a real contributor and the stream is
                             earning its keep. Keep investing in freshness.

  losers still ~87%          the problem is SIGNAL DESIGN, not data. rapid_increase
                             fires AFTER a move has happened, so it structurally
                             buys the back half of it, and no amount of data
                             freshness fixes that. The fix would be a ceiling on
                             the signal size or a different entry trigger.

There is a third outcome worth naming: if losers and winners land at the SAME
percentile, entry timing is not what separates them at all, and the difference
lives somewhere else entirely.

Read-only. Reads trade_history.csv and fetches minute bars. Touches nothing the
bot trades on, and can be run at any time including mid-session.

    python3 ops/entry-percentile.py                    # every session on file
    python3 ops/entry-percentile.py --date 2026-08-27
    python3 ops/entry-percentile.py --window 15        # +/- minutes, default 15
"""

import argparse
import collections
import csv
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADE_FIELDS = [
    "date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
    "price_source", "signal_pct", "post_exit_pct", "post_exit_note", "entry_rsi",
    "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
    "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct", "list_source",
]
TRADE_FIELDS_HISTORY = [
    [c for c in TRADE_FIELDS if c != "list_source"],
    [
        "date", "symbol", "entry_time", "entry_price", "entry_method",
        "entry_rsi", "exit_time", "exit_price", "exit_reason",
        "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct",
    ],
]


def read_rows(path, fields, legacy=()):
    """Rows keyed by name, tolerating a stale header - see ops/session-metrics.py."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return []
    header = raw[0]
    is_header = bool(header) and header[0] == "date"
    body = raw[1:] if is_header else raw

    by_width = {}
    if is_header:
        by_width[len(header)] = header
    for sch in legacy or ():
        by_width.setdefault(len(sch), list(sch))
    by_width[len(fields)] = list(fields)

    out = []
    for row in body:
        if not row:
            continue
        schema = by_width.get(len(row))
        if schema:
            out.append(dict(zip(schema, row)))
    return out


def num(row, key):
    try:
        return float(row.get(key) or "")
    except (TypeError, ValueError):
        return None


def positions(trades):
    """
    Collapse exit rows into positions.

    A tiered winner emits three exit rows for ONE entry at ONE price. Counting
    rows would weight that fill three times and bias the whole measurement
    toward whatever the winners did.
    """
    by_key = collections.defaultdict(list)
    for t in trades:
        by_key[(t.get("symbol"), t.get("entry_time"))].append(t)
    out = []
    for (symbol, entry_time), rows in by_key.items():
        price = num(rows[0], "entry_price")
        if not symbol or not entry_time or not price:
            continue
        out.append({
            "date": rows[0].get("date"),
            "symbol": symbol,
            "entry_time": entry_time,
            "entry_price": price,
            "pl": sum(num(r, "pl") or 0.0 for r in rows),
            "source": rows[0].get("price_source") or "?",
            "method": rows[0].get("entry_method") or "?",
        })
    return out


def percentile_of(price, prices):
    """
    Where `price` sits within `prices`, 0-100.

    Share of surrounding prices at or below the fill. 100 means the fill was the
    highest price in the window - the worst possible moment to buy. 50 is the
    middle of the range.
    """
    if not prices:
        return None
    below = sum(1 for p in prices if p <= price)
    return below / len(prices) * 100


def window_prices(broker, symbol, when, minutes):
    """Closes of the minute bars within +/- `minutes` of `when`."""
    try:
        bars = broker.get_historical_bars(
            symbol, when - timedelta(minutes=minutes),
            when + timedelta(minutes=minutes), "1Min",
        )
    except Exception as e:
        print(f"  {symbol}: bars unavailable ({e})")
        return []
    rows = (bars or {}).get(symbol)
    if rows is None:
        return []
    cols = getattr(rows, "columns", None)
    if cols is not None:
        return [float(x) for x in rows["close"].tolist()] if "close" in cols else []
    return [float(b["close"]) for b in rows if isinstance(b, dict) and b.get("close")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="logs/trade_history.csv")
    ap.add_argument("--date", help="one session only")
    ap.add_argument("--window", type=int, default=15, help="+/- minutes, default 15")
    args = ap.parse_args()

    rows = read_rows(args.trades, TRADE_FIELDS, TRADE_FIELDS_HISTORY)
    pos = positions(rows)
    if args.date:
        pos = [p for p in pos if p["date"] == args.date]
    if not pos:
        sys.exit(f"no positions found{' for ' + args.date if args.date else ''}")

    from src.broker.alpaca_broker import AlpacaBroker
    broker = AlpacaBroker(paper=True)

    print("=" * 74)
    print(f"ENTRY PERCENTILE  +/-{args.window} min around each fill")
    print("=" * 74)
    print("100 = the fill was the HIGHEST price in the window (bought the top).")
    print("50  = the middle of the surrounding range.\n")

    scored = []
    for p in sorted(pos, key=lambda x: x["entry_time"]):
        try:
            when = datetime.fromisoformat(p["entry_time"])
        except ValueError:
            continue
        prices = window_prices(broker, p["symbol"], when, args.window)
        pct = percentile_of(p["entry_price"], prices)
        if pct is None:
            continue
        p["pct"] = pct
        p["bars"] = len(prices)
        scored.append(p)

    if not scored:
        sys.exit("no fills could be scored - no minute bars came back")

    win = [p for p in scored if p["pl"] > 0]
    lose = [p for p in scored if p["pl"] <= 0]
    mean = lambda v: sum(x["pct"] for x in v) / len(v) if v else None

    print(f"{'date':<12}{'sym':<7}{'src':<12}{'entry':>10}{'pctile':>9}{'P&L':>10}")
    print("-" * 74)
    for p in scored:
        print(f"{p['date']:<12}{p['symbol']:<7}{p['source']:<12}"
              f"{p['entry_price']:>10.2f}{p['pct']:>8.0f}%{p['pl']:>+10.2f}")

    print("-" * 74)
    print(f"  winners  n={len(win):<4} mean percentile {mean(win) or 0:.0f}%")
    print(f"  losers   n={len(lose):<4} mean percentile {mean(lose) or 0:.0f}%")

    # By price source: the whole point of the WebSocket was that streamed fills
    # should land better than REST ones.
    by_src = collections.defaultdict(list)
    for p in scored:
        by_src[p["source"]].append(p)
    if len(by_src) > 1:
        print("\n  by price source:")
        for src, group in sorted(by_src.items()):
            g_lose = [x for x in group if x["pl"] <= 0]
            print(f"    {src:<12} n={len(group):<4} all {mean(group):.0f}%"
                  + (f"   losers {mean(g_lose):.0f}%" if g_lose else ""))

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    ml, mw = mean(lose), mean(win)
    if ml is None:
        print("  No losing fills to measure.")
    elif len(lose) < 8:
        print(f"  Losers sit at {ml:.0f}% on n={len(lose)}. That is too few to")
        print("  conclude anything - the 2026-08-20 baseline was 87% and this")
        print("  needs a comparable sample before it can be compared to it.")
    elif ml >= 80:
        print(f"  Losers still at {ml:.0f}%, against 87% on 2026-08-20 (REST only).")
        print("  Data freshness did NOT fix it. This points at SIGNAL DESIGN:")
        print("  rapid_increase fires after a move has happened, so it buys the")
        print("  back half structurally. A ceiling on signal size or a different")
        print("  entry trigger is the lever - not faster prices.")
    elif ml <= 65:
        print(f"  Losers now at {ml:.0f}%, down from 87% on 2026-08-20 (REST only).")
        print("  Latency was a real contributor and the stream is earning its keep.")
    else:
        print(f"  Losers at {ml:.0f}%, between the 87% baseline and a clean result.")
        print("  Directionally better, not settled. Needs more sessions.")
    if mw is not None and ml is not None and abs(mw - ml) < 8:
        print(f"\n  NOTE: winners ({mw:.0f}%) and losers ({ml:.0f}%) land at nearly the")
        print("  SAME percentile. Entry timing is then not what separates them,")
        print("  and the difference lives somewhere else entirely.")


if __name__ == "__main__":
    main()
