# DigitalOcean support ticket — request outbound SMTP

Submit at **https://cloudsupport.digitalocean.com/s/createticket**
(or Control Panel → Support → Get Help → Create a Ticket).

Category: **Networking / Other**
Droplet: *(fill in the droplet name)*

---

## Subject

Request to lift outbound SMTP restriction (port 587) on my Droplet

## Body

Hello,

I'd like to request that the outbound SMTP restriction be lifted on my Droplet
*(NAME / IP)*.

**What I'm trying to do.** The Droplet runs a personal Python application that
sends me exactly one email per weekday: an end-of-day summary report, from my
own Gmail account to that same Gmail account. It is a report to myself, not a
mailing list. There is no signup form, no list, no third-party recipient, and no
possibility of unsolicited mail — the recipient address is hard-coded to my own
and the send happens once per day.

**Connection details.**
- Destination: smtp.gmail.com
- Port: 587, STARTTLS, authenticated with a Google App Password
- Volume: 1 message per weekday, roughly 20 per month

**What happens now.** The connection never establishes. The application logs
`Network is unreachable` and `TimeoutError` at the socket layer, before any
SMTP dialogue begins, which is consistent with the port being filtered upstream
rather than with an authentication or configuration problem on my side. The
same code and the same credentials connect successfully from other networks.

**Confirmations.**
- I have read and agree to abide by DigitalOcean's Acceptable Use Policy.
- I am not sending bulk, marketing, or transactional mail to third parties.
- My account is in good standing with no abuse reports.

If the restriction cannot be lifted, could you confirm that in the reply so I
can move the application to an HTTPS-based email API instead? I'd rather know
either way than keep retrying a blocked port.

Thank you,
*(your name)*

---

## Set expectations before you send this

DigitalOcean blocks SMTP by default on all newer accounts, and the published
guidance points people to third-party email services rather than promising an
unblock. **Requests from accounts without a long paid history are frequently
declined.** File it — it costs nothing and sometimes works — but treat it as
the slower of two paths, not the fix.

The reliable fix is in `ops/NOTIFICATIONS.md`: send over HTTPS (port 443, which
is obviously not blocked — the bot talks to Alpaca all day over it) instead of
SMTP. That path is unblockable by definition, because blocking it would break
every Droplet on the platform.
