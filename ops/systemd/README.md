# Watchdog install

The bot cannot alert about being dead - every alert it sends is sent BY it.
This runs outside the process.

## Option A: systemd timer (preferred)

    cp ops/systemd/market-bot-watchdog.* /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now market-bot-watchdog.timer
    systemctl list-timers market-bot-watchdog.timer     # confirm the next run

The timer's `OnCalendar` is evaluated in the SYSTEM timezone. Either set the
box to ET (`timedatectl set-timezone America/New_York`) or rewrite the hours in
UTC - a watchdog running at the wrong hours is worse than none, because it
looks installed.

## Option B: cron

    crontab -e
    */5 13-20 * * 1-5 /root/market-bot/ops/watchdog.sh >/dev/null 2>&1

Hours here are UTC (13-20 UTC covers 09:00-16:00 ET during EDT). Revisit after
the DST change in November.

## Verify it actually alerts

Do this once, deliberately, rather than assuming:

    systemctl stop market-bot
    ./ops/watchdog.sh            # expect a SERVICE NOT RUNNING line + a push
    systemctl start market-bot
    ./ops/watchdog.sh            # expect silence, exit 0

Silence on the second run is the point: a watchdog that reports success every
morning is one you stop reading, and then it stops working.

Requires the Pushover/Resend keys in /etc/market-bot.env - see
ops/NOTIFICATIONS.md. Without them the watchdog prints to stdout and cron
swallows it, which is no better than nothing.
