#!/usr/bin/env python3
"""
Was the breadth halt right to fire?

breadth_halt stops new entries once the watchlist mean move falls below a
floor (-0.3%, checked at 09:45 ET), on the assumption that a weak first
fifteen minutes implies a weak session. That assumption is not measured
anywhere else - the signal journal keeps recording signals (taken or not)
straight through a halt, so the evidence of what happened AFTER is sitting
in the file whether or not a trade was placed on it.

This asks the direct question: for signals that fired at or after the halt
time, what did they actually do? If they mostly rose, the halt is costing
real winners and should be loosened (a later check_time, or a smaller floor).
If they mostly fell or went nowhere, it's doing its job.

Also breaks out any symbols named on the command line, since "but PLTR and
ADBE were trending after 10am" is a claim about specific names, not the
average - the average and the anecdote can both be true at once.

Stdlib only, runs inside the VPS venv with no installs.

    python3 ops/breadth-counterfactual.py --date 2026-09-01
    python3 ops/breadth-counterfactual.py --date 2026-09-01 --after 09:45
    python3 ops/breadth-counterfactual.py --date 2026-09-01 --symbols PLTR,ADBE
"""

import argparse
import csv
import math
import sys

JOURNAL_FIELDS = [
    "date", "signal_time", "symbol", "entry_method", "price",
    "signal_pct", "excess_vs_spy_pct", "spy_pct", "rvol", "spread_pct",
    "burst_width",
    "opening_hit_rate", "opening_avg_gain", "opening_sessions",
    "opening_efficiency", "opening_directional",
    "cf_efficiency", "cf_rel_strength", "cf_vol_accel", "cf_vwap_pos",
    "cf_exhaustion", "cf_breakout", "cf_rvol", "cf_spread", "cf_vwap",
    "cf_sector_strength", "cf_sector_etf",
    "cf_score",
    "taken", "skip_reason", "qty", "size_multiplier",
    "price_15min", "pct_15min", "price_30min", "pct_30min",
]

# v1 schema, before the opening-move and continuation columns - present for
# the same reason ops/analyze-journal.py carries it: rows written before
# 2026-08-26 are this width and must be readable, not silently dropped.
JOURNAL_FIELDS_V1 = [
    "date", "signal_time", "symbol", "entry_method", "price",
    "signal_pct", "excess_vs_spy_pct", "spy_pct", "rvol", "spread_pct",
    "burst_width",
    "taken", "skip_reason", "qty", "size_multiplier",
    "price_15min", "pct_15min", "price_30min", "pct_30min",
]


def read_rows(path):
    rows = []
    with open(path, newline="") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return rows
    header = raw[0]
    is_header = bool(header) and header[0] == "date" and "signal_time" in header
    body = raw[1:] if is_header else raw
    for row in body:
        if not row:
            continue
        w = len(row)
        if w == len(JOURNAL_FIELDS):
            rows.append(dict(zip(JOURNAL_FIELDS, row)))
        elif w == len(JOURNAL_FIELDS_V1):
            rows.append(dict(zip(JOURNAL_FIELDS_V1, row)))
    return rows


def as_float(v):
    if v is None:
        return None
    v = v.strip()
    if v == "":
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def hhmm(signal_time):
    """'HH:MM' from signal_time, which is a full ISO datetime (see
    signal_journal.py: row["signal_time"] = now.isoformat()), NOT a bare
    time - "2026-09-01T09:45:12.345-04:00". Taking the first 5 characters of
    that is the year, not the hour, and silently put every row in "after" no
    matter what --after was, on 2026-09-01's data. Split on the date/time
    separator first.
    """
    s = signal_time or ""
    if "T" in s:
        s = s.split("T", 1)[1]
    elif " " in s:
        s = s.split(" ", 1)[1]
    return s[:5]


def summarize(label, rows, horizon_field):
    vals = [as_float(r.get(horizon_field)) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        print(f"  {label:<22} n=0")
        return
    mean = sum(vals) / len(vals)
    hits = sum(1 for v in vals if v > 0)
    print(f"  {label:<22} n={len(vals):<4} mean {mean:+.3f}%   hit rate {100*hits/len(vals):.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/signal_journal.csv")
    ap.add_argument("--date", required=True)
    ap.add_argument("--after", default="09:45", help="HH:MM ET - the halt/check time")
    ap.add_argument("--symbols", default="", help="comma-separated symbols to break out individually")
    args = ap.parse_args()

    rows = [r for r in read_rows(args.file) if r.get("date") == args.date]
    if not rows:
        sys.exit(f"no rows for {args.date} in {args.file}")

    before = [r for r in rows if hhmm(r["signal_time"]) < args.after]
    after = [r for r in rows if hhmm(r["signal_time"]) >= args.after]

    print(f"{'='*72}")
    print(f"BREADTH COUNTERFACTUAL  {args.date}  (split at {args.after} ET)")
    print(f"{'='*72}")
    print(f"signals before {args.after}: {len(before)}")
    print(f"signals at/after {args.after}: {len(after)}   <- this is the halt's opportunity cost")
    print()

    if not after:
        print("Nothing fired after the split time - the halt cost nothing, there was")
        print("no later signal to have taken. This is itself an answer: on this day")
        print("the tape did not recover, so the halt did not cost missed winners.")
        return

    print("15-min forward return, signals at/after the split:")
    summarize("all", after, "pct_15min")
    summarize("taken", [r for r in after if r.get("taken") == "1"], "pct_15min")
    summarize("skipped", [r for r in after if r.get("taken") != "1"], "pct_15min")
    print()
    print("30-min forward return, signals at/after the split:")
    summarize("all", after, "pct_30min")
    summarize("taken", [r for r in after if r.get("taken") == "1"], "pct_30min")
    summarize("skipped", [r for r in after if r.get("taken") != "1"], "pct_30min")

    named = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if named:
        print()
        print(f"Named symbols ({', '.join(named)}), every signal at/after {args.after}:")
        for sym in named:
            sym_rows = [r for r in after if r.get("symbol", "").upper() == sym]
            if not sym_rows:
                print(f"  {sym:<6} no signals at/after {args.after} today")
                continue
            for r in sorted(sym_rows, key=lambda r: r["signal_time"]):
                p15 = as_float(r.get("pct_15min"))
                p30 = as_float(r.get("pct_30min"))
                p15s = f"{p15:+.2f}%" if p15 is not None else "  n/a "
                p30s = f"{p30:+.2f}%" if p30 is not None else "  n/a "
                taken = "TAKEN  " if r.get("taken") == "1" else "skipped"
                print(f"  {sym:<6} {hhmm(r['signal_time'])}  {taken}  "
                      f"signal {as_float(r.get('signal_pct')) or 0:+.2f}%  "
                      f"-> 15m {p15s}  30m {p30s}")

    print()
    print(f"{'='*72}")
    print("HOW TO READ THIS")
    print(f"{'='*72}")
    print("If 'after' mean/hit rate is clearly POSITIVE and beats what the halt")
    print("was protecting against, the -0.3% floor or the 09:45 check_time is too")
    print("tight - loosen one of them. If it is flat or negative, the halt earned")
    print("its keep today. One day is one data point either way - watch this over")
    print("several sessions before changing the threshold on it.")


if __name__ == "__main__":
    main()
