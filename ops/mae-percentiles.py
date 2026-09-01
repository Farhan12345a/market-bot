#!/usr/bin/env python3
"""
MAE-percentile store: the analysis layer PENDING_WORK.md item 3 (dynamic,
ATR/MAE-based stops) calls for, built on data that already exists.

WHAT THIS IS. For every symbol (and pooled across all of them), the
distribution of mae_pct - max adverse excursion, the worst drawdown a
position reached before exiting, logged per trade since 2026-08-24 (see
TradeManager.excursions in src/strategy/strategy.py). Read as: "the 75th
percentile MAE for HOOD is -0.62%" means 75% of HOOD's recorded trades never
drew down past -0.62% before whatever happened next (a recovery, an exit).
That is the number a percentile-based dynamic stop (PENDING_WORK item 3)
would place itself beyond, instead of one flat final_exit_loss_pct for every
symbol regardless of how that symbol actually trades.

WHAT THIS DELIBERATELY DOES NOT DO: wire a stop to any of this. Two reasons.

  1. Sample size. At num_stocks_to_trade=15 and a use_dynamic_universe pool
     that reshuffles daily, most symbols have a handful of trades apiece -
     nowhere near enough to trust a PER-SYMBOL 90th or 95th percentile. The
     pooled ("ALL SYMBOLS") row is the one with enough n to mean anything
     today; per-symbol rows are printed for symbols that recur often, with
     their n shown so nobody mistakes 4 trades for a distribution.

  2. The other half of item 3 - "1-min ATR" - is not built. What exists
     today (_get_volatility_percentile in stock_screener.py) is a 5-bucket
     ladder (10/30/50/75/95), not a true percentile - already flagged
     unfixed in PENDING_WORK.md item 0d. A stop that combines MAE percentile
     with a volatility measure that is itself five buckets wide would be
     built on a proxy for a proxy. Fix that first, or compute ATR properly
     from minute bars (a new data pull, not something this CSV-only script
     can do), before any of this decides where a stop sits.

    python3 ops/mae-percentiles.py                       # pooled + all symbols with n >= --min-n
    python3 ops/mae-percentiles.py --symbol HOOD          # one symbol only, any n
    python3 ops/mae-percentiles.py --min-n 10 --since 2026-08-24

Stdlib only.
"""

import argparse
import collections
import csv
import math
import os
import sys

# Copied from ops/session-metrics.py - see ops/be-outcomes.py for why these
# scripts duplicate this block rather than importing it (hyphenated filenames
# aren't importable modules, and every ops/*.py here is deliberately standalone).
TRADE_FIELDS = [
    "date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
    "price_source", "signal_pct", "post_exit_pct", "post_exit_note", "entry_rsi",
    "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
    "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct",
    "list_source",
]
TRADE_FIELDS_HISTORY = [
    [c for c in TRADE_FIELDS if c != "list_source"],
]


def read_rows(path, fields, legacy=()):
    if not os.path.exists(path):
        return [], {}
    with open(path, newline="") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return [], {}
    header = raw[0]
    is_header = bool(header) and header[0] == "date"
    body = raw[1:] if is_header else raw
    old = header if is_header else None
    by_width = {}
    if old:
        by_width[len(old)] = old
    for sch in legacy or ():
        by_width.setdefault(len(sch), list(sch))
    by_width[len(fields)] = list(fields)
    rows, widths = [], collections.Counter()
    for r in body:
        if not r:
            continue
        widths[len(r)] += 1
        schema = by_width.get(len(r))
        if schema:
            rows.append(dict(zip(schema, r)))
    return rows, widths


def num(row, key):
    try:
        v = float(row.get(key) or "")
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def percentile(sorted_vals, p):
    """
    Nearest-rank percentile of an ASCENDING-sorted, non-empty list.

    "the P-th percentile is X" means: P% of the values are <= X. For MAE
    (always <= 0, more negative = worse), that reads directly as "P% of
    trades drew down no deeper than X" - which is why percentiles_of_mae
    below asks for (100 - P) here to answer "P% of trades stayed within X".
    """
    n = len(sorted_vals)
    k = max(1, min(n, math.ceil(p / 100 * n)))
    return sorted_vals[k - 1]


def mae_bands(mae_values, bands=(50, 75, 90, 95)):
    """{P: level such that P% of trades never drew down deeper than it}."""
    vals = sorted(v for v in mae_values if v is not None)
    if not vals:
        return {}
    return {p: round(percentile(vals, 100 - p), 3) for p in bands}


def fmt_row(label, n, bands, min_n):
    if n < min_n:
        return f"{label:<10} n={n:<4} (below --min-n {min_n}, not shown - too few trades to trust a percentile)"
    parts = ", ".join(f"P{p}={bands.get(p):+.3f}%" for p in sorted(bands))
    return f"{label:<10} n={n:<4} {parts}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", default="logs/trade_history.csv")
    ap.add_argument("--since", help="only trades on/after this date (YYYY-MM-DD)")
    ap.add_argument("--symbol", help="restrict to one symbol (bypasses --min-n)")
    ap.add_argument("--min-n", type=int, default=15,
                     help="minimum trades before a per-symbol row is shown (default 15)")
    args = ap.parse_args()

    trades, widths = read_rows(args.trades, TRADE_FIELDS, TRADE_FIELDS_HISTORY)
    if not trades:
        sys.exit(f"no data in {args.trades}")
    if args.since:
        trades = [t for t in trades if (t.get("date") or "") >= args.since]
    unidentified = sum(c for w, c in widths.items()
                        if w not in (len(TRADE_FIELDS), *(len(s) for s in TRADE_FIELDS_HISTORY)))
    if unidentified:
        print(f"NOTE: {unidentified} row(s) had an unrecognized column count and were skipped",
              file=sys.stderr)

    with_mae = [t for t in trades if num(t, "mae_pct") is not None]
    print(f"=== MAE PERCENTILES: {len(with_mae)} of {len(trades)} trades carry mae_pct "
          f"(logged since 2026-08-24) ===\n")
    if not with_mae:
        print("Nothing to report - no trade in range has mae_pct recorded.")
        return

    if args.symbol:
        sym = args.symbol.upper()
        vals = [num(t, "mae_pct") for t in with_mae if (t.get("symbol") or "").upper() == sym]
        vals = [v for v in vals if v is not None]
        print(fmt_row(sym, len(vals), mae_bands(vals), min_n=1))
        return

    pooled = [num(t, "mae_pct") for t in with_mae]
    print(fmt_row("ALL", len(pooled), mae_bands(pooled), min_n=1))

    print(f"\nPer symbol (n >= {args.min_n}; a shallower sample is not shown - see --min-n):")
    by_symbol = collections.defaultdict(list)
    for t in with_mae:
        v = num(t, "mae_pct")
        if v is not None:
            by_symbol[t.get("symbol") or "?"].append(v)

    shown = 0
    for sym, vals in sorted(by_symbol.items(), key=lambda kv: -len(kv[1])):
        if len(vals) < args.min_n:
            continue
        print(fmt_row(sym, len(vals), mae_bands(vals), min_n=args.min_n))
        shown += 1
    if not shown:
        thin = sorted(by_symbol.items(), key=lambda kv: -len(kv[1]))[:5]
        print(f"  (none - every symbol has fewer than {args.min_n} trades. "
              f"Thickest so far: "
              + ", ".join(f"{s} n={len(v)}" for s, v in thin) + ")")


if __name__ == "__main__":
    main()
