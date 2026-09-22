#!/usr/bin/env python3
"""
Splits signal/trade outcomes by which mechanism surfaced the symbol that day:
the general screener's num_stocks_to_trade picks, or the QQQ-trend list
(use_qqq_list / qqq_list_top_n, Nasdaq-100 names added only on days QQQ
itself is trending up - see config.yaml's comment on qqq_list_top_n).

WHY THIS EXISTS. The standing question from the SPY/QQQ regime discussion
was "does tying selection to the index help, or does it just filter out the
idiosyncratic movers rapid_increase exists to catch." cf_rel_strength (a
stock's move in excess of SPY) already measures this per-signal and its
overall correlation with forward returns is close to zero - but that number
pools QQQ-list names (deliberately index-trend-conditioned) with general-
screener names (deliberately NOT) into one bucket. This script un-pools them,
so the two very different selection mechanisms can be judged separately
instead of averaged into a number that describes neither.

A symbol's source is NOT a fixed property - MARA can come from the general
screener one day and (hypothetically) not exist in the QQQ-100 at all, while
NVDA might be a QQQ-list add today and a general-screener pick some other
day if it independently qualifies. So the source is read fresh from each
day's own "List augmentation [qqq]: N watched -> M (+K: SYM, SYM, ...)" line
in service-log.txt - which only exists for days pulled with
ops/export-daily-logs.sh, and only from 2026-09-21 onward (the first day
that log line was captured this way).

    python3 ops/qqq-list-origin.py                    # every day in logs/daily/
    python3 ops/qqq-list-origin.py logs/daily/2026-09-21

Stdlib only.
"""
import csv
import glob
import os
import re
import sys
from collections import defaultdict

AUG_RE = re.compile(r"List augmentation \[qqq\]:.*\(\+\d+: (.+)\)")


def qqq_symbols_for_day(day_dir):
    log = os.path.join(day_dir, "service-log.txt")
    if not os.path.exists(log):
        return set()
    with open(log, errors="replace") as f:
        for line in f:
            m = AUG_RE.search(line)
            if m:
                return {s.strip() for s in m.group(1).split(",")}
    return set()


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def main():
    days = sys.argv[1:] or sorted(glob.glob("logs/daily/*"))
    if not days:
        print("No logs/daily/<date> directories found or given.")
        return 1

    buckets = defaultdict(list)  # (file, source) -> rows
    days_with_qqq_data = 0
    for day_dir in days:
        qqq_syms = qqq_symbols_for_day(day_dir)
        if qqq_syms:
            days_with_qqq_data += 1
        for fname in ("signal_journal.csv", "trade_context.csv"):
            for r in load_csv(os.path.join(day_dir, fname)):
                sym = (r.get("symbol") or "").strip()
                if not sym:
                    continue
                source = "qqq_list" if sym in qqq_syms else "general_screener"
                buckets[(fname, source)].append(r)

    print(f"Days scanned: {len(days)}  "
          f"({days_with_qqq_data} had a captured QQQ-list augmentation line)\n")

    for fname in ("signal_journal.csv", "trade_context.csv"):
        print(f"=== {fname} ===")
        any_rows = False
        for source in ("general_screener", "qqq_list"):
            rows = buckets.get((fname, source), [])
            if not rows:
                print(f"  {source:18s} 0 rows")
                continue
            any_rows = True
            n = len(rows)
            if fname == "signal_journal.csv":
                taken = [r for r in rows if r.get("taken") in ("True", "1", "true")]
                rel = mean(num(r.get("cf_rel_strength")) for r in rows)
                p15 = mean(num(r.get("pct_15min")) for r in rows)
                p30 = mean(num(r.get("pct_30min")) for r in rows)
                print(f"  {source:18s} n={n:4d}  taken={len(taken):4d}  "
                      f"mean cf_rel_strength={rel:.2f}" if rel is not None else
                      f"  {source:18s} n={n:4d}  taken={len(taken):4d}")
                if p15 is not None:
                    print(f"  {'':18s} mean pct_15min={p15:+.3f}%"
                          + (f"  mean pct_30min={p30:+.3f}%" if p30 is not None else ""))
            else:
                pnl = [num(r.get("realized_pnl")) for r in rows]
                pnl = [x for x in pnl if x is not None]
                wins = [x for x in pnl if x > 0]
                total = sum(pnl) if pnl else None
                wr = (len(wins) / len(pnl) * 100) if pnl else None
                if total is not None:
                    print(f"  {source:18s} n={n:4d} tranches  total_pnl={total:8.2f}  "
                          f"win_rate={wr:.0f}%")
                else:
                    print(f"  {source:18s} n={n:4d} tranches  (no realized_pnl data)")
        if not any_rows:
            print("  (no rows for either source)")
        print()


if __name__ == "__main__":
    sys.exit(main())
