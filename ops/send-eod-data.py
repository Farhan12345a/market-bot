#!/usr/bin/env python3
"""
Email everything needed to analyze today's session as attachments, over the
same Resend channel the daily report already uses: trade_history.csv,
signal_journal.csv, trade_context.csv and daily_summary.csv (today's rows),
today's trading.log lines, the rendered HTML report if one was written, and a
snapshot of config.yaml as it stood when this ran.

WHY THIS EXISTS. Every day so far, getting these files to Claude for analysis
has meant SSHing in and running 3-4 separate grep commands by hand, then
pasting the output - and MISSING files has already happened once
(trade_context.csv was forgotten on the first pass). This automates the
"gather today's rows" half of that - it does not do the analysis, it just
gets the raw material into an email instead of a terminal session.

config.yaml is included because every analysis so far has silently assumed
"today's config is whatever I have locally" - true by coincidence, not
guaranteed, and wrong the moment settings on the VPS diverge from what's in
the dev copy (which happens every deploy). Every setting in it changes what
the day's numbers mean, so it goes in every snapshot rather than being
reconstructed from memory later. No secrets live in it by design (see
senders.py's module docstring), so nothing here is sensitive.

    cd /root/market-bot
    set -a && . /etc/market-bot.env && set +a
    venv/bin/python3 ops/send-eod-data.py

Cron, run once shortly after the close (16:05 ET / 20:05 UTC during EDT):

    5 20 * * 1-5 cd /root/market-bot && set -a && . /etc/market-bot.env && \
        set +a && venv/bin/python3 ops/send-eod-data.py >> logs/eod-snapshot.log 2>&1

Adjust the UTC hour for EST (20:05 UTC -> 21:05 UTC) when DST ends - cron has
no timezone awareness here, same caveat as everywhere else in this repo.

Each file is attached in full for TODAY's date only (grep on the leading
date column, same as every manual pull this week), so the attachments stay
small even as the season's CSVs grow. Silent no-op, never raises - a failed
snapshot email must never look like a failed trading day when someone
glances at the log.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from src.notifications.senders import ResendSender

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config.yaml")

# (source file relative to repo root, output attachment name) - each grepped
# down to today's date-prefixed rows only.
CSV_SOURCES = [
    ("logs/trade_history.csv", "trade_history_today.csv"),
    ("logs/signal_journal.csv", "signal_journal_today.csv"),
    ("logs/trade_context.csv", "trade_context_today.csv"),
    ("logs/daily_summary.csv", "daily_summary_today.csv"),
]


def today_csv_rows(path, today):
    """Header line + every row whose date column starts with today's date."""
    if not os.path.exists(path):
        return None
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()
    if not lines:
        return None
    header = lines[0]
    rows = [l for l in lines[1:] if l.startswith(today)]
    if not rows:
        return None
    return (header + "".join(rows)).encode("utf-8")


def today_log_lines(path, today):
    """Every trading.log line timestamped today - substitutes for journalctl,
    which needs sudo and isn't worth granting this script."""
    if not os.path.exists(path):
        return None
    out = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            if line.startswith(today):
                out.append(line)
    if not out:
        return None
    return "".join(out).encode("utf-8")


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    config = yaml.safe_load(open(CFG_PATH))
    sender = ResendSender(config)
    if not sender.available():
        print("Resend is not configured/available - nothing sent. "
              "See ops/NOTIFICATIONS.md.")
        return 1

    attachments = []
    missing = []
    had_any_data = False  # separate from attachments, since config.yaml is
                          # always attached below and must not mask "no data"
    for rel_path, out_name in CSV_SOURCES:
        content = today_csv_rows(os.path.join(ROOT, rel_path), today)
        if content:
            attachments.append({"filename": out_name, "content": content})
            had_any_data = True
        else:
            missing.append(rel_path)

    log_content = today_log_lines(os.path.join(ROOT, "logs/trading.log"), today)
    if log_content:
        attachments.append({"filename": "trading_log_today.txt", "content": log_content})
        had_any_data = True
    else:
        missing.append("logs/trading.log")

    # The rendered daily report, if send_daily_summary already wrote one -
    # see email_notifier.py's REPORT_DIR/naming. A cross-check against this
    # script's own CSV pulls, and readable on its own without a CSV parser.
    report_path = os.path.join(ROOT, "logs", "reports", f"trading-report-{today}.html")
    if os.path.exists(report_path):
        with open(report_path, "rb") as f:
            attachments.append({"filename": f"trading-report-{today}.html", "content": f.read()})
        had_any_data = True
    else:
        missing.append(report_path)

    if not had_any_data:
        print(f"No data found for {today} in any source file - nothing sent.")
        return 1

    # config.yaml AS OF THIS RUN - every day's numbers only mean what they
    # mean in light of whatever was actually deployed that day. Attached
    # only once real data exists to send alongside it, not on its own.
    # No secrets live in this file by design - see senders.py.
    with open(CFG_PATH, "rb") as f:
        attachments.append({"filename": f"config-{today}.yaml", "content": f.read()})

    subject = f"[market-bot] EOD data snapshot - {today}"
    text = (
        f"Today's raw data, attached: {', '.join(a['filename'] for a in attachments)}.\n\n"
        + (f"No rows found for: {', '.join(missing)}.\n\n" if missing else "")
        + "This is the raw material for analysis, not a summary - forward the "
          "attachments along when asking for the day's review."
    )
    ok = sender.send(subject, text, attachments=attachments)
    print("Sent." if ok else "Send failed - check the log above for the Resend error.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
