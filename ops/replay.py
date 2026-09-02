#!/usr/bin/env python3
"""
Replay: re-run a stop/breakeven/take-profit config over every recorded trade
path and report what it WOULD have made.

See docs/REPLAY.md for the reasoning, the limits, and how to read the output.

    python3 ops/replay.py                                   # the live config
    python3 ops/replay.py --stop -0.5 --be 0.5/0.15
    python3 ops/replay.py --tp 0.75:0.4,1.0:0.3,1.25:1.0 --trail 0.4
    python3 ops/replay.py --since 2026-09-03 --json

Stdlib only.
"""

import argparse
import collections
import csv
import json
import math
import os
import sys

CONTEXT_FILE = "logs/trade_context.csv"
PATHS_FILE = "logs/trade_paths.csv"


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def load_trades(context_file, paths_file, since=None, symbols=None):
    """
    [{context row, path: [(timestamp, gain_pct), ...]}] for every trade that
    has BOTH a context row and at least two path samples.

    A trade with one sample cannot be replayed - there is no movement to
    apply a rule to - and is reported as skipped rather than silently
    counted as a flat outcome, which would drag every config toward zero.
    """
    ctx = load_csv(context_file)
    paths = load_csv(paths_file)

    by_trade = collections.defaultdict(list)
    for r in paths:
        g = num(r.get("gain_pct"))
        if g is None:
            continue
        by_trade[r.get("trade_id")].append((r.get("timestamp") or "", g))

    out, skipped = [], 0
    for row in ctx:
        if since and (row.get("date") or "") < since:
            continue
        if symbols and (row.get("symbol") or "").upper() not in symbols:
            continue
        samples = sorted(by_trade.get(row.get("trade_id")) or [])
        if len(samples) < 2:
            skipped += 1
            continue
        out.append({"ctx": row, "path": samples})
    return out, skipped


# --------------------------------------------------------------------------
# the simulator
# --------------------------------------------------------------------------

class ExitConfig:
    """
    A stop/breakeven/trailing/take-profit combination, in the same shape the
    live config expresses them.

      stop_pct       hard stop, negative (e.g. -1.0)
      first_exit     partial scale-out level, negative, or None
      first_fraction fraction sold at first_exit
      trail_pct      trailing stop distance from the peak, positive, or None
      be_trigger     peak gain that arms the breakeven floor, or None
      be_floor       where the stop sits once armed (usually a small positive)
      tiers          [(gain_pct, sell_fraction), ...] of the ORIGINAL size;
                     a fraction >= 1.0 closes whatever remains
    """

    def __init__(self, stop_pct=-1.0, first_exit=None, first_fraction=0.0,
                 trail_pct=None, be_trigger=None, be_floor=0.0, tiers=()):
        self.stop_pct = stop_pct
        self.first_exit = first_exit
        self.first_fraction = first_fraction
        self.trail_pct = trail_pct
        self.be_trigger = be_trigger
        self.be_floor = be_floor
        self.tiers = list(tiers)

    def label(self):
        tp = ",".join(f"{g:g}:{f:g}" for g, f in self.tiers) or "none"
        be = f"{self.be_trigger:g}->{self.be_floor:g}" if self.be_trigger is not None else "none"
        return (f"stop={self.stop_pct:g} trail={self.trail_pct if self.trail_pct else 'none'} "
                f"be={be} tp={tp}")


