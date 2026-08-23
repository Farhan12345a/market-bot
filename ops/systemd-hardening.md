# One-time systemd change: make a restart release the data websocket

Alpaca allows **one data websocket per account**. On `systemctl restart`, the
new process can start connecting while the old one's socket is still registered
server-side, and the new connection is refused with
`error: connection limit exceeded`.

The bot now handles this itself - it retries 4 times at 15s intervals before
giving up - so this is belt-and-braces, not a prerequisite. It closes the gap
rather than absorbing it.

Run once on the Droplet:

```
systemctl edit market-bot
```

Add:

```
[Service]
# Give the process time to close its websocket cleanly instead of being
# SIGKILLed with the connection still open.
KillSignal=SIGINT
TimeoutStopSec=30
# Wait after stopping before starting again, so Alpaca has released the
# previous connection before the new process asks for one.
RestartSec=10
```

Then:

```
systemctl daemon-reload
```

`KillSignal=SIGINT` matters most: the bot already handles KeyboardInterrupt by
flattening positions, saving the trade log, flushing the signal journal and
sending the report. A default SIGTERM skips all of that.

## If you still see `connection limit exceeded`

Something else holds the socket. In order of likelihood:

1. A second `market-bot` process - `pgrep -af "python.*src.main"` should show
   exactly one.
2. An Alpaca dashboard or another script streaming on the same keys.
3. A socket the server has not reaped. `systemctl stop market-bot`, wait 30s,
   `systemctl start market-bot`.
