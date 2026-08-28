#!/usr/bin/env python3
"""
Close every open position at market, right now.

For the overnight-leftover case: positions the 16:00 time stop did not flatten
sit there occupying max_concurrent_positions slots at the next open. On
2026-08-28 six of ten slots were taken before the bell, which would have left
the opening-move experiment four slots instead of seven and the normal session
none.

Uses Alpaca's own close_all_positions, NOT Executor.flatten_all_positions.

That matters, and it is not a stylistic preference. flatten_all_positions
submits side="sell" for every position unconditionally. On a LONG position that
closes it; on a SHORT position it doubles it. On 2026-08-28 three of the six
leftovers were short (CRWD -39, OKTA -52, MTCH -4) - the residue of the phantom
-position bug - and the sell orders this script queued premarket would have taken
CRWD to -78 and OKTA to -104 at the bell. Alpaca's close_position picks the side
from the position's own sign, so it covers a short by buying.

cancel_orders=True clears any working order first, because an opposite-side
order open on the same symbol makes Alpaca reject the close as a potential wash
trade.

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


def show_orders(client):
    """Every open order, with the field that actually matters: side.

    A queued premarket order is invisible as a position and easy to mistake for
    one in the dashboard, and its SIDE is what decides whether it closes a
    short or deepens it.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as e:
        print(f"Could not read open orders: {e}")
        return

    if not orders:
        print("Open orders: none.")
        return

    print(f"{len(orders)} open order(s):\n")
    for o in orders:
        side = str(getattr(o, "side", "?")).split(".")[-1].lower()
        print(f"  {o.symbol:<6} {side:<5} {getattr(o, 'qty', '?'):>6}"
              f"  status={str(getattr(o, 'status', '?')).split('.')[-1].lower()}"
              f"  id={getattr(o, 'id', '?')}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually place the orders")
    ap.add_argument("--cancel-only", action="store_true",
                    help="cancel working orders and stop; touch no positions")
    args = ap.parse_args()

    if not load_credentials():
        sys.exit(1)

    from src.broker.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(paper=True)
    client = broker.trading_client

    # Orders FIRST, and always. "I cancel them and they come back" is ambiguous
    # between three states that look identical in the dashboard: an order that
    # will not cancel, an order something keeps resubmitting, and a POSITION
    # being mistaken for an order. Print ids and statuses so it is none of them.
    show_orders(client)

    if args.cancel_only:
        print("\nCancelling all working orders...")
        try:
            client.cancel_orders()
        except Exception as e:
            print(f"cancel_orders failed: {e}")
            sys.exit(1)
        show_orders(client)
        print("\nPositions were NOT touched.")
        return

    positions = broker.get_positions()

    if not positions:
        print("No open positions. Nothing to do.")
        return

    shorts = []
    print(f"{len(positions)} open position(s):\n")
    for sym, p in sorted(positions.items()):
        qty = float(getattr(p, "qty", 0) or 0)
        avg = getattr(p, "avg_entry_price", "?")
        pl = getattr(p, "unrealized_pl", None)
        if qty < 0:
            shorts.append(sym)
        print(f"  {sym:<6} {qty:>9.0f} shares @ {avg}"
              + (f"   unrealized {float(pl):+.2f}" if pl is not None else "")
              + ("   [SHORT - closing BUYS]" if qty < 0 else ""))

    if shorts:
        print(f"\n{len(shorts)} short position(s): {', '.join(shorts)}.")
        print("Closing a short means BUYING to cover. This uses Alpaca's")
        print("close_position, which takes the side from the position itself.")

    if not args.yes:
        print("\nDry run - nothing was closed. Re-run with --yes to close them.")
        return

    # Premarket, a market order is accepted and then queues until 09:30, so
    # "did not confirm" here is the normal outcome rather than a failure. Say
    # which one it is instead of leaving the reader to guess.
    print("\nCancelling working orders and closing at market...")
    try:
        client.close_all_positions(cancel_orders=True)
    except Exception as e:
        print(f"close_all_positions failed: {e}")
        print("Close them by hand: Alpaca paper dashboard -> Positions -> Close All.")
        sys.exit(1)

    remaining = broker.get_positions()
    if not remaining:
        print("All positions closed.")
        return

    print(f"\nStill showing open: {', '.join(sorted(remaining))}")
    print("If the market is closed, the closing orders are QUEUED and will fill")
    print("at 09:30 - this is expected. Confirm under Orders in the dashboard:")
    print("each one should be the OPPOSITE side of its position (buy to cover a")
    print("short, sell to close a long). Re-run this after the open to verify.")


if __name__ == "__main__":
    main()
