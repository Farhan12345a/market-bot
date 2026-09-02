"""
Operational alerts - the things worth a phone buzz while the session is
running, as distinct from the end-of-day report.

WHY THIS EXISTS. EmailNotifier.send_alert() has been built, wired to both
channels and tested since it was written, and had ZERO call sites - so no
alert has ever been delivered. On 2026-09-02 the account hit its daily loss
limit at 09:38:19 and the session ended eight minutes after the open; the
first anyone knew was reading the journal hours later. Everything here is a
call site for that existing machinery.

WHAT EARNS AN ALERT. The bar is "would you want to be interrupted for this",
not "is this interesting". Four things clear it:

  - the day ENDED, and how it went (P&L realised and unrealised)
  - the day ended BADLY and early (the loss limit fired)
  - the bot cannot trade properly right now (stream dead, preflight failed)
  - something was left in a state a human must resolve (positions still open
    after a flatten, an unhandled crash)

Everything else - individual fills, individual exits, velocity warnings below
the top threshold - stays in the log. A channel that buzzes for ordinary
events is one that gets muted, and then the four above do not arrive either.

Every function here is FAIL-SAFE: an alert that cannot be delivered must never
interrupt trading, so all of them swallow their exceptions and log instead.
"""

import logging

logger = logging.getLogger(__name__)


def _send(notifier, subject, text):
    """Deliver one alert. Never raises - trading continues regardless."""
    try:
        if notifier is None:
            logger.info(f"ALERT (no notifier): {subject} - {text}")
            return False
        return bool(notifier.send_alert(subject, text))
    except Exception as e:
        logger.error(f"Alert delivery failed ({subject}): {type(e).__name__}: {e}")
        return False


def _enabled(config, key, default=True):
    cfg = ((config or {}).get("notifications") or {}).get("alerts") or {}
    if not cfg.get("enabled", True):
        return False
    return bool(cfg.get(key, default))


def _money(x):
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def session_ended(config, notifier, *, reason, realized, unrealized,
                  entries, trades, open_positions=0, equity=None, started_at=None,
                  ended_at=None):
    """
    The day is over. Sent for EVERY ending, not only bad ones - "it finished
    normally" is information too, and its absence is how you notice the
    process died without saying anything.
    """
    if not _enabled(config, "session_end"):
        return False

    total = None
    try:
        total = float(realized or 0) + float(unrealized or 0)
    except (TypeError, ValueError):
        pass

    bad = reason in ("daily_loss_limit",) or (total is not None and total < 0)
    mark = "LOSS" if (total is not None and total < 0) else "OK"
    subject = f"[market-bot] Session ended ({reason}) - {mark} {_money(total)}"

    lines = [
        f"Reason:      {reason}",
        f"Realized:    {_money(realized)}",
        f"Unrealized:  {_money(unrealized)}",
        f"Total P&L:   {_money(total)}",
        f"Entries:     {entries}",
        f"Trades:      {trades}",
    ]
    if open_positions:
        lines.append(f"STILL OPEN:  {open_positions} position(s) - check the broker")
    if equity is not None:
        lines.append(f"Equity:      {_money(equity)}")
    if started_at:
        lines.append(f"Started:     {started_at}")
    if ended_at:
        lines.append(f"Ended:       {ended_at}")
    if bad:
        lines.append("")
        lines.append("The day ended on the loss limit or in the red - the full "
                     "report has the per-trade breakdown.")
    return _send(notifier, subject, "\n".join(lines))


def loss_limit_hit(config, notifier, *, daily_pnl, limit, entries, elapsed_minutes=None):
    """
    The circuit breaker fired. Sent SEPARATELY from session_ended and before
    it, because this is the one that should arrive while it is still news.
    """
    if not _enabled(config, "loss_limit"):
        return False
    pace = ""
    try:
        if elapsed_minutes and float(elapsed_minutes) > 0:
            pace = f" ({_money(abs(float(daily_pnl)) / float(elapsed_minutes))}/min)"
    except (TypeError, ValueError):
        pass
    subject = f"[market-bot] DAILY LOSS LIMIT HIT - {_money(daily_pnl)}"
    text = (
        f"The daily loss limit fired and every position is being flattened.\n\n"
        f"Loss:     {_money(daily_pnl)} against a {_money(limit)} limit\n"
        f"Entries:  {entries} taken before it fired\n"
        + (f"Elapsed:  {elapsed_minutes:.0f} min into the session{pace}\n" if elapsed_minutes else "")
        + "\nNo further entries today. Open positions are being closed now."
    )
    return _send(notifier, subject, text)


def degraded(config, notifier, *, what, detail):
    """
    The bot is still running but cannot trade properly - the stream died and
    prices are ~15 minutes delayed, the screener timed out, the broker is
    refusing orders. Worth knowing DURING the session, because the decision
    (let it run on REST, or stop it) is yours and only useful while the market
    is open.
    """
    if not _enabled(config, "degraded"):
        return False
    return _send(notifier, f"[market-bot] DEGRADED: {what}", detail)


def crashed(config, notifier, *, error, where="run_trading_day"):
    """An unhandled exception took the session down."""
    if not _enabled(config, "crash"):
        return False
    return _send(
        notifier,
        "[market-bot] CRASHED - the session stopped unexpectedly",
        f"An unhandled error ended the session in {where}.\n\n"
        f"{type(error).__name__}: {error}\n\n"
        f"Positions may still be open at the broker - check before the close.",
    )


def positions_left_open(config, notifier, *, symbols, when):
    """
    The most dangerous state the bot can leave behind: a flatten that did not
    fully succeed, so shares are held overnight against a strategy that never
    intended to hold anything past the close.
    """
    if not _enabled(config, "positions_left_open"):
        return False
    return _send(
        notifier,
        f"[market-bot] POSITIONS STILL OPEN after {when}",
        "The bot tried to flatten and these remain at the broker:\n\n"
        + "\n".join(f"  - {s}" for s in symbols)
        + "\n\nThese will be held overnight unless closed manually. "
          "ops/flatten-now.py --yes closes them.",
    )


def preflight(config, notifier, *, status, passed, failed, warnings=0, detail=""):
    """
    Pre-market readiness. Sent every run, PASS included - a silent morning is
    indistinguishable from a bot that never woke up, and that distinction is
    the entire point of a pre-market check.
    """
    if not _enabled(config, "preflight"):
        return False
    subject = f"[market-bot] Preflight {status} - {passed} passed, {failed} failed"
    if warnings:
        subject += f", {warnings} warning(s)"
    text = f"Pre-market check: {status}\n\nPassed:   {passed}\nFailed:   {failed}\n"
    if warnings:
        text += f"Warnings: {warnings}\n"
    if detail:
        text += f"\n{detail}"
    if failed:
        text += "\n\nThe session may not trade correctly. Check before 09:30."
    return _send(notifier, subject, text)
