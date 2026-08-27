#!/usr/bin/env python3
"""
Per-session metrics for every trading day on file, plus a pooled multi-day read
of which signal features actually predict anything.

Why this exists
---------------
Findings kept being drawn from whichever session had just finished. One day is
13 symbols and a handful of positions; a rank correlation computed on it will
happily show a factor as strong that reverses the next morning. Anything acted
on needs to hold across days, and that means the numbers have to be recomputed
over the whole history every time rather than remembered from a single write-up.

So this reads the two running CSVs - which already contain every session - and
regenerates the ledger from scratch on each run. Nothing is appended by hand and
nothing goes stale: re-running after a new session updates every table.

    python3 ops/session-metrics.py                  # print to stdout
    python3 ops/session-metrics.py --write          # also update ANALYSIS_LOG.md
    python3 ops/session-metrics.py --since 2026-08-24

Stdlib only, so it runs in the VPS venv with nothing installed.
"""

import argparse
import collections
import csv
import math
import os
import sys

TRADE_FIELDS = [
    "date", "symbol", "entry_time", "entry_price", "entry_method", "burst_logic",
    "price_source", "signal_pct", "post_exit_pct", "post_exit_note", "entry_rsi",
    "mfe_pct", "mae_pct", "exit_time", "exit_price", "exit_reason",
    "stop_loss_used", "exit_rsi", "qty", "pl", "pl_pct",
]

JOURNAL_FIELDS = [
    "date", "signal_time", "symbol", "entry_method", "price",
    "signal_pct", "excess_vs_spy_pct", "spy_pct", "rvol", "spread_pct",
    "burst_width",
    "opening_hit_rate", "opening_avg_gain", "opening_sessions",
    "cf_efficiency", "cf_rel_strength", "cf_vol_accel", "cf_vwap_pos",
    "cf_exhaustion", "cf_breakout", "cf_rvol", "cf_spread", "cf_vwap", "cf_score",
    "taken", "skip_reason", "qty", "size_multiplier",
    "price_15min", "pct_15min", "price_30min", "pct_30min",
]

MARKERS = ("<!-- BEGIN GENERATED -->", "<!-- END GENERATED -->")


def read_rows(path, fields):
    """
    Rows keyed by name, tolerating a stale header.

    Both CSVs write their header once at file creation, so a file created before
    a column was added advertises the old schema while newer rows carry the new
    one (see src/analytics/csv_schema.py). Rows are keyed by WIDTH: as wide as
    the on-disk header means it was written under that header, as wide as the
    current schema means this one. Mapping by NAME rather than position matters
    because both schemas grew by INSERTION, not appending - treating an old row
    as a prefix of the new one shifts every value after the insertion point.
    """
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

    rows, widths = [], collections.Counter()
    for row in body:
        if not row:
            continue
        widths[len(row)] += 1
        if len(row) == len(fields):
            rows.append(dict(zip(fields, row)))
        elif old and len(row) == len(old):
            rows.append(dict(zip(old, row)))
        # any other width is unidentifiable: counted, never guessed at
    return rows, widths


def num(row, key):
    try:
        v = float(row.get(key) or "")
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def spearman(pairs):
    """Rank correlation, ties averaged. Rank, not Pearson - forward returns
    have fat tails and one outlier would otherwise decide the coefficient."""
    n = len(pairs)
    if n < 12:
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

    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    numer = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    return numer / (dx * dy) if dx and dy else None


def positions(trades):
    """
    Exit rows collapsed into positions.

    A tiered exit emits one row per tranche, so counting rows counts a scaled-out
    winner three times and overstates both the trade count and the win rate.
    (symbol, entry_time) is the position key.
    """
    by_key = collections.defaultdict(list)
    for t in trades:
        by_key[(t.get("symbol"), t.get("entry_time"))].append(t)

    out = []
    for (symbol, entry_time), rows in by_key.items():
        mfes = [num(r, "mfe_pct") for r in rows if num(r, "mfe_pct") is not None]
        maes = [num(r, "mae_pct") for r in rows if num(r, "mae_pct") is not None]
        out.append({
            "date": rows[0].get("date"),
            "symbol": symbol,
            "entry_time": entry_time,
            "pl": sum(num(r, "pl") or 0.0 for r in rows),
            "mfe": max(mfes) if mfes else None,
            "mae": min(maes) if maes else None,
            "reasons": [r.get("exit_reason") for r in rows],
            "tiered": any("TAKE_PROFIT" in (r.get("exit_reason") or "") for r in rows),
        })
    return out