def replay_one(path, cfg):
    """
    Walk one trade's path in time order and return
    (realized_gain_pct, exit_reason, samples_used).

    realized_gain_pct is the size-weighted % return on the ORIGINAL position,
    so partial exits contribute their fraction.

    RULE ORDER AT EACH SAMPLE, mirroring the live strategy: protective exits
    are evaluated before profit-taking, because a bar that trades through
    both a stop and a tier is a bar that went against the position. Choosing
    the other order would let the replay bank a profit the live bot would
    not have taken - the single easiest way to make a backtest lie.
    """
    remaining = 1.0
    realized = 0.0
    peak = 0.0
    tiers_hit = set()
    first_done = False
    reason = "END_OF_PATH"
    used = 0
    prev_gain = None

    for _, gain in path:
        used += 1
        peak = max(peak, gain)

        # --- protective exits, widest-binding first ---
        stop_level = cfg.stop_pct
        if cfg.be_trigger is not None and peak >= cfg.be_trigger:
            stop_level = max(stop_level, cfg.be_floor)
        if cfg.trail_pct:
            stop_level = max(stop_level, peak - cfg.trail_pct)

        if gain <= stop_level:
            # FILL AT THE LEVEL, NOT AT THE NEXT OBSERVED SAMPLE.
            #
            # Paths are sampled every ~10s, so a level is almost always
            # crossed BETWEEN two samples. Realizing at the later sample
            # charges every stop the full distance price happened to travel
            # before the next observation - which makes a -0.5% stop behave
            # like a -0.6% one, and a +0.15% breakeven floor record as -0.6%.
            #
            # That is not a harmless approximation: it penalises TIGHT stops
            # specifically, and the tightness of the stop is the parameter
            # being tuned. Left in, the grid would lean toward wide stops for
            # a reason that is an artifact of the sampling rate.
            #
            # So when the previous sample was above the level, the crossing
            # is genuine and a real stop order would have filled near it.
            # When the position was ALREADY below the level on its first
            # observed sample, there was no crossing to catch - that is a gap,
            # and the observed price is the honest fill.
            crossed = prev_gain is not None and prev_gain > stop_level
            realized += remaining * (stop_level if crossed else gain)
            remaining = 0.0
            if cfg.be_trigger is not None and peak >= cfg.be_trigger and stop_level == cfg.be_floor:
                reason = "BREAKEVEN_STOP"
            elif cfg.trail_pct and stop_level == peak - cfg.trail_pct:
                reason = "TRAILING_STOP"
            else:
                reason = "FINAL_EXIT"
            break

        if (not first_done and cfg.first_exit is not None
                and cfg.first_fraction > 0 and gain <= cfg.first_exit):
            sold = min(remaining, cfg.first_fraction)
            realized += sold * gain
            remaining -= sold
            first_done = True
            if remaining <= 1e-9:
                reason = "FIRST_EXIT"
                break

        # --- profit taking ---
        for idx, (tier_gain, frac) in enumerate(cfg.tiers):
            if idx in tiers_hit or gain < tier_gain:
                continue
            tiers_hit.add(idx)
            sold = remaining if frac >= 1.0 else min(remaining, frac)
            realized += sold * gain
            remaining -= sold
            if remaining <= 1e-9:
                reason = f"TAKE_PROFIT_{tier_gain:g}"
                break
        if remaining <= 1e-9:
            break
        prev_gain = gain

    if remaining > 1e-9:
        realized += remaining * path[-1][1]

    return realized, reason, used


def replay_all(trades, cfg):
    """Per-trade replayed results plus the aggregate."""
    rows = []
    for t in trades:
        gain, reason, used = replay_one(t["path"], cfg)
        entry_px = num(t["ctx"].get("entry_price"))
        qty = num(t["ctx"].get("position_size"))
        pnl = (entry_px * qty * gain / 100.0) if (entry_px and qty) else None
        rows.append({
            "trade_id": t["ctx"].get("trade_id"),
            "symbol": t["ctx"].get("symbol"),
            "date": t["ctx"].get("date"),
            "regime": t["ctx"].get("regime"),
            "gain_pct": gain,
            "pnl": pnl,
            "exit_reason": reason,
            "samples": used,
            "actual_pnl": num(t["ctx"].get("realized_pnl")),
            "actual_reason": t["ctx"].get("exit_reason"),
        })
    return rows


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def summarize(rows, key="pnl"):
    """
    n, mean, standard error and a 95% interval.

    The interval is what stops a grid search from being a noise generator:
    with 30 trades and a ~$70 spread the interval is roughly +/- $25, so any
    ranking inside that band is meaningless however confidently it sorts.
    """
    vals = [r[key] for r in rows if r.get(key) is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "total": None, "se": None,
                "ci_low": None, "ci_high": None, "win_rate": None}
    mean = sum(vals) / n
    total = sum(vals)
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        se = math.sqrt(var / n)
    else:
        se = None
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": n,
        "mean": mean,
        "total": total,
        "se": se,
        "ci_low": (mean - 1.96 * se) if se else None,
        "ci_high": (mean + 1.96 * se) if se else None,
        "win_rate": wins / n,
    }


