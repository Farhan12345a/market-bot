#!/usr/bin/env python3
"""
Close every open position at market, right now.

For the overnight-leftover case: positions the 16:00 time stop did not flatten
sit there occupying max_concurrent_positions slots at the next open. On
2026-08-28 six of ten slots were taken before the bell, which would have left
the opening-move experiment four slots instead of seven and the normal session
none.

Goes through Executor.flatten_all_positions, the same confirmed-order path the
16:00 stop uses, rather than issuing raw sells - so a partial failure is
reported per symbol instead of assumed.

SAFE TO RUN WHILE THE BOT IS RUNNING. It reconciles from the broker on every
poll, so it notices the positions are gone. Still: prefer running it OUTSIDE
market hours, because closing a position the bot is actively managing mid-trade
is not the same thing as tidying up leftovers.

    ./venv/bin/python3 ops/flatten-now.py        # show what would close
    ./venv/bin/python3 ops/flatten-now.py --yes  # actually close

Credentials are read from /etc/market-bot.env (the same EnvironmentFile the
systemd unit uses), so this needs no exports in the shell you run it from.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENV_FILES = ("/etc/market-bot.env", "/root/market-bot/.env", ".env")


def load_credentials():
    """Put the API keys in os.environ the way systemd does for the service.

    The bot gets its keys from the unit's EnvironmentFile. A shell you SSH into
    has never read that file, so running this script by hand died with
    "APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set" on a box where the
    service itself was authenticating fine. Read the same file the unit reads.

    Anything already exported wins, so `APCA_API_KEY_ID=... flatten-now.py`
    still overrides the file.
    """
    for path in ENV_FILES:
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)

    if os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY"):
        return True

    print("No Alpaca credentials found.")
    print("Looked in the environment and in: " + ", ".join(ENV_FILES))
    print("Fix either of these:")
    print("  1. Add them to /etc/market-bot.env (the file systemd reads), or")
    print("  2. Run this with them exported:")
    print("       APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... "
          "./venv/bin/python3 ops/flatten-now.py --yes")
    print("Or close the positions by hand: Alpaca paper dashboard -> Positions.")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually place the orders")
    args = ap.parse_args()

    if not load_credentials():
        sys.exit(1)

    import yaml
    from src.broker.alpaca_broker import AlpacaBroker
    from src.executor.executor import Executor

    config = yaml.safe_load(open("config.yaml"))
    broker = AlpacaBroker(paper=True)
    positions = broker.get_positions()

    if not positions:
        print("No open positions. Nothing to do.")
        return

    print(f"{len(positions)} open position(s):\n")
    for sym, p in sorted(positions.items()):
        qty = getattr(p, "qty", "?")
        avg = getattr(p, "avg_entry_price", "?")
        pl = getattr(p, "unrealized_pl", None)
        print(f"  {sym:<6} {qty:>8} shares @ {avg}"
              + (f"   unrealized {float(pl):+.2f}" if pl is not None else ""))

    if not args.yes:
        print("\nDry run - nothing was closed. Re-run with --yes to close them.")
        return

    print("\nClosing at market...")
    executor = Executor(broker, config)
    flattened = executor.flatten_all_positions()

    print(f"\nConfirmed closed: {', '.join(flattened) if flattened else 'none'}")
    remaining = broker.get_positions()
    if remaining:
        print(f"STILL OPEN: {', '.join(sorted(remaining))}")
        print("Those orders did not confirm. Check the Alpaca dashboard.")
        sys.exit(1)
    print("All positions closed.")


if __name__ == "__main__":
    main()
