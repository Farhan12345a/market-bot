#!/usr/bin/env python3
"""
BE-outcome distribution: for trades that touched the breakeven trigger, what
happened next?

Queued in PENDING_WORK.md item 7 as "the next small tool" after the
2026-09-01 phantom-position fix: "after touching +0.15%, how often does a
position go on to +0.75% / +1% / -0.3% / back to entry."

WHAT THIS ANSWERS. breakeven_tiers arms a floor once a position's PEAK since
entry (mfe_pct) reaches trigger_pct, so price falling back to entry+floor_pct
afterward exits the WHOLE position rather than losing the round trip. That is
a bet: once a trade proves itself by +0.15% (or whatever trigger_pct is),
does exiting cheaply on any pullback protect real gains, or does it scratch
trades that would have kept running? This tool reads trade_history.csv for
every trade whose mfe_pct cleared the trigger and reports what it actually
did, split four ways (see BUCKETS below) plus the full exit_reason breakdown.

WHAT THIS CANNOT ANSWER. mfe_pct and mae_pct are each the single worst/best
excursion over the WHOLE trade - they do not record which happened first.
"Peaked +0.4%, dipped to -0.2%, closed +0.1%" and "dipped to -0.2%, ran to
+0.4%, closed +0.1%" produce identical mfe_pct/mae_pct/pl_pct rows. So the
buckets below are read from PEAK (mfe_pct) and FINAL OUTCOME (exit_reason,
pl_pct) - real, decision-relevant numbers - not from a reconstructed path.
The BE-outcome question ("did it fall to -0.3% AFTER touching +0.15%, or was
the dip first") needs order-of-events data this CSV does not carry - noted
here rather than silently assumed away.

    python3 ops/be-outcomes.py                     # trigger 0.15, all history
    python3 ops/be-outcomes.py --trigger 0.5        # the normal session's own floor
    python3 ops/be-outcomes.py --since 2026-08-26 --symbols HOOD,CRM

Stdlib only.
"""

import argparse
import collections
import csv
import math
import os
import sys

# Copied from ops/session-metrics.py rather than imported - ops/*.py files are
# hyphenated (not valid Python module names) and every script here is
# deliberately standalone. Keep this block in sync if TRADE_FIELDS changes.
TRADE_FIELDS = [
    "date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
    "price_source", "signal_pct", "post_exit_pct", "post_exit_note", "entry_rsi",
    "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
    "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct",
    "list_source",
]
TRADE_FIELDS_HISTORY = [
    # Every schema this CSV has ever been written under, recovered from git
    # (executor.py's fieldnames list at 544ce31 / c3df889 / 5691a2d /
    # 62605f4 / 1487f3b / 66c5d87). Rows are matched by WIDTH, so a schema
    # missing from this list is not mis-parsed - it is silently DROPPED.
    #
    # That was happening: only the 21-wide legacy was declared, so every
    # width-17 and width-18 row was discarded. On the live history that is
    # 124 of 378 rows - a third of the record - and both of those schemas
    # DO carry mfe_pct and mae_pct, which is exactly the column the exit
    # tuning depends on.
    #
    # 21: current minus list_source
    [c for c in TRADE_FIELDS if c != "list_source"],
    # 18: before signal_pct and the post-exit columns
    ["date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
     "price_source", "entry_rsi", "mfe_pct", "mae_pct", "exit_time", "exit_price",
     "exit_reason", "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct"],
    # 17: before price_source
    ["date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
     "entry_rsi", "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
     "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct"],
    # 15: before excursions were recorded at all - no mfe/mae to recover
    ["date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
     "entry_rsi", "exit_time", "exit_price", "exit_reason", "stop_loss_used",
     "exit_rsi", "qty", "pl", "pl_pct"],
    # 14: before burst_logic
    ["date", "symbol", "entry_time", "entry_price", "entry_method", "entry_rsi",
     "exit_time", "exit_price", "exit_reason", "stop_loss_used", "exit_rsi",
     "qty", "pl", "pl_pct"],
]


def read_rows(path, fields, legacy=()):
    """Rows keyed by name, tolerating a stale header - see session-metrics.py
    for the full explanation. Rows are matched by WIDTH because both schemas
    grow by insertion, not appending, so a positional read would misalign
    every column after the insertion point for an older row."""
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
    for row in body:
        if not row:
            continue
        widths[len(row)] += 1
        schema = by_width.get(len(row))
        if schema:
            rows.append(dict(zip(schema, row)))
    return rows, widths


def num(row, key):
    try:
        v = float(row.get(key) or "")
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def pct(n, d):
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else f"{n}/0 (n/a)"


def group_stats(rows):
    n = len(rows)
    pls = [num(r, "pl") for r in rows]
    pls = [p for p in pls if p is not None]
    pl_pcts = [num(r, "pl_pct") for r in rows]
    pl_pcts = [p for p in pl_pcts if p is not None]
    return {
        "n": n,
        "total_pl": sum(pls) if pls else None,
        "mean_pl_pct": (sum(pl_pcts) / len(pl_pcts)) if pl_pcts else None,
        "win_rate": (sum(1 for p in pl_pcts if p > 0) / len(pl_pcts)) if pl_pcts else None,
    }


