import os, sys, json, shutil, tempfile
from datetime import datetime, timedelta

from _repo import REPO, CONFIG, repo_file, sandbox_cwd
from src.notifications.email_notifier import EmailNotifier

tmp = tempfile.mkdtemp()
report_dir = os.path.join(tmp, "reports")
trades_file = os.path.join(tmp, "trades.json")

TRADES = [
    {"symbol": "NU", "entry_price": 12.0, "exit_price": 13.0, "qty": 100, "pl": 100.0,
     "pl_pct": 8.33, "exit_reason": "TRAILING_STOP", "entry_method": "THREE_BAR_MOMENTUM",
     "entry_rsi": 44.2, "exit_rsi": 61.0, "stop_loss_used": False, "timestamp": "2026-08-19T13:40:00"},
    {"symbol": "MRVL", "entry_price": 242.6, "exit_price": 239.0, "qty": 40, "pl": -144.45,
     "pl_pct": -1.49, "exit_reason": "FINAL_EXIT_-1.0%", "entry_method": "RAPID_INCREASE_IMMEDIATE",
     "entry_rsi": 55.0, "exit_rsi": 48.1, "stop_loss_used": True, "timestamp": "2026-08-19T13:36:00"},
]
with open(trades_file, "w") as f:
    json.dump(TRADES, f)

failures = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        failures.append(name)

today = datetime.now().strftime("%Y-%m-%d")
expected = os.path.join(report_dir, f"trading-report-{today}.html")

# ---- Test 1: email ENABLED but SMTP unreachable (today's real VPS situation) ----
cfg = {"notifications": {"report_dir": report_dir, "report_retention_days": 7,
       "email": {"enabled": True, "sender_email": "a@b.com", "sender_password": "x",
                 "recipient_email": "a@b.com", "smtp_server": "127.0.0.1", "smtp_port": 9}}}
n = EmailNotifier(cfg)
sent = n.send_daily_summary(trades_file=trades_file)
check("T1 email reports failure", sent is False)
check("T1 report saved anyway", os.path.exists(expected), expected)
body = open(expected).read() if os.path.exists(expected) else ""
check("T1 report has real content", "MRVL" in body and "NU" in body and "-144.45" in body)
check("T1 report has all columns", all(c in body for c in
      ["Entry Method", "Entry RSI", "Exit RSI", "Exit Reason", "Stop Loss?"]))

# ---- Test 2: email DISABLED entirely -> report must still save ----
shutil.rmtree(report_dir)
cfg2 = {"notifications": {"report_dir": report_dir, "report_retention_days": 7,
        "email": {"enabled": False}}}
n2 = EmailNotifier(cfg2)
sent2 = n2.send_daily_summary(trades_file=trades_file)
check("T2 returns False (no email)", sent2 is False)
check("T2 report still saved", os.path.exists(expected), expected)

# ---- Test 3: retention prunes >7d, keeps <=7d, ignores foreign files ----
def touch(name, content="x"):
    p = os.path.join(report_dir, name)
    with open(p, "w") as f: f.write(content)
    return p

old   = touch(f"trading-report-{(datetime.now()-timedelta(days=8)).strftime('%Y-%m-%d')}.html")
edge  = touch(f"trading-report-{(datetime.now()-timedelta(days=7)).strftime('%Y-%m-%d')}.html")
recent= touch(f"trading-report-{(datetime.now()-timedelta(days=3)).strftime('%Y-%m-%d')}.html")
ancient=touch(f"trading-report-{(datetime.now()-timedelta(days=400)).strftime('%Y-%m-%d')}.html")
foreign=touch("my-notes.html")
weird = touch("trading-report-notadate.html")

n2._prune_old_reports()
check("T3 8-day-old pruned", not os.path.exists(old))
check("T3 400-day-old pruned", not os.path.exists(ancient))
check("T3 exactly-7-day kept", os.path.exists(edge))
check("T3 3-day-old kept", os.path.exists(recent))
check("T3 today's kept", os.path.exists(expected))
check("T3 foreign file untouched", os.path.exists(foreign))
check("T3 malformed name untouched", os.path.exists(weird))

# ---- Test 4: unwritable report dir must not kill the run ----
cfg3 = {"notifications": {"report_dir": "/proc/cannot/write/here",
        "email": {"enabled": False}}}
try:
    EmailNotifier(cfg3).send_daily_summary(trades_file=trades_file)
    check("T4 disk failure does not raise", True)
except Exception as e:
    check("T4 disk failure does not raise", False, repr(e))

# ---- Test 5: no trades file -> graceful, no crash, no empty report ----
try:
    r = EmailNotifier(cfg2).send_daily_summary(trades_file=os.path.join(tmp, "nope.json"))
    check("T5 missing trades file handled", r is False)
except Exception as e:
    check("T5 missing trades file handled", False, repr(e))

# ---- Test 6: idempotent re-run same day overwrites, doesn't duplicate ----
before = len(os.listdir(report_dir))
EmailNotifier(cfg2).send_daily_summary(trades_file=trades_file)
after = len(os.listdir(report_dir))
check("T6 same-day rerun overwrites", before == after, f"{before} -> {after}")

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("ALL PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
