"""
HTTPS delivery for notifications: Resend for email, Pushover for phone push.

Why this exists: the Droplet cannot open outbound SMTP. Ports 25/465/587 are
filtered by DigitalOcean, so `smtplib` has failed 100% of the time on every run
this bot has ever done, and no report has ever been delivered. Both senders here
speak plain HTTPS on 443 - the same port the bot already uses to talk to Alpaca
all day - which cannot be blocked without breaking the platform.

SECRETS LIVE IN THE ENVIRONMENT, NEVER IN config.yaml. config.yaml is committed,
and it already leaked one Gmail app password that way. Each sender reads its key
from an env var and reports itself unconfigured if the var is missing, so a
half-finished setup degrades to "no delivery" rather than to a crash.

Nothing in this module raises. A notification failing is never a reason to take
down a process that is holding real positions.
"""
import logging
import os

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 15
PUSHOVER_MESSAGE_LIMIT = 1024   # Pushover truncates past this; we trim first


class Sender:
    """Base: a named delivery channel that reports whether it's usable."""

    name = "sender"

    def available(self) -> bool:
        raise NotImplementedError

    def send(self, subject: str, text: str, html: str = None) -> bool:
        raise NotImplementedError


class ResendSender(Sender):
    """
    Email over https://api.resend.com/emails.

    Without a verified domain, Resend only allows sending FROM onboarding@resend.dev
    TO the address the account was registered with. That is exactly this use case -
    a daily report mailed to yourself - so no domain setup is needed. Verify a
    domain later only if the report should ever go to a second address.
    """

    name = "resend"
    ENDPOINT = "https://api.resend.com/emails"

    def __init__(self, config):
        cfg = config.get("notifications", {}).get("resend", {}) or {}
        self.enabled = cfg.get("enabled", False)
        self.sender = cfg.get("from", "onboarding@resend.dev")
        self.recipient = cfg.get("to")
        self.api_key = os.environ.get("RESEND_API_KEY", "").strip()

    def available(self) -> bool:
        if not self.enabled:
            return False
        if not self.api_key:
            logger.warning(
                "Resend is enabled but RESEND_API_KEY is not set in the environment - "
                "email will not be delivered. Add it to the systemd unit "
                "(Environment=RESEND_API_KEY=...) or /etc/market-bot.env, not to config.yaml."
            )
            return False
        if not self.recipient:
            logger.warning("Resend is enabled but notifications.resend.to is empty")
            return False
        return True

    def send(self, subject, text, html=None) -> bool:
        import requests

        payload = {
            "from": self.sender,
            "to": [self.recipient],
            "subject": subject,
            "html": html or f"<pre>{text}</pre>",
        }
        try:
            resp = requests.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.error(f"Resend request failed: {type(e).__name__}: {e}")
            return False

        if resp.status_code in (200, 201):
            logger.info(f"Report emailed via Resend to {self.recipient}")
            return True

        # 403 here almost always means the domain/recipient pairing, not the key.
        detail = resp.text[:300]
        if resp.status_code in (401, 403):
            logger.error(
                f"Resend rejected the request (HTTP {resp.status_code}): {detail}. "
                f"Check that RESEND_API_KEY is valid and, if 'from' is still "
                f"onboarding@resend.dev, that 'to' is the address the Resend "
                f"account was registered with."
            )
        else:
            logger.error(f"Resend returned HTTP {resp.status_code}: {detail}")
        return False


class PushoverSender(Sender):
    """
    Phone push over https://api.pushover.net/1/messages.json.

    Not SMS, and better than it for this purpose: it arrives faster, it can be
    given its own alert sound, and it costs nothing per message. Plain text only -
    a 40-row HTML trade table is not a push notification, so the report body is
    condensed to a summary line and the full report stays on disk and in email.
    """

    name = "pushover"
    ENDPOINT = "https://api.pushover.net/1/messages.json"

    def __init__(self, config):
        cfg = config.get("notifications", {}).get("pushover", {}) or {}
        self.enabled = cfg.get("enabled", False)
        self.token = os.environ.get("PUSHOVER_TOKEN", "").strip()
        self.user = os.environ.get("PUSHOVER_USER", "").strip()
        self.priority = cfg.get("priority", 0)

    def available(self) -> bool:
        if not self.enabled:
            return False
        missing = [n for n, v in (("PUSHOVER_TOKEN", self.token),
                                  ("PUSHOVER_USER", self.user)) if not v]
        if missing:
            logger.warning(
                f"Pushover is enabled but {' and '.join(missing)} not set in the "
                f"environment - push notifications will not be delivered."
            )
            return False
        return True

    def send(self, subject, text, html=None) -> bool:
        import requests

        message = text or subject
        if len(message) > PUSHOVER_MESSAGE_LIMIT:
            message = message[: PUSHOVER_MESSAGE_LIMIT - 3] + "..."

        try:
            resp = requests.post(
                self.ENDPOINT,
                data={"token": self.token, "user": self.user,
                      "title": subject, "message": message,
                      "priority": self.priority},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.error(f"Pushover request failed: {type(e).__name__}: {e}")
            return False

        if resp.status_code == 200:
            logger.info("Push notification delivered via Pushover")
            return True

        logger.error(f"Pushover returned HTTP {resp.status_code}: {resp.text[:300]}")
        return False


def build_senders(config):
    """
    Every configured-and-usable sender, in delivery order.

    Logs exactly which channels are live at startup, so "did my notification go
    anywhere?" is answerable from the first ten lines of the log rather than by
    waiting until 4pm to find out it didn't.
    """
    senders = []
    for cls in (ResendSender, PushoverSender):
        try:
            s = cls(config)
            if s.available():
                senders.append(s)
        except Exception as e:
            logger.error(f"Could not construct {cls.name} sender: {e}")

    if senders:
        logger.info(f"Notification channels active: {', '.join(s.name for s in senders)}")
    else:
        logger.info(
            "No HTTPS notification channels are active. The daily report is still "
            "saved to logs/reports/ - see ops/NOTIFICATIONS.md to enable delivery."
        )
    return senders


def notify(senders, subject, text, html=None) -> bool:
    """
    Fan out to every channel. Returns True if AT LEAST ONE delivered.

    Deliberately not first-success-wins: email and push are complementary, not
    redundant - the push tells you something happened, the email carries the
    report you actually read. Never raises, whatever any channel does.
    """
    delivered = False
    for s in senders:
        try:
            if s.send(subject, text, html):
                delivered = True
        except Exception as e:
            logger.error(f"Sender {s.name} raised, continuing: {type(e).__name__}: {e}")
    return delivered
