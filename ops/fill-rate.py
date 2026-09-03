#!/usr/bin/env python3
"""
Execution-cost report: what slippage actually costs, and whether a PASSIVE
limit entry would be worth the fills it would miss.

THE QUESTION THIS ANSWERS. Entries are currently routed as MARKETABLE limits -
placed above the reference price, so they cross the spread and fill like a
market order while refusing a fill arbitrarily far away. The obvious next step
is a PASSIVE limit, at or below the reference: strictly better fills when it
works, and no fill at all when it does not.

That trade cannot be reasoned about, only measured, because this strategy buys
into a RISING price ("up 0.3% in 3 minutes"). The orders a passive limit would
fail to fill are exactly the fastest movers - plausibly the best trades. So it
could improve average fill price and LOWER total P&L at the same time.

The measurement was impossible before 2026-09-02 because entry slippage was
computed, logged and thrown away. It is now persisted per trade, so:

  - how much slippage costs today, in dollars and as a share of gross P&L
  - what the trades with the WORST entry slippage went on to do. If they are
    the winners, a passive limit is buying a better price on the trades you
    most want and missing them; if they are the losers, it is free money.

Reads logs/trade_context.csv. Read-only - nothing here touches the live path.
"""
import argparse
import csv
import os
import sys


def num(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "n/a"


def median(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[len(s) // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", default="logs/trade_context.csv")
    ap.add_argument("--since", help="YYYY-MM-DD")
    ap.add_argument("--buckets", type=int, default=4,
                    help="how many slippage buckets to split outcomes across")
    args = ap.parse_args()

    if not os.path.exists(args.context):
        print(f"no such file: {args.context}", file=sys.stderr)
        return 1

    rows = []
    with open(args.context, newline="") as fh:
        for r in csv.DictReader(fh):
            if args.since and (r.get("date") or "") < args.since:
                continue
            rows.append(r)

    if not rows:
        print("Nothing to report.")
        return 0

    ent = [(num(r.get("entry_slippage_pct")), r) for r in rows]
    ent = [(v, r) for v, r in ent if v is not None]
    ext = [num(r.get("exit_slippage_pct")) for r in rows]
    ext = [v for v in ext if v is not None]

    print(f"=== EXECUTION COST: {len(rows)} trades"
          + (f" since {args.since}" if args.since else "") + " ===")
    print(f"{len(ent)} carry entry slippage, {len(ext)} carry exit slippage.")
    if not ent and not ext:
        print("\nNo slippage recorded yet. Both columns were added 2026-09-02;")
        print("trades logged before that have nothing to report, which is not")
        print("the same as having had no slippage.")
        return 0

    if ent:
        vals = [v for v, _ in ent]
        adverse = [v for v in vals if v > 0]        # paid MORE than the signal
        print(f"\n--- ENTRY (positive = paid above the signal price) ---")
        print(f"mean {sum(vals) / len(vals):+.3f}%   median {median(vals):+.3f}%   "
              f"worst {max(vals):+.3f}%")
        print(f"{len(adverse)} of {len(vals)} ({pct(len(adverse), len(vals))}) filled worse "
              f"than the price the signal fired at")

    if ext:
        print(f"\n--- EXIT (negative = filled worse than the exit rule asked for) ---")
        print(f"mean {sum(ext) / len(ext):+.3f}%   median {median(ext):+.3f}%   "
              f"worst {min(ext):+.3f}%")
        bad = [v for v in ext if v < 0]
        print(f"{len(bad)} of {len(ext)} ({pct(len(bad), len(ext))}) filled worse than "
              f"the decision price")

    # Round-trip cost against the edge it is eating.
    pnls = [num(r.get("realized_pnl")) for r in rows]
    pnls = [p for p in pnls if p is not None]
    if ent and pnls:
        rt = (sum(v for v, _ in ent) / len(ent)) + abs(sum(ext) / len(ext) if ext else 0)
        gross = sum(pnls) / len(pnls)
        print(f"\nRound-trip slippage is about {rt:.3f}% per trade against a mean "
              f"realized P&L of ${gross:+,.2f}.")
        print("A cost of this size matters in proportion to the edge, not in "
              "absolute terms - the same 0.3% is noise against a 2% winner and "
              "most of the trade against a 0.4% one.")

    # THE ACTUAL QUESTION: do the worst-filled entries go on to win?
    if len(ent) >= args.buckets * 2:
        print(f"\n--- WOULD A PASSIVE LIMIT HELP? outcomes by entry slippage ---")
        ordered = sorted(ent, key=lambda x: x[0])
        size = len(ordered) // args.buckets
        print(f"{'slippage band':>22}  {'n':>4}  {'mean P&L':>10}  {'win rate':>8}")
        band_means = []
        for i in range(args.buckets):
            chunk = ordered[i * size:(i + 1) * size] if i < args.buckets - 1 else ordered[i * size:]
            if not chunk:
                continue
            ps = [num(r.get("realized_pnl")) for _, r in chunk]
            ps = [p for p in ps if p is not None]
            if not ps:
                continue
            wins = sum(1 for p in ps if p > 0)
            lo, hi = chunk[0][0], chunk[-1][0]
            band_means.append((lo, hi, sum(ps) / len(ps)))
            print(f"{lo:+8.3f}% .. {hi:+7.3f}%  {len(ps):>4}  "
                  f"${sum(ps) / len(ps):>+9,.2f}  {pct(wins, len(ps)):>8}")

        if len(band_means) >= 2:
            worst_band = band_means[-1]
            best_band = band_means[0]
            print()
            if worst_band[2] > best_band[2]:
                print("READ: the WORST-filled entries are the BETTER trades. A passive")
                print("limit would buy a better price on exactly the ones you want and")
                print("miss them entirely. Do NOT switch to passive limits on this "
                      "evidence; the marketable limit already caps the tail.")
            else:
                print("READ: the worst-filled entries are NOT the better trades, so the")
                print("fills being chased are not worth chasing. A passive limit is worth")
                print("TESTING - but only for one config, one week, with fill rate")
                print("recorded, because this measures fills you got, not fills you would")
                print("have missed.")
        print("\nCAVEAT, and it is not a small one: every row here is a trade that")
        print("FILLED. It cannot tell you what a passive limit would have missed.")
        print("Only running one and recording the misses answers that.")
    else:
        print(f"\nNot enough trades with entry slippage yet ({len(ent)}) to bucket "
              f"outcomes. Needs about {args.buckets * 2}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