def ceiling_table(journal, ceiling):
    """
    The highest signal each session reached, against the ceiling meant to cut it.

    Tracked daily because a ceiling that never binds produces no evidence and
    cannot be judged. On 2026-08-26 rapid_increase_max_pct was 2.0 and the
    largest signal all day was 1.452% - the threshold had refused nothing since
    it shipped, which is indistinguishable in the logs from it working.
    """
    dates = sorted({r["date"] for r in journal if r.get("date")})
    lines = [f"Ceiling in force: **{ceiling}%** (`rapid_increase_max_pct`)", "",
             "| date | signals | peak signal | over ceiling | p90 | median |",
             "|---|---|---|---|---|---|"]
    for d in dates:
        vals = [num(r, "signal_pct") for r in journal if r["date"] == d]
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            continue
        over = sum(1 for v in vals if v > ceiling)
        p90 = vals[min(len(vals) - 1, int(len(vals) * 0.9))]
        med = vals[len(vals) // 2]
        flag = "" if over else "  <- never bound"
        lines.append(f"| {d} | {len(vals)} | **{vals[-1]:.3f}%** | {over}{flag} "
                     f"| {p90:.2f}% | {med:.2f}% |")
    lines.append("")
    lines.append("`over ceiling` is how many signals the ceiling actually refused. "
                 "A run of zeros means the threshold is inert and the number cannot "
                 "be evaluated from this data at all.")
    return lines


def session_table(pos_by_date):
    lines = [
        "| date | P&L | pos | win rate | avg win | avg loss | payoff | breakeven WR |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for date in sorted(pos_by_date):
        ps = pos_by_date[date]
        wins = [p["pl"] for p in ps if p["pl"] > 0]
        losses = [p["pl"] for p in ps if p["pl"] < 0]
        pl = sum(p["pl"] for p in ps)
        wr = 100 * len(wins) / len(ps) if ps else 0
        aw = sum(wins) / len(wins) if wins else 0
        al = sum(losses) / len(losses) if losses else 0
        payoff = (aw / abs(al)) if al else float("inf")
        be = 100 / (1 + payoff) if payoff not in (0, float("inf")) else 0
        lines.append(
            f"| {date} | {pl:+.2f} | {len(ps)} | {wr:.0f}% | {aw:+.2f} | {al:+.2f} | "
            f"{payoff:.2f}x | {be:.0f}% |"
        )
    return lines


def mfe_table(pos_by_date):
    """The bimodality check: does the outcome separate by how far a position
    ever went green? This is the finding that has held up, so it gets tracked
    per session rather than asserted once."""
    buckets = [
        (-9e9, 0.001, "never above entry"),
        (0.001, 0.5, "green, under +0.5%"),
        (0.5, 1.0, "+0.5% to +1.0%"),
        (1.0, 9e9, "+1.0% or better"),
    ]
    lines = ["| date | " + " | ".join(b[2] for b in buckets) + " |",
             "|---|" + "---|" * len(buckets)]
    for date in sorted(pos_by_date):
        ps = [p for p in pos_by_date[date] if p["mfe"] is not None]
        cells = []
        for lo, hi, _ in buckets:
            g = [p for p in ps if lo <= p["mfe"] < hi]
            if not g:
                cells.append("-")
                continue
            w = sum(1 for p in g if p["pl"] > 0)
            cells.append(f"{sum(p['pl'] for p in g):+.0f} ({w}/{len(g)})")
        lines.append(f"| {date} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("Each cell is P&L (winners/positions) for positions whose PEAK "
                 "landed in that band.")
    return lines


def reason_table(pos_by_date):
    per = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0]))
    reasons = set()
    for date, ps in pos_by_date.items():
        for p in ps:
            for r in p["reasons"]:
                r = r or "?"
                reasons.add(r)
                per[date][r][0] += 1
    lines = ["| date | " + " | ".join(sorted(reasons)) + " |",
             "|---|" + "---|" * len(reasons)]
    for date in sorted(per):
        lines.append(f"| {date} | " +
                     " | ".join(str(per[date][r][0]) if per[date][r][0] else "-"
                                for r in sorted(reasons)) + " |")
    lines.append("")
    lines.append("Exit-reason counts are per TRANCHE, not per position - a tiered "
                 "winner contributes to several.")
    return lines


