#!/usr/bin/env python3
"""
Grid search over exit configurations, with error bars and a plateau reading.

This tool is deliberately BAD at naming a winner and good at showing you
where the good region is. That is not a limitation, it is the point.

WHY. Testing 64 combinations against 30 trades and taking the best cell does
not find the best config - it finds the luckiest one. With a per-trade spread
of ~$70 the standard error on a 30-trade mean is ~$13, and the maximum of 64
noisy draws sits roughly 2.5 standard errors high by chance alone: about
$32/trade of pure illusion, against real effects of maybe $5-15. So the top
cell at low n is noise wearing a number.

Two defences, both built in:

  1. EVERY CELL CARRIES ITS INTERVAL, and the tool says plainly when no cell
     is distinguishable from the best rather than sorting by mean and
     printing a champion.

  2. THE PLATEAU READING is the headline. A smooth region of good values
     (stops from -0.4 to -0.6 all decent, -0.9 clearly worse) is real signal
     at far lower n than any single cell reaching significance. An isolated
     spike surrounded by bad cells is noise however good its number looks.
     Read the marginal tables first; read the leaderboard second, if at all.

    python3 ops/grid.py
    python3 ops/grid.py --stops=-0.4,-0.5,-0.6 --bes=0.5/0.05,0.5/0.15,0.5/0.30,none
    python3 ops/grid.py --by-regime

NOTE THE `=`. A value starting with a minus sign has to be attached with
`--stops=-0.4,...` - written apart, argparse reads the leading `-` as the
start of another flag and refuses.

Stdlib only.
"""

import argparse
import collections
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_replay = None


def _load_replay():
    """ops/*.py are hyphen-free here, but importing a sibling script still
    needs an explicit path load to stay robust to how this is invoked."""
    global _replay
    if _replay is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "replay_mod", os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay.py"))
        _replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_replay)
    return _replay


# The breakeven variants asked for on 2026-09-02, as the default sweep:
#   A  +0.5% -> +0.05%      C  +0.5% -> +0.30%
#   B  +0.5% -> +0.15%      D  no breakeven until +0.75%
DEFAULT_STOPS = [-0.4, -0.5, -0.75, -1.0]
DEFAULT_BES = ["0.5/0.05", "0.5/0.15", "0.5/0.30", "0.75/0.15", "none"]
DEFAULT_TPS = [
    "0.75:0.4,1.0:0.3,1.25:1.0",
    "1.0:0.4,1.25:0.3,1.5:1.0",
    "1.0:0.5,2.0:1.0",
    "1.5:1.0",
]


def fmt_money(v):
    return f"${v:+,.2f}" if v is not None else "n/a"


def marginal(results, index):
    """
    Mean outcome grouped by ONE parameter, averaged over every setting of the
    others. This is the plateau view: it asks "is this stop good across the
    board", which is a far more robust question than "is this exact triple
    the best", and it needs much less data to answer.
    """
    buckets = collections.defaultdict(list)
    for combo, summ in results:
        if summ["mean"] is None:
            continue
        buckets[combo[index]].append(summ["mean"])
    out = []
    for key, means in buckets.items():
        out.append((key, sum(means) / len(means), len(means)))
    return out