def fmt_stats(label, s):
    total_pl = f"${s['total_pl']:+.2f}" if s["total_pl"] is not None else "n/a"
    mean_pl = f"{s['mean_pl_pct']:+.3f}%" if s["mean_pl_pct"] is not None else "n/a"
    win = f"{100 * s['win_rate']:.0f}%" if s["win_rate"] is not None else "n/a"
    return f"{label:<28} n={s['n']:<5} total {total_pl:<12} mean {mean_pl:<10} win rate {win}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", default="logs/trade_history.csv")
    ap.add_argument("--trigger", type=float, default=0.15,
                     help="mfe_pct floor a trade must clear to count as 'touched BE' (default 0.15, the opening-burst tier)")
    ap.add_argument("--since", help="only trades on/after this date (YYYY-MM-DD)")
    ap.add_argument("--symbols", help="comma-separated symbols to restrict to")
    args = ap.parse_args()

    trades, widths = read_rows(args.trades, TRADE_FIELDS, TRADE_FIELDS_HISTORY)
    if not trades:
        sys.exit(f"no data in {args.trades}")
    if args.since:
        trades = [t for t in trades if (t.get("date") or "") >= args.since]
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        trades = [t for t in trades if (t.get("symbol") or "").upper() in wanted]
    unidentified = sum(c for w, c in widths.items()
                        if w not in (len(TRADE_FIELDS), *(len(s) for s in TRADE_FIELDS_HISTORY)))
    if unidentified:
        print(f"NOTE: {unidentified} row(s) had an unrecognized column count and were skipped",
              file=sys.stderr)

    touched = [t for t in trades if (num(t, "mfe_pct") or -999) >= args.trigger]
    untouched = [t for t in trades if (num(t, "mfe_pct") or -999) < args.trigger]

    print(f"=== BE-OUTCOME DISTRIBUTION: trades that touched +{args.trigger:g}% mfe ===")
    print(f"{len(touched)} of {len(trades)} trades cleared the trigger "
          f"({len(untouched)} never did, or had no mfe_pct recorded)\n")

    if not touched:
        print("Nothing to report - no trade in range reached this trigger. "
              "Try a lower --trigger or a wider --since.")
        return

    # The four questions from PENDING_WORK item 7, each independent (a trade
    # can answer yes to more than one - reaching +1% implies reaching +0.75%).
    reached_075 = [t for t in touched if (num(t, "mfe_pct") or -999) >= 0.75]
    reached_100 = [t for t in touched if (num(t, "mfe_pct") or -999) >= 1.0]
    fell_neg03 = [t for t in touched if (num(t, "pl_pct") or 999) <= -0.3]
    scratched = [t for t in touched if (t.get("exit_reason") or "") == "BREAKEVEN_STOP"]

    print("--- Of the trades that touched the trigger, how many also... ---")
    print(f"  ...ran to +0.75% or more (mfe_pct):  {pct(len(reached_075), len(touched))}")
    print(f"  ...ran to +1.00% or more (mfe_pct):  {pct(len(reached_100), len(touched))}")
    print(f"  ...still closed at -0.3% or worse:   {pct(len(fell_neg03), len(touched))}")
    print(f"  ...exited via BREAKEVEN_STOP:        {pct(len(scratched), len(touched))}")

    print("\n--- Outcome group stats (pl_pct / total pl / win rate) ---")
    print(fmt_stats("ran to >=1.00%", group_stats(reached_100)))
    print(fmt_stats("ran to >=0.75% (incl. above)", group_stats(reached_075)))
    print(fmt_stats("closed <=-0.3% despite trigger", group_stats(fell_neg03)))
    print(fmt_stats("scratched (BREAKEVEN_STOP)", group_stats(scratched)))
    print(fmt_stats("ALL trades that touched trigger", group_stats(touched)))
    print(fmt_stats("trades that never touched trigger", group_stats(untouched)))

    print("\n--- Full exit_reason breakdown, trades that touched the trigger ---")
    by_reason = collections.defaultdict(list)
    for t in touched:
        by_reason[t.get("exit_reason") or "(unknown)"].append(t)
    for reason, rows in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(fmt_stats(reason, group_stats(rows)))

    if scratched:
        s = group_stats(scratched)
        note = ("close to the intended +0.05% floor - the BE rule is doing its "
                "documented job" if s["mean_pl_pct"] is not None and 0 <= s["mean_pl_pct"] <= 0.15
                else "NOT close to the +0.05% floor - check breakeven_floor_pct and the "
                     "exit spread before trusting this rule's cost")
        print(f"\nBREAKEVEN_STOP mean pl_pct is {s['mean_pl_pct']:+.3f}% - {note}")


if __name__ == "__main__":
    main()