FACTORS = [
    ("cf_score", "cf_score (composite)"),
    ("opening_hit_rate", "opening_hit_rate"),
    ("cf_exhaustion", "cf_exhaustion (neg=good)"),
    ("cf_rel_strength", "cf_rel_strength"),
    ("cf_vwap_pos", "cf_vwap_pos"),
    ("cf_efficiency", "cf_efficiency"),
    ("cf_vol_accel", "cf_vol_accel"),
    ("cf_breakout", "cf_breakout"),
    ("cf_rvol", "cf_rvol"),
    ("cf_spread", "cf_spread"),
    ("signal_pct", "signal_pct"),
    ("excess_vs_spy_pct", "excess_vs_spy_pct"),
    ("rvol", "rvol (raw)"),
    ("spread_pct", "spread_pct (raw)"),
    ("burst_width", "burst_width"),
]


def factor_table(journal, horizon):
    """
    Per-day rho AND a pooled rho, side by side.

    The point of the per-day columns is not the individual numbers - it is
    whether a factor keeps its SIGN. A factor that is +0.4 one day and -0.3 the
    next has told you nothing, however good the pooled figure looks.
    """
    col = f"pct_{horizon}min"
    dates = sorted({r["date"] for r in journal if num(r, col) is not None})
    lines = ["| factor | pooled rho | n | " + " | ".join(dates) + " |",
             "|---|---|---|" + "---|" * len(dates)]
    rows = []
    for key, label in FACTORS:
        pooled = [(num(r, key), num(r, col)) for r in journal
                  if num(r, key) is not None and num(r, col) is not None]
        rho = spearman(pooled)
        if rho is None:
            continue
        cells = []
        for d in dates:
            day = [(num(r, key), num(r, col)) for r in journal
                   if r["date"] == d and num(r, key) is not None and num(r, col) is not None]
            dr = spearman(day)
            cells.append(f"{dr:+.2f}" if dr is not None else "·")
        rows.append((abs(rho), f"| {label} | **{rho:+.3f}** | {len(pooled)} | "
                               + " | ".join(cells) + " |"))
    lines += [r[1] for r in sorted(rows, reverse=True)]
    return lines


def independence_note(journal):
    lines = []
    for date in sorted({r["date"] for r in journal}):
        day = [r for r in journal if r["date"] == date]
        syms = collections.Counter(r["symbol"] for r in day)
        lines.append(f"- **{date}**: {len(day)} signals across {len(syms)} symbols "
                     f"(most repeated: " +
                     ", ".join(f"{s}x{n}" for s, n in syms.most_common(3)) + ")")
    return lines


def symbols_table(pos_by_date):
    lines = ["| date | symbols traded | best | worst |", "|---|---|---|---|"]
    for date in sorted(pos_by_date):
        per = collections.defaultdict(float)
        for p in pos_by_date[date]:
            per[p["symbol"]] += p["pl"]
        if not per:
            continue
        best = max(per.items(), key=lambda kv: kv[1])
        worst = min(per.items(), key=lambda kv: kv[1])
        lines.append(f"| {date} | {' '.join(sorted(per))} | {best[0]} {best[1]:+.0f} "
                     f"| {worst[0]} {worst[1]:+.0f} |")
    return lines


def recurring_symbols(pos_by_date):
    per = collections.defaultdict(lambda: {"days": set(), "pl": 0.0, "n": 0})
    for date, ps in pos_by_date.items():
        for p in ps:
            e = per[p["symbol"]]
            e["days"].add(date)
            e["pl"] += p["pl"]
            e["n"] += 1
    lines = ["| symbol | days traded | positions | cumulative P&L |", "|---|---|---|---|"]
    for s, e in sorted(per.items(), key=lambda kv: -kv[1]["pl"]):
        lines.append(f"| {s} | {len(e['days'])} | {e['n']} | {e['pl']:+.2f} |")
    lines.append("")
    lines.append("A symbol's cumulative P&L over a handful of positions is NOT "
                 "evidence it is good or bad - at 1-2 trades per symbol per week "
                 "this is mostly noise. It is here to show CONCENTRATION: which "
                 "names the screener keeps returning to.")
    return lines


