#!/usr/bin/env python3
"""
Deliver the preflight result to phone and email.

Called by ops/preflight.sh with the counters it already tracks. Separate from
preflight.sh because the notification stack is Python (Resend/Pushover over
HTTPS) and the check itself is shell - and because a check that can only be
read by a human sitting at a terminal is not a pre-market check, it is a
manual chore.

Sent on PASS as well as FAIL, deliberately. A silent morning is
indistinguishable from a bot that never woke up, and telling those two apart
is the entire point of running this before the bell.

Usage:  send-preflight-alert.py PASSED FAILED WARNINGS [detail...]
Exit code is always 0 - a notification that cannot be delivered must never
make the preflight itself look like it failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    try:
        passed = int(sys.argv[1]); failed = int(sys.argv[2]); warnings = int(sys.argv[3])
    except (IndexError, ValueError):
        print("usage: send-preflight-alert.py PASSED FAILED WARNINGS [detail...]",
              file=sys.stderr)
        return 0
    detail = " ".join(sys.argv[4:])

    try:
        import yaml
        from src.notifications.email_notifier import EmailNotifier
        from src.notifications import alerts

        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "config.yaml")) as fh:
            config = yaml.safe_load(fh)

        status = "FAIL" if failed else ("PASS with warnings" if warnings else "PASS")
        ok = alerts.preflight(
            config, EmailNotifier(config),
            status=status, passed=passed, failed=failed,
            warnings=warnings, detail=detail,
        )
        print(f"preflight alert {'sent' if ok else 'NOT sent (no channel configured)'}")
    except Exception as e:
        print(f"preflight alert failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
