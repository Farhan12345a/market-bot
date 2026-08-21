#!/usr/bin/env python3
"""
Send a test notification through the REAL delivery path and report exactly
what happened.

    cd /root/market-bot && python3 ops/test-notifications.py

Uses the same config, the same env vars and the same sender classes the bot
uses at 4pm, so a pass here means the daily report will deliver. Sends nothing
but a short test message - it does not touch trades, logs or the report.
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

import yaml
from src.notifications.senders import build_senders, notify

CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


def main():
    config = yaml.safe_load(open(CFG_PATH))
    notif = config.get("notifications", {})

    print("\n=== config ===")
    for name in ("resend", "pushover"):
        print(f"  {name:9} enabled: {notif.get(name, {}).get('enabled', False)}")
    print(f"  {'smtp':9} enabled: {notif.get('email', {}).get('enabled', False)}")

    print("\n=== environment ===")
    for var in ("RESEND_API_KEY", "PUSHOVER_TOKEN", "PUSHOVER_USER"):
        val = os.environ.get(var, "")
        # Never print a key. Enough to tell "set" from "set to the wrong thing".
        print(f"  {var:18} {'set (' + str(len(val)) + ' chars, ends ...' + val[-4:] + ')' if val else 'NOT SET'}")

    resend_to = notif.get("resend", {}).get("to")
    resend_from = notif.get("resend", {}).get("from")
    if notif.get("resend", {}).get("enabled") and resend_from == "onboarding@resend.dev":
        print(f"\n  NOTE: sending from the shared onboarding@resend.dev sender, so")
        print(f"        '{resend_to}' MUST be the address the Resend account was")
        print(f"        registered with, or Resend replies 403. If the test email you")
        print(f"        already received went to a different address, change")
        print(f"        notifications.resend.to in config.yaml to match it.")

    print("\n=== building senders ===")
    senders = build_senders(config)
    if not senders:
        print("\nRESULT: no channels active - nothing was sent.")
        print("Set enabled: true in config.yaml and put the keys in the environment.")
        print("If running by hand, the systemd EnvironmentFile is not loaded - try:")
        print("  set -a && . /etc/market-bot.env && set +a && python3 ops/test-notifications.py")
        return 1

    print("\n=== sending ===")
    ok = notify(
        senders,
        "market-bot: notification test",
        "If you are reading this, the daily report will deliver too.",
        "<h2>market-bot notification test</h2>"
        "<p>If you are reading this, the daily report will deliver too.</p>",
    )

    print()
    if ok:
        print("RESULT: delivered. Check your inbox / phone.")
        print("        Nothing else to do - the 4pm report uses this exact path.")
        return 0
    print("RESULT: every channel failed. The errors above say why.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
