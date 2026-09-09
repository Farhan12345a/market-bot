#!/usr/bin/env python3
"""
One command, one paste: everything usually asked for after a trading day,
combined into a single report instead of seven separate greps and script
runs. Built 2026-09-08 after a diagnostic session for that day's run needed
the EOD email, a trading.log grep, trade_history.csv, daily_summary.csv,
signal_journal.csv, session-metrics.py and analyze-journal.py pasted in
one at a time.

Stdlib only, on purpose - it has to run inside the VPS venv with no installs.
Calls session-metrics.py and analyze-journal.py as subprocesses with the SAME
interpreter this was launched with, so their own (also stdlib-only) output is
folded into one paste rather than needing separate invocations.

    python3 ops/daily-brief.py                       # today (ET)
    python3 ops/daily-brief.py --date 2026-09-08      # a specific session
    python3 ops/daily-brief.py --date 2026-09-08 --no-metrics   # skip the
                                                          # two slower scripts
"""
import argparse
import csv
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta

try:
    import pytz
    ET = pytz.timezone("America/New_York")
except ImportError:
    ET = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO, "logs")


def today_et():
    if ET:
        return datetime.now(ET).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------
# ENTRY-SKIP REASONS, BUCKETED
# ---------------------------------------------------------------------
# Raw grep of "entry skipped" on a real session can run to hundreds of
# lines (2026-09-08 had 1428 signals) - almost all of them the SAME reason
# repeating every poll. A count per reason answers "why did nothing trade"
# in one glance instead of a wall of identical lines.
_SKIP_BUCKETS = [
    (re.compile(r"below min_stock_price"), "price below min_stock_price"),
    (re.compile(r"above max_stock_price"), "price above max_stock_price"),
    (re.compile(r"regime is bearish"), "regime bearish - no new longs"),
    (re.compile(r"at max_positions_per_sector"), "sector concentration cap"),
    (re.compile(r"at max_concurrent_positions"), "concurrent-position cap"),
    (re.compile(r"would exceed max_total_exposure_fraction"), "exposure cap"),
    (re.compile(r"insufficient buying power"), "buying power"),
    (re.compile(r"at max_entry_attempts_per_symbol_per_day"), "per-symbol attempt cap"),
    (re.compile(r"cooldown"), "re-entry cooldown"),
    (re.compile(r"worked out to 0 shares"), "sized to zero"),
    (re.compile(r"excluded"), "excluded symbol (leveraged/basket/explicit)"),
    (re.compile(r"halt"), "halt check"),
    (re.compile(r"stale"), "stale/no fresh data"),
]

_SKIP_LINE_RE = re.compile(
    r"^(?P<ts>\S+ \S+) - \S+ - (?P<level>\w+) - (?P<symbol>\S+): entry skipped - (?P<reason>.+)$"
)


def _bucket_reason(reason):
    for pattern, label in _SKIP_BUCKETS:
        if pattern.search(reason):
            return label
    return reason[:50]


def summarize_entry_skips(date, log_path):
    if not os.path.exists(log_path):
        return None
    counts = Counter()
    examples = {}
    symbols_by_bucket = {}
    with open(log_path, errors="replace") as f:
        for line in f:
            if date not in line or "entry skipped" not in line:
                continue
            m = _SKIP_LINE_RE.match(line.strip())
            if not m:
                continue
            bucket = _bucket_reason(m.group("reason"))
            counts[bucket] += 1
            examples.setdefault(bucket, m.group("reason"))
            symbols_by_bucket.setdefault(bucket, set()).add(m.group("symbol"))
    return counts, examples, symbols_by_bucket


# ---------------------------------------------------------------------
# WARNINGS/ERRORS WORTH SURFACING WITHOUT GREPPING BY HAND
# ---------------------------------------------------------------------
_NOTABLE_RE = re.compile(
    r"(phantom|forced (a )?market|forced entry|forced exit|"
    r"cancelled \d+ (working|unfilled) order|symbol limit exceeded|"
    r"insufficient qty available|RECONCILE:.*mismatch)",
    re.IGNORECASE,
)


def notable_log_lines(date, log_path, limit=40):
    if not os.path.exists(log_path):
        return []
    out = []
    with open(log_path, errors="replace") as f:
        for line in f:
            if date not in line:
                continue
            if _NOTABLE_RE.search(line):
                out.append(line.rstrip("\n"))
    return out[:limit]


# ---------------------------------------------------------------------
# CSV HELPERS
# ---------------------------------------------------------------------
def rows_for_date(csv_path, date, date_col="date", prefix_ok=True):
    if not os.path.exists(csv_path):
        return []
    out = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            v = (row.get(date_col) or "")
            if v == date or (prefix_ok and v.startswith(date)):
                out.append(row)
    return out


