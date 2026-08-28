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

    python3 ops/flatten-now.py          # show what would close, change nothing
    python3 ops/flatten-now.py --yes    # actually close
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually place the orders")
    args = ap.parse_args()

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