def print_marginal(title, rows, order=None):
    print(f"\n--- {title} (mean $/trade, averaged over all other settings) ---")
    if order:
        rows = sorted(rows, key=lambda r: order.index(r[0]) if r[0] in order else 99)
    else:
        rows = sorted(rows, key=lambda r: -r[1])
    best = max(r[1] for r in rows) if rows else 0
    for key, mean, n in rows:
        bar = "#" * max(0, int(round((mean - min(r[1] for r in rows)) / 2))) if len(rows) > 1 else ""
        flag = "  <- best" if mean == best else ""
        print(f"  {str(key):<26} {fmt_money(mean):>12}  ({n} combos) {bar}{flag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default="logs/trade_context.csv")
    ap.add_argument("--paths", default="logs/trade_paths.csv")
    ap.add_argument("--since")
    ap.add_argument("--stops", help="comma-separated, e.g. -0.4,-0.5,-0.75")
    ap.add_argument("--bes", help="comma-separated trigger/floor, or 'none'")
    ap.add_argument("--tps", help="semicolon-separated tier sets")
    ap.add_argument("--trail", type=float, default=None)
    ap.add_argument("--by-regime", action="store_true",
                    help="split the whole grid by the regime recorded at entry")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    R = _load_replay()
    trades, skipped = R.load_trades(args.context, args.paths, args.since, None)
    if not trades:
        sys.exit(
            f"no replayable trades yet in {args.context} + {args.paths}.\n"
            f"They fill once the recorder is deployed - see docs/REPLAY.md."
        )

    stops = [float(s) for s in args.stops.split(",")] if args.stops else DEFAULT_STOPS
    bes = args.bes.split(",") if args.bes else DEFAULT_BES
    tps = args.tps.split(";") if args.tps else DEFAULT_TPS

    groups = {"ALL": trades}
    if args.by_regime:
        groups = collections.defaultdict(list)
        for t in trades:
            groups[(t["ctx"].get("regime") or "unknown")].append(t)

    for group_name, group_trades in groups.items():
        header = f"=== GRID: {len(stops)}x{len(bes)}x{len(tps)} = {len(stops)*len(bes)*len(tps)} configs"
        header += f" over {len(group_trades)} trades"
        if group_name != "ALL":
            header += f"  [regime: {group_name}]"
        print(header + " ===")
        if skipped and group_name == "ALL":
            print(f"({skipped} trades skipped - too few path samples to replay)")

        results = []
        for stop, be, tp in itertools.product(stops, bes, tps):
            trig, floor = R.parse_be(be)
            cfg = R.ExitConfig(
                stop_pct=stop, trail_pct=args.trail,
                be_trigger=trig, be_floor=floor, tiers=R.parse_tiers(tp),
            )
            rows = R.replay_all(group_trades, cfg)
            results.append(((stop, be, tp), R.summarize(rows)))

        n = max((s["n"] for _, s in results), default=0)

        # ---- the headline: plateau tables, not a champion ----
        print_marginal("STOP", marginal(results, 0))
        print_marginal("BREAKEVEN", marginal(results, 1))
        print_marginal("TAKE-PROFIT", marginal(results, 2))

        # ---- the leaderboard, with the honesty attached ----
        ranked = sorted((r for r in results if r[1]["mean"] is not None),
                        key=lambda r: -r[1]["mean"])
        if not ranked:
            print("\nno cell produced a result - nothing to rank")
            continue

        best = ranked[0][1]
        print(f"\n--- top {min(args.top, len(ranked))} cells (READ THIS SECOND) ---")
        for combo, s in ranked[:args.top]:
            ci = (f"[{fmt_money(s['ci_low'])}, {fmt_money(s['ci_high'])}]"
                  if s["ci_low"] is not None else "[n too small]")
            print(f"  stop={combo[0]:<6} be={combo[1]:<10} tp={combo[2]:<26} "
                  f"mean {fmt_money(s['mean']):>10}  n={s['n']:<4} 95% {ci}")

        # How many cells are statistically indistinguishable from the best?
        if best["ci_low"] is not None:
            overlapping = sum(
                1 for _, s in ranked
                if s["ci_high"] is not None and s["ci_high"] >= best["ci_low"]
            )
            print()
            if overlapping > 1:
                print(
                    f"VERDICT: {overlapping} of {len(ranked)} configs are NOT "
                    f"distinguishable from the top one at n={best['n']} - their "
                    f"intervals all overlap it. Do NOT pick the top row. Read the "
                    f"marginal tables above for a region that is good across the "
                    f"board, and re-run as n grows."
                )
            else:
                print(
                    f"VERDICT: the top config's interval clears every other cell "
                    f"at n={best['n']}. That is a real separation - still confirm "
                    f"it holds as n grows before deploying it."
                )
        if n < 200:
            print(f"\nSAMPLE SIZE: n={n}. Below ~200 trades a grid maximum is "
                  f"mostly noise (see the header of this file). The marginal "
                  f"tables are usable earlier than the leaderboard.")
        print()


if __name__ == "__main__":
    main()