def print_header(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (ET)")
    ap.add_argument("--no-metrics", action="store_true",
                     help="skip the session-metrics.py / analyze-journal.py subprocess calls")
    ap.add_argument("--metrics-since-days", type=int, default=14,
                     help="how many days back to pass to session-metrics.py --since (default 14)")
    args = ap.parse_args()
    date = args.date or today_et()

    print(f"DAILY BRIEF for {date}")
    print(f"(repo: {REPO})")

    # ---- 1. Day summary row -------------------------------------------------
    print_header("1. DAILY SUMMARY (logs/daily_summary.csv)")
    summary_rows = rows_for_date(os.path.join(LOG_DIR, "daily_summary.csv"), date)
    if not summary_rows:
        print("No row for this date yet - finish_day has not run, or the date is wrong.")
    else:
        for row in summary_rows:
            for k, v in row.items():
                print(f"  {k}: {v}")
            print()

    # ---- 2. Trades ------------------------------------------------------
    print_header("2. TRADES (logs/trade_history.csv)")
    trades = rows_for_date(os.path.join(LOG_DIR, "trade_history.csv"), date, date_col="date")
    if not trades:
        print("No closed trades recorded for this date.")
    else:
        def _f(v, nd=2):
            try:
                return f"{float(v):.{nd}f}"
            except (TypeError, ValueError):
                return str(v)

        for t in trades:
            print(f"  {t.get('symbol', '?'):6s} {(t.get('entry_method') or ''):20s} "
                  f"entry {_f(t.get('entry_price')):>8s} -> exit {_f(t.get('exit_price')):>8s}  "
                  f"qty {t.get('qty', '?'):>4s}  P&L ${_f(t.get('pl')):>9s} "
                  f"({_f(t.get('pl_pct'))}%)  {t.get('exit_reason', '?')}")
        total_pl = sum(float(t.get("pl") or 0) for t in trades)
        wins = sum(1 for t in trades if float(t.get("pl") or 0) > 0)
        print(f"\n  {len(trades)} trade(s), {wins} win(s), total ${total_pl:+,.2f}")

    # ---- 3. Entry-skip reasons, bucketed ---------------------------------
    print_header("3. ENTRY-SKIP REASONS (logs/trading.log, bucketed)")
    log_path = os.path.join(LOG_DIR, "trading.log")
    skip_summary = summarize_entry_skips(date, log_path)
    if skip_summary is None:
        print(f"No log file at {log_path}")
    else:
        counts, examples, symbols_by_bucket = skip_summary
        if not counts:
            print("No 'entry skipped' lines found for this date.")
        else:
            for bucket, n in counts.most_common():
                syms = sorted(symbols_by_bucket.get(bucket, []))
                sym_note = f" ({', '.join(syms[:6])}{'...' if len(syms) > 6 else ''})" if syms else ""
                print(f"  {n:5d}x  {bucket}{sym_note}")
            print(f"\n  Total entry-skip log lines: {sum(counts.values())}")
            print("  If ONE bucket dominates the whole day, that is usually the actual")
            print("  story - e.g. 'regime bearish' meaning regime_sizing stood the")
            print("  entire session down, not a bug in any per-entry check.")

    # ---- 4. Notable warnings/errors --------------------------------------
    print_header("4. NOTABLE LOG LINES (phantoms, forced retries, reconciliation)")
    notable = notable_log_lines(date, log_path)
    if not notable:
        print("None found - a quiet day on this front.")
    else:
        for line in notable:
            print(f"  {line}")
        if len(notable) == 40:
            print("  ...(truncated at 40; grep the log directly for the full list)")

    # ---- 5. Signal journal, opening-burst rows only ----------------------
    print_header("5. OPENING-MOVE SIGNALS (logs/signal_journal.csv)")
    journal_rows = rows_for_date(os.path.join(LOG_DIR, "signal_journal.csv"), date, date_col="date")
    ob_rows = [r for r in journal_rows if r.get("entry_method") == "OPENING_MOVE"]
    if not ob_rows:
        print("No OPENING_MOVE rows for this date (mode disabled, or nothing measured).")
    else:
        taken = [r for r in ob_rows if str(r.get("taken")).strip().lower() == "true"]
        print(f"  {len(ob_rows)} symbol(s) measured, {len(taken)} entry attempt(s) made, "
              f"{len(trades)} of those in trade_history.csv (filled)")
        if len(taken) > len(trades):
            print(f"  -> {len(taken) - len(trades)} attempted entry(ies) never filled - "
                  f"see the Opening-Move Experiment section of the email for the fill-rate line.")

    # ---- 6. session-metrics.py and analyze-journal.py ---------------------
    if not args.no_metrics:
        since = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=args.metrics_since_days)).strftime("%Y-%m-%d")
        for title, cmd in [
            (f"6. SESSION METRICS (--since {since})",
             [sys.executable, os.path.join(REPO, "ops", "session-metrics.py"), "--since", since]),
            (f"7. SIGNAL JOURNAL ANALYSIS (--date {date})",
             [sys.executable, os.path.join(REPO, "ops", "analyze-journal.py"), "--date", date]),
        ]:
            print_header(title)
            try:
                result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=120)
                print(result.stdout or "(no output)")
                if result.returncode != 0:
                    print(f"  [exit code {result.returncode}]")
                    if result.stderr:
                        print(result.stderr[-2000:])
            except Exception as e:
                print(f"  Could not run {cmd[1]}: {e}")
    else:
        print_header("6-7. SKIPPED (--no-metrics)")

    print("\nDone. Paste this whole block for the most complete single-shot read.")


if __name__ == "__main__":
    main()