def build(trades, journal, tw, jw, ceiling=1.25):
    pos = positions(trades)
    by_date = collections.defaultdict(list)
    for p in pos:
        by_date[p["date"]].append(p)

    out = []
    out.append("## Sessions")
    out.append("")
    out += session_table(by_date)
    out.append("")
    out.append("Positions, not exit rows: a tiered winner scaling out in three "
               "tranches is ONE position. Counting rows would inflate both the "
               "trade count and the win rate.")
    out.append("")
    out.append("## Signal ceiling")
    out.append("")
    out += ceiling_table(journal, ceiling)
    out.append("")
    out.append("## Outcome by peak (MFE)")
    out.append("")
    out += mfe_table(by_date)
    out.append("")
    out.append("## Exit reasons")
    out.append("")
    out += reason_table(by_date)
    out.append("")
    out.append("## Symbols")
    out.append("")
    out += symbols_table(by_date)
    out.append("")
    out.append("### Across all sessions")
    out.append("")
    out += recurring_symbols(by_date)
    out.append("")
    out.append("## Do the signal features predict anything?")
    out.append("")
    out.append("Per-day columns exist to check whether a factor KEEPS ITS SIGN. "
               "A factor that flips between sessions has told you nothing, however "
               "strong the pooled number looks. `·` means too few rows that day.")
    for h in (15, 30):
        out.append("")
        out.append(f"### {h}-minute forward return")
        out.append("")
        out += factor_table(journal, h)
    out.append("")
    out.append("## Sample independence")
    out.append("")
    out.append("Signals re-fire on the same symbol every poll, so the row count "
               "badly overstates how much independent evidence there is. Read "
               "every rho above against the symbol count, not the signal count.")
    out.append("")
    out += independence_note(journal)
    if tw and len(tw) > 1:
        out.append("")
        out.append(f"> `trade_history.csv` row widths: {dict(tw)} - the file spans "
                   f"a schema change.")
    if jw and len(jw) > 1:
        out.append(f"> `signal_journal.csv` row widths: {dict(jw)} - the file spans "
                   f"a schema change.")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="logs/trade_history.csv")
    ap.add_argument("--journal", default="logs/signal_journal.csv")
    ap.add_argument("--since", help="only sessions on/after this date (YYYY-MM-DD)")
    ap.add_argument("--out", default="ANALYSIS_LOG.md")
    ap.add_argument("--write", action="store_true",
                    help="rewrite the generated block of --out in place")
    args = ap.parse_args()

    trades, tw = read_rows(args.trades, TRADE_FIELDS)
    journal, jw = read_rows(args.journal, JOURNAL_FIELDS)
    if not trades and not journal:
        sys.exit(f"no data in {args.trades} or {args.journal}")
    if args.since:
        trades = [t for t in trades if (t.get("date") or "") >= args.since]
        journal = [j for j in journal if (j.get("date") or "") >= args.since]

    # Read the live ceiling so the table is measured against what is actually
    # in force, not a number frozen into this script.
    ceiling = 1.25
    try:
        import yaml
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "config.yaml")
        ceiling = yaml.safe_load(open(cfg_path))["trading"]["rapid_increase_max_pct"]
    except Exception:
        pass

    body = "\n".join(build(trades, journal, tw, jw, ceiling=ceiling))

    if not args.write:
        print(body)
        return

    head = (f"# Analysis log\n\n"
            f"Everything below is REGENERATED by `ops/session-metrics.py` from\n"
            f"`{args.trades}` and `{args.journal}`. Do not edit it by hand - re-run\n"
            f"the script after each session instead. Hand-written notes go BELOW\n"
            f"the end marker, where they survive regeneration.\n\n")
    block = f"{MARKERS[0]}\n\n{head}{body}\n\n{MARKERS[1]}\n"

    existing = ""
    if os.path.exists(args.out):
        existing = open(args.out).read()

    if MARKERS[0] in existing and MARKERS[1] in existing:
        pre = existing[:existing.index(MARKERS[0])]
        post = existing[existing.index(MARKERS[1]) + len(MARKERS[1]):]
        new = pre + block + post
    else:
        new = block + ("\n" + existing if existing else "")

    with open(args.out, "w") as f:
        f.write(new)
    print(f"wrote {args.out} ({len(body.splitlines())} generated lines)")


if __name__ == "__main__":
    main()
