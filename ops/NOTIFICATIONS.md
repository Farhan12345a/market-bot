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

## Email over HTTPS

Any of these work; all are free at this volume (one message a weekday).

**Resend** — simplest. Sign up, verify a domain *or* use their sandbox sender,
create an API key. Sending is one POST to `https://api.resend.com/emails`.
Free tier: 3,000/month.

**Mailgun / SendGrid** — same shape, slightly more setup. Pick these if you
already have an account.

The change on our side is small: `EmailNotifier.send_daily_summary` currently
ends in an `smtplib` call. It grows a `transport:` config key — `smtp` (today's
behaviour, kept as the default so nothing changes for anyone) or `https` — and
the HTTPS branch POSTs the same HTML body it already builds. The report-saving
and retention logic is untouched, since that already happens before any send is
attempted.

**I can implement this in one pass once you have an API key.** Say the word and
paste the key into the Droplet's environment (not into git).

---

## Text messages

DigitalOcean has nothing to do with SMS — no ticket will fix this. Three real
options, best first:

**1. Pushover** — you've used it before. $5 once per platform, no subscription.
Delivers to the phone as a push notification with sound, which is what you
actually want at 09:35 on a trading morning; it is not literally SMS, but it
arrives faster and more reliably than one. Needs a User Key (in the app) and an
application API token (create one at pushover.net/apps/build). Sending is a
single POST to `https://api.pushover.net/1/messages.json`.

**2. Carrier email-to-SMS gateway** — free, and rides on whatever email
transport you end up with: send to `5551234567@vtext.com` (Verizon),
`@txt.att.net` (AT&T), `@tmomail.net` (T-Mobile). Zero extra services. The
catch is that carriers have been quietly retiring these gateways and delivery
is best-effort with no error when it silently drops.

**3. Twilio** — real SMS, ~$0.0079 a message plus ~$1.15/month for a number.
Only worth it if you specifically need a genuine SMS rather than a push.

**Recommendation: Pushover.** It's the cheapest, it's the most reliable, you
already know it, and it solves alerting for everything else too — a stream
disconnect at 09:31, the daily loss limit firing, the bot dying mid-session.
Those matter far more than the end-of-day report, which is already on disk.

---

## What to send, once something works

Not just the daily report. Ranked by how much you'd want the interruption:

1. **The bot is not running** at 09:25 ET on a trading day. Silence currently
   looks identical to a healthy morning.
2. **Daily loss limit fired** — everything just got flattened.
3. **Stream fell back to REST** at the open, meaning entries are running on
   15-minute-delayed prices again.
4. Daily report at the close. The one you have now, and the least urgent of the
   four — it's saved to disk either way.