# --------------------------------------------------------------------------
# config plumbing
# --------------------------------------------------------------------------

def config_from_live(path="config.yaml"):
    """The currently-deployed exit rules, as the replay's baseline. Parsed
    with a minimal reader so this stays stdlib-only on the VPS venv."""
    try:
        import yaml
    except ImportError:
        return None
    try:
        t = yaml.safe_load(open(path))["trading"]
    except Exception:
        return None
    be = (t.get("breakeven_tiers") or [{}])[0]
    return ExitConfig(
        stop_pct=t.get("final_exit_loss_pct", -1.0),
        first_exit=t.get("first_exit_loss_pct"),
        first_fraction=t.get("first_exit_fraction", 0.0) or 0.0,
        trail_pct=t.get("trailing_stop_pct"),
        be_trigger=be.get("trigger_pct"),
        be_floor=be.get("floor_pct", 0.0) or 0.0,
        tiers=[(x["gain_pct"], x["sell_fraction"]) for x in (t.get("take_profit_tiers") or [])],
    )


def parse_tiers(s):
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        gain, frac = part.split(":")
        out.append((float(gain), float(frac)))
    return out


def parse_be(s):
    if not s or s.lower() == "none":
        return None, 0.0
    trigger, floor = s.split("/")
    return float(trigger), float(floor)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default=CONTEXT_FILE)
    ap.add_argument("--paths", default=PATHS_FILE)
    ap.add_argument("--since")
    ap.add_argument("--symbols")
    ap.add_argument("--stop", type=float)
    ap.add_argument("--trail", type=float)
    ap.add_argument("--be", help="trigger/floor, e.g. 0.5/0.15, or 'none'")
    ap.add_argument("--tp", help="gain:fraction pairs, e.g. 0.75:0.4,1.0:0.3,1.25:1.0")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    syms = ({s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None)
    trades, skipped = load_trades(args.context, args.paths, args.since, syms)
    if not trades:
        sys.exit(
            f"no replayable trades in {args.context} + {args.paths}"
            + (f" ({skipped} had too few path samples)" if skipped else "")
            + ".\nThese files start filling once the recorder is deployed - see docs/REPLAY.md."
        )

    cfg = config_from_live() or ExitConfig()
    if args.stop is not None:
        cfg.stop_pct = args.stop
    if args.trail is not None:
        cfg.trail_pct = args.trail
    if args.be is not None:
        cfg.be_trigger, cfg.be_floor = parse_be(args.be)
    if args.tp is not None:
        cfg.tiers = parse_tiers(args.tp)

    rows = replay_all(trades, cfg)
    s = summarize(rows)

    if args.json:
        print(json.dumps({"config": cfg.label(), "summary": s, "trades": rows},
                          indent=2, default=str))
        return

    print(f"=== REPLAY: {cfg.label()} ===")
    print(f"{s['n']} trades replayed" + (f", {skipped} skipped (too few path samples)" if skipped else ""))
    if s["total"] is not None:
        print(f"total ${s['total']:+,.2f}   mean ${s['mean']:+,.2f}/trade   "
              f"win rate {100 * s['win_rate']:.0f}%")
    if s["ci_low"] is not None:
        print(f"95% interval on the mean: ${s['ci_low']:+,.2f} to ${s['ci_high']:+,.2f}")
        if s["n"] < 200:
            print(f"NOTE: n={s['n']}. Rankings between configs are not "
                  f"trustworthy much below ~200 trades - see docs/REPLAY.md.")

    actual = summarize(rows, key="actual_pnl")
    if actual["total"] is not None:
        print(f"\nwhat actually happened: total ${actual['total']:+,.2f}, "
              f"mean ${actual['mean']:+,.2f}/trade")
        if s["total"] is not None:
            print(f"this config vs. actual:  ${s['total'] - actual['total']:+,.2f}")

    by_reason = collections.Counter(r["exit_reason"] for r in rows)
    print("\nexit reasons under this config: "
          + ", ".join(f"{k} {v}" for k, v in by_reason.most_common()))


if __name__ == "__main__":
    main()
