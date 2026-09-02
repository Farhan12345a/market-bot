# Getting email and text working

The blocker is not the code. `EmailNotifier` builds the report correctly and
saves it to `logs/reports/` every day. What fails is the socket: DigitalOcean
filters outbound SMTP, so ports 25/465/587 never connect from the Droplet.

Two ways out. They are not equal.

| | DO support ticket | HTTPS sender |
|---|---|---|
| Time to working | days, if ever | ~15 minutes |
| Can be declined | **yes, commonly** | no |
| Fixes text too | no | yes |
| Effort | one form | one API key + a code change |

Do both if you like — but the ticket is a lottery ticket, and the HTTPS path is
the fix. Details of the ticket are in `DO_SUPPORT_TICKET.md`.

---

## The code is already done

Both channels are built, wired and tested (61 cases). They ship **disabled**, so
nothing changes until you flip a flag. What is left is only account signup and
putting two or three keys on the Droplet — no code session needed, and I never
need to see a key.

---

## Email: use Resend

Chosen over Mailgun and SendGrid for one reason that matters here: **it needs no
domain.** With no verified domain, Resend lets you send from
`onboarding@resend.dev` to the address you signed up with — which is exactly
this use case, a report mailed to yourself. Mailgun and SendGrid both want
domain verification (DNS records, a wait) before they will send anything useful.

Free tier is 3,000/month, 100/day. This bot sends one a weekday.

    1. Sign up at resend.com with shahbazfarhan25@gmail.com
       (this must be the SAME address as notifications.resend.to)
    2. API Keys -> Create API Key -> copy it (starts with re_)
    3. On the Droplet:
         echo 'RESEND_API_KEY=re_xxxxxxxx' >> /etc/market-bot.env
         chmod 600 /etc/market-bot.env
    4. In config.yaml set notifications.resend.enabled: true
    5. Verify before waiting for 4pm:
         set -a && . /etc/market-bot.env && set +a
         venv/bin/python3 ops/test-notifications.py
    6. bash ops/deploy.sh

Note the interpreter: the bot runs from a virtualenv, so `python3` is the
WRONG python and will fail on imports that are installed perfectly well.
`systemctl cat market-bot | grep ExecStart` names the right one.

Verify a domain later only if the report should ever reach a second address.

---

## Text: use Pushover

    1. Install Pushover, sign in, copy your User Key from the main screen
    2. Create an application token at https://pushover.net/apps/build
       (name it "market-bot"; any icon)
    3. On the Droplet:
         echo 'PUSHOVER_TOKEN=xxxx' >> /etc/market-bot.env
         echo 'PUSHOVER_USER=xxxx'  >> /etc/market-bot.env
    4. In config.yaml set notifications.pushover.enabled: true
    5. bash ops/deploy.sh

$5 once per platform, no subscription. Not literally SMS — and better than it
for this: it arrives faster, it can have its own alert sound, and it costs
nothing per message. Real SMS via Twilio is ~$0.0079/message plus ~$1.15/month
for a number, and buys nothing you want here.

**If you only do one of the two, do Pushover.** The report is already saved to
`logs/reports/` every day, so email mostly buys convenience. What is genuinely
missing is a buzz when something is wrong at 09:31.

---

## Making systemd read the env file

One-time, on the Droplet:

    systemctl edit market-bot

Add:

    [Service]
    EnvironmentFile=-/etc/market-bot.env

Then `systemctl daemon-reload`. The leading `-` means a missing file is not an
error, so the bot still starts if the env file is ever absent.

---

## What gets sent where

| | Resend (email) | Pushover (phone) |
|---|---|---|
| Daily report | full HTML table | one-line summary: P&L, win rate, best/worst, take-profit fires |
| `send_alert()` | plain text | plain text |

Both fire independently — one failing does not stop the other, and neither can
raise into the trading loop. If every channel fails, the log says so explicitly
and the report is still on disk.

---

## The alerts (wired 2026-09-02)

`EmailNotifier.send_alert(subject, text)` had existed, tested and wired to both
channels, with **zero call sites** — so no alert had ever been delivered. On
2026-09-02 the session ended at 09:38:19 on the daily loss limit and nothing
said so until the journal was read hours later. `src/notifications/alerts.py`
is now the call-site layer, toggled under `notifications.alerts` in config.yaml:

| Alert | Fires when |
|---|---|
| `preflight` | every pre-market run, **PASS included** |
| `loss_limit` | the circuit breaker fires — sent *before* the flatten |
| `session_end` | every ending: realized + unrealized P&L, reason, entries, trades |
| `degraded` | the stream died and the session is on ~15-min-delayed REST |
| `crash` | an unhandled exception ended the session — sent *before* cleanup |
| `positions_left_open` | a flatten did not fully succeed; shares held overnight |

Deliberately NOT alerted: individual fills, individual exits, sub-threshold
velocity warnings. A channel that buzzes for ordinary events gets muted, and
then the six above stop arriving too.

Every path is fail-safe: an alert that cannot be delivered never interrupts
trading, and `ops/send-preflight-alert.py` always exits 0 so a notification
problem can never make the preflight itself look like a readiness failure.

### Still missing: the dead-process watchdog

**The bot not running at 09:25 on a trading day is still silent**, and it is
the most important gap left. A dead process cannot alert about itself, so this
needs something outside it — a cron or systemd timer on the Droplet that checks
`systemctl is-active market-bot` and sends if the answer is not `active`.

Note that `ops/preflight.sh` now sends its result, so a *scheduled* preflight
at ~09:05 covers most of this in practice: if the preflight alert does not
arrive, something is wrong at the box level. That is a weaker guarantee than a
real watchdog — it says the cron ran, not that the bot is healthy — but it
turns a silent morning into a noticeable one. Add to crontab:

    5 9 * * 1-5 cd /root/market-bot && bash ops/preflight.sh >/dev/null 2>&1

---

## The DigitalOcean ticket

Still worth filing (`DO_SUPPORT_TICKET.md`) but it is now strictly optional:
nothing above touches SMTP. See that file for why it is commonly declined.

---

## Rotate the leaked Gmail password

`config.yaml` carried a live Gmail app password in plaintext, committed since
the initial commit. It has been removed from the file, but **git history still
has it** — revoke it at https://myaccount.google.com/apppasswords. Nothing needs
it any more; SMTP is off and the HTTPS channels do not use it.
