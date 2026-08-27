#!/usr/bin/env python3
"""
Ask the signal journal one question: do the continuation factors actually
separate the signals that worked from the ones that didn't?

Reads logs/signal_journal.csv, which records EVERY signal that fired - taken or
skipped - with the features available at that moment and the forward return
15/30 minutes later. The skipped rows are the control group: without them any
ranking scheme is an untestable claim that the ones you passed on would have
done worse.

Stdlib only, on purpose - it has to run inside the VPS venv with no installs.

    python3 ops/analyze-journal.py                       # every date on file
    python3 ops/analyze-journal.py --date 2026-08-26     # one session
    python3 ops/analyze-journal.py --horizon 30          # score against 30-min
"""

import argparse
import csv
import math
import os
import sys
from collections import defaultdict

JOURNAL_FIELDS = [
    "date", "signal_time", "symbol", "entry_method", "price",
    "signal_pct", "excess_vs_spy_pct", "spy_pct", "rvol", "spread_pct",
    "burst_width",
    "opening_hit_rate", "opening_avg_gain", "opening_sessions",
    "cf_efficiency", "cf_rel_strength", "cf_vol_accel", "cf_vwap_pos",
    "cf_exhaustion", "cf_breakout", "cf_rvol", "cf_spread", "cf_vwap",
    "cf_sector_strength", "cf_sector_etf",
    "cf_score",
    "taken", "skip_reason", "qty", "size_multiplier",
    "price_15min", "pct_15min", "price_30min", "pct_30min",
]

# The continuation factors, in the order they appear in the journal. Each is
# 0-100 and HIGHER IS BETTER, except exhaustion, which is subtracted by
# continuation_score and so should correlate NEGATIVELY with forward return.
FACTORS = [
    ("cf_efficiency", "efficiency (momentum persistence)"),
    ("cf_rel_strength", "rel strength vs SPY"),
    ("cf_vol_accel", "volume acceleration"),
    ("cf_vwap_pos", "VWAP position"),
    ("cf_rvol", "RVOL"),
    ("cf_breakout", "breakout quality"),
    ("cf_spread", "spread quality"),
    ("cf_exhaustion", "exhaustion (NEGATIVE is good)"),
    ("cf_sector_strength", "sector strength"),
    ("cf_score", "*** COMPOSITE cf_score ***"),
]

# Raw features already logged before the continuation work, kept in the report
# as a baseline: any new factor has to beat what was already free.
BASELINE = [
    ("signal_pct", "signal size at fire"),
    ("excess_vs_spy_pct", "excess vs SPY"),
    ("rvol", "RVOL (raw)"),
    ("spread_pct", "spread % (raw, lower better)"),
    ("burst_width", "burst width"),
]



# The journal's header is written once, at file creation, so a file created
# before a column was added still advertises the old schema while its newer
# rows carry the new one - see src/analytics/csv_schema.py. csv.DictReader
# trusts the header, which on 2026-08-26 silently hid every continuation
# factor.
#
# Rows are therefore keyed by WIDTH: a row as wide as the on-disk header was
# written under that header, a row as wide as the current schema under this
# one. Mapping by NAME rather than by position matters - the new columns were
# inserted at index 11, not appended, so an old 19-column row does NOT hold the
# first 19 values of the 32-column schema and treating it that way would put
# `taken` under `opening_hit_rate`.
def read_rows(path):
    """[(dict)], {width: count} - rows keyed by name regardless of the header."""
    rows, widths = [], {}
    with open(path, newline="") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return rows, widths

    header = raw[0]
    is_header = bool(header) and header[0] == "date" and "signal_time" in header
    body = raw[1:] if is_header else raw
    old_fields = header if is_header else None

    for row in body:
        if not row:
            continue
        w = len(row)
        widths[w] = widths.get(w, 0) + 1
        if w == len(JOURNAL_FIELDS):
            rows.append(dict(zip(JOURNAL_FIELDS, row)))
        elif old_fields and w == len(old_fields):
            rows.append(dict(zip(old_fields, row)))
        # Any other width is unidentifiable; counted in `widths` and reported,
        # never guessed at.
    return rows, widths


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


def spearman(pairs):
    """
    Rank correlation, ties averaged. Rank rather than Pearson because these
    factors are bounded 0-100 and forward returns have fat tails - one +9%
    print would otherwise decide the whole coefficient.
    """
    n = len(pairs)
    if n < 4:
        return None

    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx = ranks([p[0] for p in pairs])
    ry = ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def quartiles(pairs):
    """Mean forward return and hit rate by quartile of the factor."""
    if len(pairs) < 8:
        return None
    ordered = sorted(pairs, key=lambda p: p[0])
    n = len(ordered)
    out = []
    for q in range(4):
        lo, hi = n * q // 4, n * (q + 1) // 4
        chunk = ordered[lo:hi]
        if not chunk:
            return None
        rets = [c[1] for c in chunk]
        out.append({
            "n": len(chunk),
            "factor_lo": chunk[0][0],
            "factor_hi": chunk[-1][0],
            "mean_ret": sum(rets) / len(rets),
            "hit": sum(1 for r in rets if r > 0) / len(rets) * 100,
        })
    return out


def report_factor(label, pairs, show_quartiles):
    rho = spearman(pairs)
    rho_s = f"{rho:+.3f}" if rho is not None else "  n/a"
    print(f"  {label:<34} n={len(pairs):<5} rho={rho_s}")
    if not show_quartiles:
        return
    qs = quartiles(pairs)
    if not qs:
        return
    for i, q in enumerate(qs):
        print(f"      Q{i + 1} [{q['factor_lo']:7.2f}..{q['factor_hi']:7.2f}] "
              f"n={q['n']:<4} mean {q['mean_ret']:+6.2f}%   hit {q['hit']:5.1f}%")
    spread = qs[-1]["mean_ret"] - qs[0]["mean_ret"]
    verdict = "SEPARATES" if abs(spread) >= 0.15 else "no separation"
    print(f"      Q4 - Q1 = {spread:+.2f}pp   -> {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/signal_journal.csv")
    ap.add_argument("--date", help="restrict to one session, e.g. 2026-08-26")
    ap.add_argument("--horizon", type=int, default=15, choices=(15, 30))
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"no journal at {args.file}")

    ret_col = f"pct_{args.horizon}min"
    rows, widths = read_rows(args.file)
    if args.date:
        rows = [r for r in rows if r.get("date") == args.date]

    if not rows:
        sys.exit(f"no rows{' for ' + args.date if args.date else ''} in {args.file}")

    by_date = defaultdict(int)
    for r in rows:
        by_date[r.get("date", "?")] += 1

    labelled = [r for r in rows if as_float(r.get(ret_col)) is not None]
    def was_taken(r):
        return str(r.get("taken", "")).strip().lower() in ("true", "1", "yes")

    taken = [r for r in labelled if was_taken(r)]

    print("=" * 72)
    print(f"SIGNAL JOURNAL  {args.file}")
    print("=" * 72)
    print(f"sessions       : {len(by_date)}  ({', '.join(sorted(by_date))})")
    if len(widths) > 1:
        spread = ", ".join(f"{w} cols x{n}" for w, n in sorted(widths.items()))
        print(f"row widths     : {spread}")
        print("                 ^ the file spans a schema change; rows are mapped")
        print("                   by width, not by the (stale) header on disk.")
    print(f"signals        : {len(rows)}")
    print(f"with {ret_col:<10}: {len(labelled)}   "
          f"(the rest are too recent to have a forward return yet)")
    print(f"  taken        : {len(taken)}")
    print(f"  skipped      : {len(labelled) - len(taken)}   <- the control group")

    if labelled:
        rets = [as_float(r[ret_col]) for r in labelled]
        print(f"\nAll signals, {args.horizon}-min forward return:")
        print(f"  mean {sum(rets) / len(rets):+.3f}%   "
              f"hit rate {sum(1 for x in rets if x > 0) / len(rets) * 100:.1f}%")
        if taken and len(taken) < len(labelled):
            tr = [as_float(r[ret_col]) for r in taken]
            sk = [as_float(r[ret_col]) for r in labelled if not was_taken(r)]
            print(f"  taken   mean {sum(tr) / len(tr):+.3f}%  "
                  f"hit {sum(1 for x in tr if x > 0) / len(tr) * 100:.1f}%  (n={len(tr)})")
            if sk:
                print(f"  skipped mean {sum(sk) / len(sk):+.3f}%  "
                      f"hit {sum(1 for x in sk if x > 0) / len(sk) * 100:.1f}%  (n={len(sk)})")
                print("  ^ if skipped beats taken, the selection logic is "
                      "actively picking the worse half.")

    # A factor is only usable if it is actually being written. Report coverage
    # before correlation, because "rho = 0.02 on 3 rows" is not a finding.
    print("\n" + "-" * 72)
    print(f"CONTINUATION FACTORS vs {args.horizon}-min forward return")
    print("  rho = Spearman rank correlation. Positive = higher factor, better")
    print("  outcome. Anything |rho| < 0.10 on a few hundred rows is noise.")
    print("-" * 72)

    any_coverage = False
    for col, label in FACTORS:
        pairs = [(as_float(r.get(col)), as_float(r[ret_col])) for r in labelled]
        pairs = [p for p in pairs if p[0] is not None]
        if not pairs:
            print(f"  {label:<34} NOT POPULATED - no values in the journal")
            continue
        any_coverage = True
        report_factor(label, pairs, show_quartiles=(col == "cf_score"))

    if not any_coverage:
        print("\n  No continuation factors are populated for this range. They")
        print("  started being written on the session the feature deployed -")
        print("  run without --date, or pick a later date.")

    print("\n" + "-" * 72)
    print("BASELINE FEATURES (what was already free before the factors)")
    print("-" * 72)
    for col, label in BASELINE:
        pairs = [(as_float(r.get(col)), as_float(r[ret_col])) for r in labelled]
        pairs = [p for p in pairs if p[0] is not None]
        if not pairs:
            print(f"  {label:<34} not populated")
            continue
        report_factor(label, pairs, show_quartiles=False)

    print("\n" + "=" * 72)
    print("HOW TO READ THIS")
    print("=" * 72)
    print("The composite cf_score is worth turning on only if its Q4-Q1 spread")
    print("is clearly positive AND it beats every baseline feature's rho. If a")
    print("single raw column like excess_vs_spy_pct does just as well, use that")
    print("instead - it needs no weights fitted and cannot overfit.")
    print()
    print("One session is not enough to fit weights to. Two weeks is the gate")
    print("set in PENDING_WORK.md item 4; this is here to watch it approach.")


if __name__ == "__main__":
    main()
