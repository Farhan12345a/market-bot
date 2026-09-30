import smtplib
import json
import logging
import glob
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import os

from src.notifications.senders import build_senders, notify
from src.analytics.performance_timeline import render_performance_timeline_html

logger = logging.getLogger(__name__)

REPORT_DIR = "logs/reports"
REPORT_RETENTION_DAYS = 7
_REPORT_NAME_RE = re.compile(r"^trading-report-(\d{4}-\d{2}-\d{2})\.html$")


def _peak_signal_note(ctx):
    """Whether the ceiling actually bound today, in words."""
    peak = ctx.get("peak_signal_pct") or 0
    ceiling = ctx.get("rapid_increase_max_pct") or 0
    sym = ctx.get("peak_signal_symbol")
    if not peak:
        return "no signals"
    who = f"{sym} " if sym else ""
    if not ceiling:
        return f"{who}(no ceiling set)"
    if peak > ceiling:
        return f"{who}- ceiling BOUND"
    return f"{who}- ceiling never bound"


class EmailNotifier:
    """
    Builds the daily trading report, saves it to disk, and (if configured)
    emails it.

    Saving to disk is deliberately NOT conditional on the email working, or
    even on email being enabled at all. Originally the report was generated
    in memory inside send_daily_summary() purely as the email body, so when
    the SMTP send failed the report was discarded with it - and SMTP fails
    100% of the time on the current DigitalOcean droplet, which blocks
    outbound port 587. The result was that no report from any run had ever
    been recoverable. The report is now written before the send is even
    attempted, so a broken (or disabled) mail path can never destroy it.
    """

    def __init__(self, config):
        self.config = config
        self.email_config = config.get("notifications", {}).get("email", {})
        self.enabled = self.email_config.get("enabled", False)

        notif_config = config.get("notifications", {})
        self.report_dir = notif_config.get("report_dir", REPORT_DIR)
        self.report_retention_days = notif_config.get(
            "report_retention_days", REPORT_RETENTION_DAYS
        )

        # HTTPS delivery (Resend / Pushover). Independent of self.enabled, which
        # only ever governed the SMTP path - so turning SMTP off, as anyone on a
        # DigitalOcean box eventually does, no longer silently turns off every
        # other channel with it.
        self.senders = build_senders(config)

        # Run context for the report header - what data path this session
        # actually used. Set by main once the symbol list and stream state are
        # known. Every documented test run needs this to be comparable to the
        # next one; "was this on ticks or bar closes, and over how many
        # symbols?" is not answerable after the fact from P&L alone.
        self.run_context = {}

        if not self.enabled:
            logger.info("Email notifications disabled (daily report will still be saved to disk)")
            return

        self.sender_email = self.email_config.get("sender_email")
        # Environment first. The config key is still read as a fallback for any
        # host with a working SMTP route, but it must not be filled in on a
        # committed file - that is how the last app password leaked.
        self.sender_password = (
            os.environ.get("SMTP_PASSWORD", "").strip()
            or self.email_config.get("sender_password")
        )
        self.recipient_email = self.email_config.get("recipient_email")
        self.smtp_server = self.email_config.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = self.email_config.get("smtp_port", 587)

        if not all([self.sender_email, self.sender_password, self.recipient_email]):
            logger.warning("Email notifications enabled but credentials missing")
            self.enabled = False

    def send_daily_summary(self, trades_file="logs/trades.json", burst_summary=""):
        """The end-of-session report. See send_report."""
        return self.send_report(trades_file, burst_summary=burst_summary, label="Daily Summary")

    def send_report(self, trades_file="logs/trades.json", burst_summary="",
                    label="Daily Summary", open_positions=None):
        """
        Build the report, save it to disk, then deliver it.

        `label` distinguishes the several sends a single day now makes (a
        midday status at 10:35, one the moment the last position closes, one
        at the close) so an inbox with three of them is readable at a glance.

        `open_positions` is a list of still-open position rows. A midday
        report showing only CLOSED trades would be actively misleading - on a
        morning holding eight positions it would report an empty day.

        Returns True only if at least one channel delivered. The saved-to-disk
        report is independent of that and of self.enabled.
        """
        open_positions = open_positions or []
        try:
            trades = []
            if os.path.exists(trades_file):
                # A malformed trades file must not cost the whole report. On
                # 2026-08-21 the 10:35 Midday Status died outright on
                # "Expecting value: line 16 column 17" and delivered nothing,
                # even though the open-position data it also carries was fine
                # and came from memory, not from this file.
                try:
                    with open(trades_file) as f:
                        trades_data = json.load(f)
                    trades = (trades_data if isinstance(trades_data, list)
                              else trades_data.get("trades", []))
                except (ValueError, OSError) as e:
                    logger.error(
                        f"Could not parse {trades_file} ({e}) - reporting open "
                        f"positions only. The closed-trade table will be empty "
                        f"for this send; the file is rewritten at the next save."
                    )
            else:
                logger.warning(f"No trades file found: {trades_file}")

            if not trades and not open_positions:
                logger.info(f"Nothing to report for '{label}' - no closed trades, no open positions")
                return False

            html_content = self._generate_html_summary(
                trades, burst_summary=burst_summary, label=label,
                open_positions=open_positions,
            )
        except Exception as e:
            logger.error(f"Error building report '{label}': {e}")
            return False

        # Save FIRST, before the email is attempted - see the class docstring.
        # Wrapped separately so a disk problem can't stop the email, and an
        # email problem can't stop the save.
        try:
            self._save_report(html_content)
        except Exception as e:
            logger.error(f"Could not save daily report to disk: {e}")

        try:
            self._prune_old_reports()
        except Exception as e:
            logger.error(f"Could not prune old reports: {e}")

        subject = f"Trading Bot {label} - {datetime.now().strftime('%Y-%m-%d')}"
        delivered = False

        # HTTPS channels first: on this host they are the ones that can work.
        if self.senders:
            delivered = notify(
                self.senders, subject,
                self._plain_text_summary(trades, open_positions), html_content,
            )

        if self.enabled:
            try:
                self._send_email(subject, html_content)
                logger.info(f"✓ Daily summary emailed to {self.recipient_email}")
                delivered = True
            except Exception as e:
                logger.error(f"Error sending email (report is still saved to disk): {e}")

        if not delivered:
            logger.warning(
                f"Report '{label}' was NOT delivered by any channel - it is saved at "
                f"{self.report_dir}/"
            )
        return delivered

    def _plain_text_summary(self, trades, open_positions=None):
        """
        The report condensed to something that fits in a push notification.

        A 40-row HTML table is not a phone alert. This is the line you want to
        read on a lock screen; the full report stays on disk and in email.
        """
        try:
            all_closed = [t for t in trades if t.get("exit_price") is not None]
            # Same primary-only filter as the HTML headline (see
            # _generate_html_summary) - the push notification's P&L must
            # agree with the report's, or the two disagree about "how did
            # today go" for no reason a reader could see. As of 2026-09-29
            # this includes primary-window shorts, same as the headline -
            # only entry_window == "extended" is still excluded.
            closed = [t for t in all_closed if t.get("entry_window") != "extended"]
            pl = sum(float(t.get("pl") or 0) for t in closed)
            wins = sum(1 for t in closed if float(t.get("pl") or 0) > 0)
            n = len(closed)
            win_rate = (wins / n * 100) if n else 0.0

            ranked = sorted(closed, key=lambda t: float(t.get("pl") or 0))
            ctx = self.run_context or {}
            lines = []
            if ctx:
                lines.append(
                    f"[{ctx.get('symbols_streamed', 0)}/{ctx.get('symbols_watched', 0)} streamed, "
                    f"ticks {'ON' if ctx.get('trade_ticks') else 'OFF'}, "
                    f"{ctx.get('price_source', '?')}]"
                )
            lines.append(f"P&L ${pl:+,.2f} on {n} round-trips, {win_rate:.0f}% win rate")
            if ranked:
                best, worst = ranked[-1], ranked[0]
                lines.append(f"Best  {best.get('symbol','?')} ${float(best.get('pl') or 0):+,.2f}")
                lines.append(f"Worst {worst.get('symbol','?')} ${float(worst.get('pl') or 0):+,.2f}")

            tp = sum(1 for t in closed if t.get("exit_reason") == "TAKE_PROFIT")
            if tp:
                lines.append(f"{tp} take-profit scale-out(s) fired")

            # Primary-window shorts are already inside `pl` above (see the
            # filter comment) - break them out here too so the split is
            # visible without a separate email. Only EXTENDED-hours trades
            # (either side) are still genuinely excluded from `pl`, so only
            # those get the "(separate)" framing.
            short_in_pl = [t for t in closed if t.get("side") == "short"]
            if short_in_pl:
                short_pl = sum(float(t.get("pl") or 0) for t in short_in_pl)
                lines.append(f"Of which, shorts: ${short_pl:+,.2f} on {len(short_in_pl)} trade(s)")
            ext_closed = [t for t in all_closed if t.get("entry_window") == "extended"]
            if ext_closed:
                ext_pl = sum(float(t.get("pl") or 0) for t in ext_closed)
                ext_shorts = sum(1 for t in ext_closed if t.get("side") == "short")
                lines.append(
                    f"Extended hours (separate, NOT in P&L above): "
                    f"${ext_pl:+,.2f} on {len(ext_closed)} trade(s), "
                    f"{ext_shorts} short/{len(ext_closed) - ext_shorts} long"
                )

            if open_positions:
                unreal = sum(float(p.get("unrealized_pl") or 0) for p in open_positions)
                lines.append(f"{len(open_positions)} still open, ${unreal:+,.2f} unrealized")
                lines.append(f"Combined ${pl + unreal:+,.2f}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"Could not build plain-text summary: {e}")
            return "Daily report is ready - see logs/reports/."

    def send_alert(self, subject, text):
        """
        Deliver a one-off operational alert (not the daily report).

        Separate from send_daily_summary because the useful alerts have nothing
        to do with trades: the process not running at 09:25, the daily loss
        limit firing, the price stream falling back to REST at the open. Those
        are worth a phone buzz; the end-of-day report largely is not, since it
        is already on disk.
        """
        if not self.senders:
            logger.info(f"ALERT (no channel configured): {subject} - {text}")
            return False
        return notify(self.senders, subject, text)

    def _save_report(self, html_content):
        """Write the report to logs/reports/trading-report-YYYY-MM-DD.html."""
        os.makedirs(self.report_dir, exist_ok=True)
        filename = f"trading-report-{datetime.now().strftime('%Y-%m-%d')}.html"
        path = os.path.join(self.report_dir, filename)
        with open(path, "w") as f:
            f.write(html_content)
        logger.info(f"✓ Daily report saved to {os.path.abspath(path)}")
        return path

    def _prune_old_reports(self):
        """
        Delete saved reports older than report_retention_days.

        Dates come from the FILENAME, not the file's mtime: an mtime is easy
        to bump by accident (a copy, an rsync, a backup restore) which would
        silently keep stale reports alive forever, whereas the date in the
        name is the date the report is actually about. Anything in the
        directory that doesn't match the expected report-name pattern is
        left strictly alone.
        """
        if not os.path.isdir(self.report_dir):
            return

        cutoff = (datetime.now() - timedelta(days=self.report_retention_days)).date()
        removed = 0

        for path in glob.glob(os.path.join(self.report_dir, "*.html")):
            match = _REPORT_NAME_RE.match(os.path.basename(path))
            if not match:
                continue
            try:
                report_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if report_date < cutoff:
                os.remove(path)
                removed += 1

        if removed:
            logger.info(
                f"Pruned {removed} report(s) older than {self.report_retention_days} days "
                f"from {self.report_dir}"
            )

    def _reentry_labels(self, trades):
        """
        Label each trade with whether it was a re-entry into a symbol traded
        earlier the same day, and how long after the previous exit.

        Computed here from the trade list rather than recorded at entry time,
        so it works on every report ever saved, including past ones. The
        cooldown (reentry_cooldown_minutes) only gates symbols that just LOST,
        so this column is how you see whether the setting is doing anything:
        a "2nd, +6m" row on a 5-minute cooldown was allowed by a hair, and a
        column with no re-entries at all means the cooldown is longer than the
        entry window and nothing can ever come back.
        """
        labels, seen = {}, {}
        for trade in sorted(trades, key=lambda x: str(x.get("timestamp", ""))):
            sym = trade.get("symbol")
            prev = seen.get(sym)
            if prev is None:
                labels[id(trade)] = "1st"
            else:
                n = prev["count"] + 1
                gap = ""
                try:
                    t_now = datetime.fromisoformat(str(trade.get("timestamp")))
                    t_prev = datetime.fromisoformat(str(prev["timestamp"]))
                    mins = (t_now - t_prev).total_seconds() / 60
                    gap = f", +{int(mins)}m"
                except Exception:
                    pass
                labels[id(trade)] = f"{self._ordinal(n)}{gap}"
            seen[sym] = {
                "count": (prev["count"] + 1) if prev else 1,
                "timestamp": trade.get("timestamp"),
            }
        return labels

    @staticmethod
    def _ordinal(n):
        return f"{n}{'th' if 11 <= n % 100 <= 13 else {1:'st',2:'nd',3:'rd'}.get(n % 10, 'th')}"

    def _run_context_html(self):
        """
        The band at the top of every report describing HOW the session ran.

        Deliberately first, above the P&L. Comparing two days' results is
        meaningless without knowing whether prices arrived by stream or by
        15-minute-delayed REST, whether entries used trade ticks or bar closes,
        and across how many symbols - and none of that is recoverable from the
        numbers afterwards.
        """
        ctx = self.run_context or {}
        if not ctx:
            return ""

        def cell(label, value, note=""):
            return (
                '<td style="padding:10px 14px;vertical-align:top;">'
                f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;'
                f'letter-spacing:.04em;">{label}</div>'
                f'<div style="font-size:16px;font-weight:600;">{value}</div>'
                + (f'<div style="font-size:11px;color:#6b7280;">{note}</div>' if note else "")
                + '</td>'
            )

        streamed = ctx.get("symbols_streamed")
        watched = ctx.get("symbols_watched")
        rest = ctx.get("symbols_rest")
        ticks = ctx.get("trade_ticks")
        source = ctx.get("price_source", "unknown")

        source_color = {"stream": "#10b981", "REST (stream failed)": "#ef4444",
                        "REST": "#6b7280"}.get(source, "#6b7280")

        cells = [
            cell("Total symbols", watched if watched is not None else "n/a",
                 ctx.get("symbols_note", "")),
            cell("Streamed live",
                 f"{streamed} of {watched}" if streamed is not None else "0",
                 f"{rest} on REST" if rest is not None else ""),
            cell("Trade ticks",
                 "ON" if ticks else "OFF",
                 "entry detection" if ticks else "bar closes only"),
            cell("Price source",
                 f'<span style="color:{source_color};">{source}</span>',
                 ctx.get("feed", "")),
            cell("Signal ceiling",
                 (f'{ctx.get("rapid_increase_max_pct")}%'
                  if ctx.get("rapid_increase_max_pct") else "none"),
                 f'floor {ctx.get("rapid_increase_pct", "?")}%'),
            # The peak sits next to the ceiling on purpose. A ceiling that never
            # binds reads exactly like one that is working, and on 2026-08-26 the
            # 2.0% setting had refused nothing since it shipped.
            cell("Peak signal today",
                 (f'{ctx.get("peak_signal_pct"):.3f}%'
                  if ctx.get("peak_signal_pct") else "-"),
                 _peak_signal_note(ctx)),
            cell("Resistance exit",
                 "ON" if ctx.get("use_resistance_exit") else '<span style="color:#ef4444;">OFF</span>',
                 "failed-breakout rule" if ctx.get("use_resistance_exit") else "DISABLED for this test"),
            cell("Re-entry cooldown",
                 f'{ctx.get("reentry_cooldown_minutes", "?")} min',
                 "after losses only" if ctx.get("reentry_cooldown_after_loss_only") else "after any exit"),
        ]

        return (
            '<div style="background:#f5f7fa;border-left:4px solid #1a1f2e;'
            'border-radius:8px;margin-bottom:20px;overflow-x:auto;">'
            '<table style="width:100%;border-collapse:collapse;">'
            '<tr>' + "".join(cells) + '</tr></table></div>'
        )

    def _open_positions_html(self, open_positions):
        """
        The open-positions table for a mid-session report.

        Deliberately separate from the closed-trade table: these rows have no
        exit price, no realised P&L and no final exit reason, and forcing them
        into the same 14 columns would mean a dozen "N/A"s per row. The
        interesting numbers while a position is still live are different ones -
        what it is worth now, and how far it has travelled in each direction.
        """
        if not open_positions:
            return ""

        rows = []
        for p in sorted(open_positions,
                        key=lambda x: float(x.get("unrealized_pl") or 0), reverse=True):
            pl = float(p.get("unrealized_pl") or 0)
            pl_pct = float(p.get("unrealized_pl_pct") or 0)
            cls = "profit" if pl >= 0 else "loss"
            mfe, mae = p.get("mfe_pct"), p.get("mae_pct")
            mfe_s = f"{mfe:+.2f}%" if isinstance(mfe, (int, float)) else "N/A"
            mae_s = f"{mae:+.2f}%" if isinstance(mae, (int, float)) else "N/A"
            rows.append(
                f"<tr><td class='symbol'>{p.get('symbol','N/A')}</td>"
                f"<td>${float(p.get('entry_price') or 0):,.2f}</td>"
                f"<td>${float(p.get('current_price') or 0):,.2f}</td>"
                f"<td>{p.get('qty_remaining', 0)} of {p.get('entry_qty', 0)}</td>"
                f"<td class='{cls}'>{pl_pct:+.2f}%</td>"
                f"<td class='{cls}'>${pl:,.2f}</td>"
                f"<td>{mfe_s}</td><td>{mae_s}</td>"
                f"<td class='exit-reason'>{p.get('entry_method') or 'N/A'}</td>"
                f"<td class='exit-reason'>{p.get('held_for') or 'N/A'}</td></tr>"
            )

        return (
            '<h2 style="margin-top:30px;border-bottom:2px solid #f59e0b;'
            'padding-bottom:10px;">Open Positions</h2>'
            '<table class="trades-table"><thead><tr>'
            '<th>Symbol</th><th>Entry</th><th>Current</th><th>Qty</th>'
            '<th>Unrealized %</th><th>Unrealized P&L</th>'
            '<th>Peak (MFE)</th><th>Trough (MAE)</th>'
            '<th>Entry Method</th><th>Held</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
        )

    def _replay_progress_html(self, context_file="logs/trade_context.csv"):
        """
        How much replay data exists, and what it currently supports.

        Shown every day because the number changes every day and because the
        honest answer to "what stop should I use?" depends entirely on it.
        Without this the temptation is to run ops/grid.py after three
        sessions, read the top row, and deploy a config that is pure noise -
        at 64 cells and 30 trades the best-looking cell sits about 2.5
        standard errors high by chance alone. See docs/REPLAY.md.
        """
        try:
            import csv as _csv
            import os as _os
            if not _os.path.exists(context_file):
                n = 0
            else:
                with open(context_file, newline="") as fh:
                    n = sum(1 for _ in _csv.DictReader(fh))
        except Exception:
            return ""

        if n < 50:
            stage, colour = "Not enough to conclude anything about a single config.", "#ef4444"
            advice = ("The marginal tables in ops/grid.py are a weak hint at best. "
                      "Do not read the leaderboard.")
        elif n < 200:
            stage, colour = "Plateau regions are becoming readable.", "#f59e0b"
            advice = ("Read the STOP / BREAKEVEN / TAKE-PROFIT marginal tables for a "
                      "region that is good across the board. Still no single winner.")
        elif n < 400:
            stage, colour = "Real differences (~$10/trade) start to separate.", "#3b82f6"
            advice = "Leaderboard intervals are becoming meaningful. Confirm across regimes."
        else:
            stage, colour = "A specific configuration can be defended.", "#10b981"
            advice = "Check --by-regime before settling on one global config."

        target = 400
        pct = min(100, int(round(100 * n / target)))
        return (
            '<div style="background:#f8fafc;border-left:4px solid ' + colour + ';'
            'padding:12px 15px;border-radius:8px;margin-bottom:20px;">'
            '<h3 style="margin:0 0 4px 0;font-size:13px;color:#334155;'
            'text-transform:uppercase;letter-spacing:.04em;">Exit-tuning data</h3>'
            f'<div style="font-size:14px;"><strong>{n}</strong> replayable trade(s) '
            f'recorded &mdash; {pct}% of the ~{target} that supports a defensible '
            f'single config.<br>'
            f'<span style="color:{colour};font-weight:600;">{stage}</span><br>'
            f'<span style="color:#64748b;">{advice}</span></div></div>'
        )

    def _opening_burst_html(self, trades):
        """
        The opening-move experiment, reported entirely on its own.

        Separated from the main table on purpose. These trades are taken under
        different rules (measured from the 09:30 open, decided by 09:32, half
        size, no continuation ranking, no ceiling), so folding them into the
        session totals would make BOTH numbers unreadable - the experiment would
        be diluted by the normal session and the normal session's win rate would
        move for reasons that have nothing to do with its own settings.

        Returns "" when no opening trades were taken, so a normal day's report
        is unchanged.
        """
        ob = [t for t in (trades or []) if (t.get("entry_method") or "") == "OPENING_MOVE"]
        summary = (getattr(self, "run_context", None) or {}).get("opening_burst_summary")
        if not ob:
            # No trades is a RESULT, not an absence. Rendering nothing made a
            # starved experiment look identical to a disabled one on 2026-08-27.
            if not summary:
                return ""
            return self._opening_burst_empty_html(summary)

        total = sum(t.get("pl", 0) or 0 for t in ob)
        wins = [t for t in ob if (t.get("pl", 0) or 0) > 0]
        losses = [t for t in ob if (t.get("pl", 0) or 0) < 0]
        gross_win = sum(t.get("pl", 0) or 0 for t in wins)
        gross_loss = sum(t.get("pl", 0) or 0 for t in losses)
        wr = (len(wins) / len(ob) * 100) if ob else 0
        pl_color = "#10b981" if total >= 0 else "#ef4444"

        # FILL RATE. len(ob) only ever contains entries that actually filled -
        # a marketable-limit entry that never crosses never produces a
        # trade_history.csv row at all, so a bad session here looked
        # identical to a quiet one until this was added. 2026-09-08: the
        # burst took 9 entries and only 1 (ORCL) filled - the other 8 sat
        # unfilled and were dropped as phantoms with no visible trace in this
        # section at all before this line existed. `summary["taken"]` is the
        # attempted count, set at burst-close time regardless of fill status.
        fill_rate_html = ""
        attempted = (summary or {}).get("taken")
        if isinstance(attempted, int) and attempted > len(ob):
            unfilled = attempted - len(ob)
            fill_rate_html = (
                '<div style="font-size:12px;color:#92400e;margin-top:6px;'
                'font-weight:600;">'
                f'{attempted} entered, {len(ob)} filled, {unfilled} unfilled - '
                f'{len(ob) / attempted * 100:.0f}% fill rate</div>'
            )

        def pct(t):
            v = t.get("pl_pct")
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else "N/A"

        def num(t, key, suffix="%"):
            v = t.get(key)
            return f"{v:+.2f}{suffix}" if isinstance(v, (int, float)) else "N/A"

        rows = []
        for t in sorted(ob, key=lambda x: x.get("entry_time") or ""):
            pl = t.get("pl", 0) or 0
            c = "#10b981" if pl >= 0 else "#ef4444"
            rows.append(
                f"<tr><td><strong>{t.get('symbol','?')}</strong></td>"
                f"<td>{(t.get('entry_time') or '')[11:19]}</td>"
                f"<td>{(t.get('exit_time') or '')[11:19]}</td>"
                f"<td>${t.get('entry_price', 0):.2f}</td>"
                f"<td>${t.get('exit_price', 0):.2f}</td>"
                f"<td>{t.get('qty', 0)}</td>"
                f"<td>{num(t, 'signal_pct')}</td>"
                f"<td style='color:{c};font-weight:600;'>${pl:,.2f}</td>"
                f"<td style='color:{c};font-weight:600;'>{pct(t)}</td>"
                f"<td>{num(t, 'mfe_pct')}</td><td>{num(t, 'mae_pct')}</td>"
                f"<td class='exit-reason'>{t.get('exit_reason','?')}</td>"
                f"<td class='exit-reason'>{t.get('post_exit_note') or 'N/A'}</td></tr>"
            )

        return (
            '<h2 style="margin-top:30px;border-bottom:2px solid #6366f1;'
            'padding-bottom:10px;">Opening-Move Experiment (09:30-09:32)</h2>'
            '<div style="background:#eef2ff;border-left:4px solid #6366f1;'
            'padding:12px 15px;border-radius:8px;margin-bottom:14px;">'
            f'<div style="font-size:22px;font-weight:700;color:{pl_color};">'
            f'${total:,.2f}</div>'
            f'<div style="font-size:13px;color:#3730a3;margin-top:4px;">'
            f'{len(ob)} trade(s) &middot; {len(wins)}W / {len(losses)}L &middot; '
            f'{wr:.0f}% win rate &middot; gross +${gross_win:,.2f} / ${gross_loss:,.2f}</div>'
            '<div style="font-size:11px;color:#4338ca;margin-top:6px;">'
            'Measured from the 09:30 open, decided by 09:32, half size, streamed '
            'symbols only. Reported separately because these run under different '
            'rules than the rest of the session - mixing them would make both '
            'numbers unreadable.</div>'
            + fill_rate_html
            + self._opening_exit_profile_html() +
            '</div>'
            + self._after_exit_ratio_html(ob) +
            '<table class="trades-table"><thead><tr>'
            '<th>Symbol</th><th>Entry</th><th>Exit</th><th>Entry $</th><th>Exit $</th>'
            '<th>Qty</th><th>Move at entry</th><th>P&L</th><th>P&L %</th>'
            '<th>Peak (MFE)</th><th>Trough (MAE)</th><th>Exit Reason</th>'
            '<th>After Exit</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
        )

    def _opening_burst_empty_html(self, s):
        """
        The experiment ran and took nothing - say which kind of nothing.

        "Threshold too high" and "could not measure" need opposite responses, and
        a blank section says neither.
        """
        measured, thresh = s.get("measured") or 0, s.get("threshold")
        best = s.get("best_move")
        attempted = s.get("taken") or 0
        if measured:
            if best is not None and best < (thresh or 0):
                outcome = "This is a THRESHOLD result - the mechanism worked."
            elif attempted:
                # Distinguished from "no entry completed" - see the 2026-09-08
                # incident (9 attempted, 1 filled) that motivated tracking
                # this separately from qualification at all.
                outcome = (
                    f"{attempted} entry attempt(s) were made but NONE filled - "
                    f"a fill-rate problem (see Executor.retry_unfilled_entries), "
                    f"not a threshold or measurement one."
                )
            else:
                outcome = "Qualifying symbols existed but no entry was attempted."
            verdict = (
                f"Ran and measured {measured} symbol(s); {s.get('qualified', 0)} cleared "
                f"the {thresh}% threshold. Best move {best:+.3f}%. " + outcome
            )
            colour = "#92400e"
        else:
            verdict = (
                "Measured NOTHING - no symbol produced a baseline price. This is not a "
                "threshold result: the stream was not serving live prices at the "
                "baseline instant, so every symbol was skipped by streamed_only."
            )
            colour = "#b91c1c"
        return (
            '<h2 style="margin-top:30px;border-bottom:2px solid #6366f1;'
            'padding-bottom:10px;">Opening-Move Experiment '
            f'({s.get("window", "09:30-09:32")})</h2>'
            '<div style="background:#fffbeb;border-left:4px solid #f59e0b;'
            'padding:12px 15px;border-radius:8px;margin-bottom:20px;">'
            f'<div style="font-size:20px;font-weight:700;color:{colour};">No trades</div>'
            f'<div style="font-size:13px;color:#78350f;margin-top:5px;">{verdict}</div>'
            + self._opening_exit_profile_html() +
            '</div>'
        )

    def _regime_timeline_html(self, timeline_file="logs/regime_timeline.json"):
        """
        A colored 09:30-16:00 strip showing which regime (bullish/bearish/
        neutral/choppy) was in force through the session - explicit user
        request, 2026-09-29, to see at a glance when the tape favored longs
        vs shorts without reading the log.

        Reads timeline_file, written by run_trading_day every time the
        CONFIRMED regime label actually changes (not every poll - see
        main.py's regime_timeline list and _regime_multiplier's hysteresis).
        A table of proportionally-widthed <td> cells, not CSS gradients or
        flex - the one horizontal-bar technique that renders consistently
        across email clients including Outlook.

        TICKS (added 2026-09-30, explicit user request - "add ticks
        throughout the bar so i know the exact time the regime was and when
        it was changing"): the original bar only exposed transition times via
        a hover `title` attribute, which does nothing on a phone. Two
        additions, both still plain <td>/border so they survive Outlook:
          - a 1px border-left on every cell after the first, marking each
            transition as a visible line ON the bar itself, not just a color
            change (color alone is hard to place a time against);
          - a chronological "HH:MM LABEL -> HH:MM LABEL -> ..." text line
            underneath, one entry per transition, so every regime-change
            timestamp is readable as text instead of needing to eyeball
            where along the bar a color started. Proportional label
            PLACEMENT under the bar (each time positioned at its own x
            offset) was considered and rejected: narrow segments (a regime
            that held for a few minutes) would need overlapping text, which
            position:absolute could solve but Outlook's engine mangles -
            see the class docstring's reason for avoiding it on the bar
            itself. A sequential list reads cleanly regardless of segment
            width.

        Returns "" when the file is missing, unreadable, empty, or stamped
        for a different day (stale data left over from a prior session) -
        same never-guess-from-old-data convention every optional section
        here uses.
        """
        try:
            with open(timeline_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return ""
        events = data.get("events") or []
        if not events or data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return ""

        colors = {"bullish": "#10b981", "bearish": "#ef4444",
                  "neutral": "#9ca3af", "choppy": "#f59e0b"}

        day_start = datetime.strptime("09:30:00", "%H:%M:%S")
        day_end = datetime.strptime("16:00:00", "%H:%M:%S")
        total_minutes = (day_end - day_start).total_seconds() / 60

        cells = []
        # One entry per transition, in order, for the text line under the
        # bar: (HH:MM the segment started, its label). Kept separate from
        # `cells` (which also carries the synthetic "no read yet" lead-in
        # and gets clipped to day_start/day_end) since the tick line should
        # show only REAL regime reads, not the gray gap.
        ticks = []

        # Anything before the FIRST recorded label (typically the first few
        # minutes before check_time) has no opinion yet - a neutral gray gap
        # rather than a guess.
        first_t = datetime.strptime(events[0]["time"], "%H:%M:%S")
        lead_pct = max(0.0, (first_t - day_start).total_seconds() / 60 / total_minutes * 100)
        if lead_pct > 0.05:
            cells.append((lead_pct, "#e5e7eb", f"9:30 - {first_t:%H:%M}: no read yet"))

        for i, ev in enumerate(events):
            t0 = max(datetime.strptime(ev["time"], "%H:%M:%S"), day_start)
            t1 = (datetime.strptime(events[i + 1]["time"], "%H:%M:%S")
                  if i + 1 < len(events) else day_end)
            t1 = min(t1, day_end)
            if t1 <= t0:
                continue
            width_pct = (t1 - t0).total_seconds() / 60 / total_minutes * 100
            label = ev.get("label", "")
            color = colors.get(label, "#e5e7eb")
            cells.append((width_pct, color, f"{t0:%H:%M} ET: {label.upper()}"))
            ticks.append((t0.strftime("%H:%M"), label))

        # A thin light line on the LEADING edge of every cell after the
        # first marks each transition as a visible tick on the bar itself -
        # border-left on a <td>, not position:absolute, so it survives
        # Outlook the same way the colored cells already do.
        bar_cells = "".join(
            f'<td style="width:{w:.2f}%;background:{c};'
            f'{"border-left:1px solid rgba(255,255,255,0.85);" if idx > 0 else ""}" '
            f'title="{t}"></td>'
            for idx, (w, c, t) in enumerate(cells)
        )
        legend = "".join(
            f'<span style="display:inline-block;margin-right:14px;font-size:11px;'
            f'color:#374151;"><span style="display:inline-block;width:10px;height:10px;'
            f'background:{c};border-radius:2px;margin-right:4px;"></span>{l.upper()}</span>'
            for l, c in colors.items()
        )

        # Every transition, in order, as plain text - the exact-time detail
        # a hover title can't give on a phone. "9:34 CHOPPY -> 9:41 BEARISH
        # -> ..." rather than positioning each label under its own tick,
        # which breaks down on segments too narrow to hold their own text.
        ticks_html = " &rarr; ".join(
            f'<span style="color:{colors.get(lab, "#6b7280")};">{tm} {lab.upper()}</span>'
            for tm, lab in ticks
        )

        return (
            '<div style="margin-bottom:20px;">'
            '<h3 style="margin:0 0 8px 0;font-size:13px;color:#6b7280;'
            'text-transform:uppercase;letter-spacing:.04em;">Regime Timeline '
            '(9:30 - 16:00)</h3>'
            '<table style="width:100%;border-collapse:collapse;height:22px;" '
            f'cellpadding="0" cellspacing="0"><tr>{bar_cells}</tr></table>'
            '<div style="display:flex;justify-content:space-between;font-size:10px;'
            'color:#9ca3af;margin-top:2px;"><span>9:30</span><span>16:00</span></div>'
            f'<div style="margin-top:8px;">{legend}</div>'
            '<div style="margin-top:6px;font-size:10px;color:#6b7280;line-height:1.6;">'
            f'{ticks_html}</div>'
            '</div>'
        )

    def _extended_hours_html(self, trades):
        """
        Extended-hours measurement (trading.extended_hours_experiment),
        added 2026-09-27 on explicit user request: the SAME entry logic as
        the primary 09:33-10:15 window, just left running through the
        16:00 close instead of going idle. Filtered on entry_window ==
        "extended" - a trade_history.csv column added the same day,
        defaulting to "primary" for anything that predates it, so this
        section is simply empty (returns "") on any older trade and on a
        day where extended_hours_experiment is off.

        Shows BOTH longs and shorts together (as of 2026-09-29 - previously
        this section was long-only and redirected any extended-hours short
        to _short_strategy_html instead). The same short trades still ALSO
        appear in _short_strategy_html's own extended-hours category - that
        overlap is deliberate, two different lenses on the same trades (by
        window here, by side there), not a double-count of any total: this
        section's own total below is display-only and, like every extended-
        hours trade regardless of side, is NEVER folded into the headline
        Total P&L above. Explicit user instruction, 2026-09-29: "nothing at
        all is going to be included in the total P&L from the extended time
        period."
        """
        eh = [t for t in (trades or []) if (t.get("entry_window") or "") == "extended"]
        if not eh:
            return ""

        total = sum(t.get("pl", 0) or 0 for t in eh)
        wins = [t for t in eh if (t.get("pl", 0) or 0) > 0]
        losses = [t for t in eh if (t.get("pl", 0) or 0) < 0]
        wr = (len(wins) / len(eh) * 100) if eh else 0
        pl_color = "#10b981" if total >= 0 else "#ef4444"

        def pct(t):
            v = t.get("pl_pct")
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else "N/A"

        rows = []
        for t in sorted(eh, key=lambda x: x.get("entry_time") or ""):
            pl = t.get("pl", 0) or 0
            c = "#10b981" if pl >= 0 else "#ef4444"
            side_label = "SHORT" if t.get("side") == "short" else "LONG"
            rows.append(
                f"<tr><td><strong>{t.get('symbol','?')}</strong></td>"
                f"<td>{side_label}</td>"
                f"<td>{(t.get('entry_time') or '')[11:19]}</td>"
                f"<td>{(t.get('exit_time') or '')[11:19]}</td>"
                f"<td>${t.get('entry_price', 0):.2f}</td>"
                f"<td>${t.get('exit_price', 0):.2f}</td>"
                f"<td>{t.get('qty', 0)}</td>"
                f"<td style='color:{c};font-weight:600;'>${pl:,.2f}</td>"
                f"<td style='color:{c};font-weight:600;'>{pct(t)}</td>"
                f"<td class='exit-reason'>{t.get('exit_reason','?')}</td></tr>"
            )

        return (
            '<h2 style="margin-top:30px;border-bottom:2px solid #0891b2;'
            'padding-bottom:10px;">Extended Hours (10:15-16:00)</h2>'
            '<div style="background:#ecfeff;border-left:4px solid #0891b2;'
            'padding:12px 15px;border-radius:8px;margin-bottom:14px;">'
            f'<div style="font-size:22px;font-weight:700;color:{pl_color};">'
            f'${total:,.2f}</div>'
            f'<div style="font-size:13px;color:#0e7490;margin-top:4px;">'
            f'{len(eh)} trade(s) &middot; {len(wins)}W / {len(losses)}L &middot; '
            f'{wr:.0f}% win rate &middot; longs and shorts both</div>'
            '<div style="font-size:11px;color:#155e75;margin-top:6px;">'
            'Same signal, sizing and exit rules as the primary window, '
            'whichever side the regime favored, just left running the rest '
            'of the day. Reported separately and NEVER summed into the '
            'Total P&L above, per standing instruction.</div>'
            '</div>'
            '<table class="trades-table"><thead><tr>'
            '<th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Entry $</th><th>Exit $</th>'
            '<th>Qty</th><th>P&L</th><th>P&L %</th><th>Exit Reason</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
        )

    def _short_strategy_html(self, trades):
        """
        The config-gated short strategy (trading.short_strategy), enabled
        2026-09-27. Filtered on side == "short" - ANY window (primary,
        extended, or opening_burst, though the opening burst does not
        currently open shorts at all) - so this is the single home for
        every short trade regardless of when it happened, a side-specific
        breakdown alongside _extended_hours_html's window-specific one
        (the two now deliberately overlap on extended-hours shorts - see
        that method's docstring for why that overlap is fine).

        STATUS CHANGED 2026-09-29. Originally shorts were entirely excluded
        from the primary P&L ("lets not mix it in the primary P&L", 2026-
        09-27) - the user revisited that once real short P&L existed and
        asked for PRIMARY-WINDOW shorts (category 1 below) to be folded
        into the headline Total P&L above, same as longs, with this section
        kept as a dedicated detail view rather than the only place the
        money shows up. EXTENDED-HOURS shorts (category 2) are NOT part of
        that reversal - they stay excluded from the headline exactly as
        before, same as every other extended-hours trade regardless of
        side: "nothing at all is going to be included in the total P&L
        from the extended time period."

        Returns "" when no short trades exist (the mechanism took nothing,
        or the account still has shorting blocked by Alpaca's own
        no_shorting setting - see main()'s startup check) - same
        empty-day convention every other optional section uses.
        """
        sh = [t for t in (trades or []) if t.get("side") == "short"]
        if not sh:
            return ""

        def pct(t):
            v = t.get("pl_pct")
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else "N/A"

        def stat_block(label_html, group, empty_note):
            """One of the two time-window sub-summaries - explicit user
            request, 2026-09-28: "I want two categories for the shorting.
            First category will be from 9:30 to 10:15... second category
            will be from 10:15 to the end of the day." Each gets its own
            P&L, trade count, and win rate - a reader should never have to
            do that arithmetic by hand from the combined table."""
            if not group:
                return (
                    '<div style="background:var(--surface-alt,#f5f3ff);border:1px solid '
                    '#ddd6fe;border-radius:8px;padding:10px 14px;flex:1;min-width:220px;">'
                    f'<div style="font-size:12px;font-weight:600;color:#5b21b6;'
                    f'text-transform:uppercase;letter-spacing:.03em;">{label_html}</div>'
                    f'<div style="font-size:13px;color:#6b7280;margin-top:6px;">{empty_note}</div>'
                    '</div>'
                )
            gtotal = sum(t.get("pl", 0) or 0 for t in group)
            gwins = [t for t in group if (t.get("pl", 0) or 0) > 0]
            glosses = [t for t in group if (t.get("pl", 0) or 0) < 0]
            gwr = (len(gwins) / len(group) * 100) if group else 0
            gcolor = "#10b981" if gtotal >= 0 else "#ef4444"
            return (
                '<div style="background:#fff;border:1px solid #ddd6fe;border-radius:8px;'
                'padding:10px 14px;flex:1;min-width:220px;">'
                f'<div style="font-size:12px;font-weight:600;color:#5b21b6;'
                f'text-transform:uppercase;letter-spacing:.03em;">{label_html}</div>'
                f'<div style="font-size:20px;font-weight:700;color:{gcolor};margin-top:4px;">'
                f'${gtotal:,.2f}</div>'
                f'<div style="font-size:12.5px;color:#6b7280;margin-top:2px;">'
                f'{len(group)} trade(s) &middot; {len(gwins)}W / {len(glosses)}L &middot; '
                f'{gwr:.0f}% win rate</div>'
                '</div>'
            )

        # Category 1: 9:30-10:15, the same clock window the primary long
        # session runs in (entry_window == "primary" - opening_burst is a
        # separate mechanism that does not currently open shorts at all,
        # but is included here too on the chance it ever does, since it
        # also falls inside 9:30-10:15).
        cat_primary_window = [t for t in sh if t.get("entry_window") in ("primary", "opening_burst")]
        # Category 2: 10:15 to the close.
        cat_extended_window = [t for t in sh if t.get("entry_window") == "extended"]

        overall_total = sum(t.get("pl", 0) or 0 for t in sh)
        overall_color = "#10b981" if overall_total >= 0 else "#ef4444"

        window_labels = {"primary": "9:30-10:15", "extended": "10:15-close",
                         "opening_burst": "9:30-10:15"}

        rows = []
        for t in sorted(sh, key=lambda x: x.get("entry_time") or ""):
            pl = t.get("pl", 0) or 0
            c = "#10b981" if pl >= 0 else "#ef4444"
            win_label = window_labels.get(t.get("entry_window"), t.get("entry_window") or "?")
            rows.append(
                f"<tr><td><strong>{t.get('symbol','?')}</strong></td>"
                f"<td>{(t.get('entry_time') or '')[11:19]}</td>"
                f"<td>{(t.get('exit_time') or '')[11:19]}</td>"
                f"<td>${t.get('entry_price', 0):.2f}</td>"
                f"<td>${t.get('exit_price', 0):.2f}</td>"
                f"<td>{t.get('qty', 0)}</td>"
                f"<td style='color:{c};font-weight:600;'>${pl:,.2f}</td>"
                f"<td style='color:{c};font-weight:600;'>{pct(t)}</td>"
                f"<td>{win_label}</td>"
                f"<td class='exit-reason'>{t.get('exit_reason','?')}</td></tr>"
            )

        return (
            '<h2 style="margin-top:30px;border-bottom:2px solid #7c3aed;'
            'padding-bottom:10px;">Short Strategy (Bearish Regime)</h2>'
            '<div style="font-size:11px;color:#4c1d95;margin-bottom:10px;">'
            'A mirror of the long side\'s entry/exit logic, only taken when '
            'the regime reads BEARISH (never alongside a long entry in the '
            'same regime window - see regime_sizing/short_strategy\'s hard '
            'mutual exclusion). The 9:30-10:15 category below is already '
            'included in the Total P&amp;L above - shown here as a side-'
            'specific breakdown. The extended-hours category is NOT '
            'included in the Total P&amp;L, shown here for reference only.'
            '</div>'
            f'<div style="font-size:15px;font-weight:600;color:{overall_color};'
            f'margin-bottom:10px;">All shorts combined: ${overall_total:,.2f} '
            f'({len(sh)} trade(s))</div>'
            '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;">'
            + stat_block("9:30 - 10:15 (in Total P&amp;L above)", cat_primary_window,
                        "No shorts taken in this window today.")
            + stat_block("10:15 - close (NOT in Total P&amp;L)", cat_extended_window,
                        "No shorts taken in this window today.")
            + '</div>'
            '<table class="trades-table"><thead><tr>'
            '<th>Symbol</th><th>Entry</th><th>Exit</th><th>Entry $</th><th>Exit $</th>'
            '<th>Qty</th><th>P&L</th><th>P&L %</th><th>Window</th><th>Exit Reason</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
        )

    def _opening_exit_profile_html(self):
        """
        The exit rules these trades ran under, printed next to their results.

        Without it the section is unanalysable: a -0.3% first exit and a -0.5%
        one produce very different exit-reason mixes, and six weeks from now
        nobody will remember which was in force. Showing it against the normal
        session is the point - it turns "FIRST_EXIT fired 5 times" into
        "FIRST_EXIT fired 5 times at a stop 40% tighter than the session's".
        """
        # getattr, not self.run_context: this is reached from
        # _generate_html_summary, and a missing attribute here would take the
        # ENTIRE report down over a decorative sub-table. The report surviving
        # matters more than this section appearing.
        prof = (getattr(self, "run_context", None) or {}).get("opening_exits")
        if not prof:
            return ""
        rows = "".join(
            f"<tr><td style='padding:2px 10px 2px 0;color:#4338ca;'>{k}</td>"
            f"<td style='padding:2px 12px 2px 0;color:#6b7280;'>{n}</td>"
            f"<td style='padding:2px 0;color:#3730a3;font-weight:600;'>{o}</td></tr>"
            for k, n, o in prof
        )
        return (
            '<div style="margin-top:10px;font-size:11px;">'
            '<div style="color:#4338ca;font-weight:600;margin-bottom:3px;">'
            'Exit profile for these trades</div>'
            '<table style="border-collapse:collapse;font-size:11px;">'
            '<tr><td></td><td style="color:#9ca3af;padding-right:12px;">normal</td>'
            '<td style="color:#9ca3af;">opening</td></tr>'
            + rows + '</table></div>'
        )

    def _after_exit_ratio_html(self, trades):
        """
        Ratio summary of the After Exit column, at the top of the report.

        The After Exit column (post_exit_pct/post_exit_note) already scores
        every exit after the fact - whether a stop/breakeven fired too early
        (price bounced back) or was justified (price kept falling), and
        whether a take-profit tier left money on the table (price ran
        further) or was exited at the right time (price gave it back
        afterward). That verdict existed per-trade but nothing ever
        aggregated it, so "are the stops too tight" had to be answered by
        reading rows one at a time. This is the ratio, computed once per
        report so it is visible before scrolling to a single trade.

        Two separate ratios because they measure different things: stop-type
        exits (BREAKEVEN_STOP, FIRST_EXIT, TRAILING_STOP, RESISTANCE,
        FINAL_EXIT) answer "is the stop too tight", take-profit exits answer
        "is the top tier too low". Mixing them into one number would answer
        neither question.
        """
        STOP_EARLY = "bounced - exit was early"
        STOP_RIGHT = "kept falling - exit was right"
        TP_LEFT_ON_TABLE = "ran further"
        TP_RIGHT = "gave it back"
        FLAT = "flat"

        stop_notes, tp_notes = [], []
        for t in (trades or []):
            note = t.get("post_exit_note")
            if not note:
                continue
            reason = t.get("exit_reason") or ""
            if "TAKE_PROFIT" in reason:
                tp_notes.append(note)
            else:
                stop_notes.append(note)

        if not stop_notes and not tp_notes:
            return ""

        def _block(title, notes, left_label, right_label, left_key, right_key, neutral_key):
            left = notes.count(left_key)
            right = notes.count(right_key)
            neutral = notes.count(neutral_key)
            total = len(notes)
            if total == 0:
                return ""
            ratio = f"{left}:{right}" if right else (f"{left}:0" if left else "0:0")
            lp = left / total * 100
            rp = right / total * 100
            return (
                f'<div style="flex:1;min-width:200px;">'
                f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;'
                f'letter-spacing:.04em;margin-bottom:2px;">{title}</div>'
                f'<div style="font-size:18px;font-weight:700;color:#1a1f2e;">{ratio}</div>'
                f'<div style="font-size:12px;color:#6b7280;">'
                f'{left_label}: {left} ({lp:.0f}%) &middot; {right_label}: {right} ({rp:.0f}%)'
                + (f' &middot; flat: {neutral}' if neutral else '') +
                '</div></div>'
            )

        stop_html = _block(
            "Stop/Breakeven exits - early vs. right", stop_notes,
            "too early", "right", STOP_EARLY, STOP_RIGHT, FLAT,
        )
        tp_html = _block(
            "Take-profit exits - left on table vs. right", tp_notes,
            "ran further", "gave it back", TP_LEFT_ON_TABLE, TP_RIGHT, FLAT,
        )
        if not stop_html and not tp_html:
            return ""
        return (
            '<div style="background:#f5f7fa;border-left:4px solid #8b5cf6;'
            'padding:12px 15px;border-radius:8px;margin-bottom:20px;">'
            '<h3 style="margin:0 0 8px 0;font-size:13px;color:#6b7280;'
            'text-transform:uppercase;letter-spacing:.04em;">After-Exit Ratios</h3>'
            '<div style="display:flex;gap:24px;flex-wrap:wrap;">'
            + stop_html + tp_html +
            '</div></div>'
        )

    def _generate_html_summary(self, trades, burst_summary="", label="Daily Summary",
                               open_positions=None):
        """Generate the HTML report: closed trades, and any still-open positions."""
        open_positions = open_positions or []
        # The headline Total P&L/win-rate/trade-count is everything from the
        # PRIMARY trading window (09:30-10:15, or opening_burst) - LONGS AND
        # SHORTS BOTH, as of 2026-09-29. This is a reversal of the original
        # 2026-09-27 instruction that excluded shorts entirely: the user
        # revisited it once real short P&L existed and asked for shorts to
        # be folded into the actual total, same as longs, with a dedicated
        # breakdown section (_short_strategy_html) alongside for detail -
        # not instead of being in the headline.
        #
        # entry_window == "extended" is STILL excluded, unconditionally,
        # regardless of side. Explicit user instruction, 2026-09-29: "the
        # total P&L should only be for 9:30 to 10:15... nothing at all is
        # going to be included in the total P&L from the extended time
        # period." That part of the original 2026-09-27 instruction stands.
        primary_trades = [t for t in trades if t.get("entry_window") != "extended"]
        total_pl = sum(t.get("pl", 0) for t in primary_trades)
        winning_trades = [t for t in primary_trades if t.get("pl", 0) > 0]
        losing_trades = [t for t in primary_trades if t.get("pl", 0) < 0]
        win_rate = (len(winning_trades) / len(primary_trades) * 100) if primary_trades else 0

        # Long/short split of the SAME headline total, at a glance - the
        # full side-specific detail lives in _short_strategy_html below,
        # this is just enough to see the mix without scrolling.
        primary_longs = [t for t in primary_trades if t.get("side") != "short"]
        primary_shorts = [t for t in primary_trades if t.get("side") == "short"]
        long_short_split_html = ""
        if primary_shorts:
            pl_longs = sum(t.get("pl", 0) for t in primary_longs)
            pl_shorts = sum(t.get("pl", 0) for t in primary_shorts)
            lc = "#10b981" if pl_longs >= 0 else "#ef4444"
            sc = "#10b981" if pl_shorts >= 0 else "#ef4444"
            long_short_split_html = (
                '<div style="font-size:12.5px;color:#6b7280;margin:-8px 0 18px 2px;">'
                f'Of the total above: <strong>longs</strong> '
                f'<span style="color:{lc};font-weight:600;">${pl_longs:,.2f}</span> '
                f'({len(primary_longs)}) &middot; <strong>shorts</strong> '
                f'<span style="color:{sc};font-weight:600;">${pl_shorts:,.2f}</span> '
                f'({len(primary_shorts)}) - full short breakdown below.</div>'
            )

        # Color coding
        pl_color = "#10b981" if total_pl >= 0 else "#ef4444"

        # Only on the end-of-day sends, not "Midday Status" - today's row in
        # daily_summary.csv is written by finish_day AFTER the report is
        # built, so a midday send would only ever show yesterday and older,
        # which is confusing on a report whose whole point is "today."
        performance_timeline_html = (
            render_performance_timeline_html() if label != "Midday Status" else ""
        )
        regime_timeline_html = self._regime_timeline_html()
        after_exit_ratio_html = self._after_exit_ratio_html(trades)
        run_context_html = self._run_context_html()
        replay_progress_html = self._replay_progress_html()
        opening_burst_html = self._opening_burst_html(trades)
        extended_hours_html = self._extended_hours_html(trades)
        short_strategy_html = self._short_strategy_html(trades)
        open_positions_html = self._open_positions_html(open_positions)
        unrealized_pl = sum(float(p.get("unrealized_pl") or 0) for p in open_positions)

        open_summary_html = ""
        if open_positions:
            open_color = "#10b981" if unrealized_pl >= 0 else "#ef4444"
            open_summary_html = (
                '<div style="background:#fffbeb;border-left:4px solid #f59e0b;'
                'padding:12px 15px;border-radius:8px;margin-bottom:20px;">'
                '<h3 style="margin:0 0 4px 0;font-size:13px;color:#92400e;'
                'text-transform:uppercase;letter-spacing:.04em;">Still Open</h3>'
                f'<div style="font-size:14px;">{len(open_positions)} position(s) open, '
                f'<span style="color:{open_color};font-weight:600;">'
                f'${unrealized_pl:,.2f}</span> unrealized. '
                'These are NOT included in the P&L figures above, which count '
                'closed trades only.</div></div>'
            )

        # Build HTML
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #1a1f2e; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
                .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 20px; }}
                .summary-box {{ background: #f5f7fa; padding: 15px; border-radius: 8px; text-align: center; border-left: 4px solid #3b82f6; }}
                .pl-box {{ border-left-color: {pl_color}; }}
                .summary-box h3 {{ margin: 0 0 10px 0; font-size: 14px; color: #6b7280; }}
                .summary-box .value {{ font-size: 24px; font-weight: bold; color: {pl_color}; }}
                .trades-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                .trades-table th {{ background: #1a1f2e; color: white; padding: 12px; text-align: left; font-weight: 600; }}
                .trades-table td {{ padding: 12px; border-bottom: 1px solid #e5e7eb; }}
                .trades-table tr:hover {{ background: #f9fafb; }}
                .symbol {{ font-weight: 600; }}
                .profit {{ color: #10b981; }}
                .loss {{ color: #ef4444; }}
                .exit-reason {{ font-size: 12px; color: #6b7280; }}
                .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #e5e7eb; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Trading Bot {label}</h1>
                    <p style="margin: 5px 0 0 0; opacity: 0.9;">{datetime.now().strftime('%A, %B %d, %Y')}</p>
                </div>

                <div class="summary-grid">
                    <div class="summary-box">
                        <h3>Total P&L</h3>
                        <div class="value pl-box" style="color: {pl_color};">${total_pl:,.2f}</div>
                    </div>
                    <div class="summary-box">
                        <h3>Total Trades</h3>
                        <div class="value">{len(trades)}</div>
                    </div>
                    <div class="summary-box">
                        <h3>Win Rate</h3>
                        <div class="value">{win_rate:.1f}%</div>
                    </div>
                    <div class="summary-box">
                        <h3>Wins / Losses</h3>
                        <div class="value">{len(winning_trades)} / {len(losing_trades)}</div>
                    </div>
                </div>
                {long_short_split_html}
                {regime_timeline_html}
                {performance_timeline_html}
                {after_exit_ratio_html}
                {run_context_html}
                {replay_progress_html}
                {open_summary_html}

                <div style="background:#f5f7fa;border-left:4px solid #6366f1;padding:12px 15px;border-radius:8px;margin-bottom:20px;">
                    <h3 style="margin:0 0 4px 0;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;">Bursting Logic</h3>
                    <div style="font-size:14px;">{burst_summary or 'Not recorded for this session.'}</div>
                </div>

                {opening_burst_html}

                {extended_hours_html}

                {short_strategy_html}

                {open_positions_html}

                <h2 style="margin-top: 30px; border-bottom: 2px solid #3b82f6; padding-bottom: 10px;">Closed Trades</h2>
                <table class="trades-table">
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Side</th>
                            <th>Entry</th>
                            <th>Entry Method</th>
                            <th>Signal %</th>
                            <th>Bursting Logic</th>
                            <th>Price Source</th>
                            <th>After Exit</th>
                            <th>Entry RSI</th>
                            <th>Exit</th>
                            <th>Exit RSI</th>
                            <th>Qty</th>
                            <th>% Change</th>
                            <th>P&L</th>
                            <th>Peak (MFE)</th>
                            <th>Trough (MAE)</th>
                            <th>Exit Reason</th>
                            <th>Re-entry</th>
                            <th>Stop Loss?</th>
                        </tr>
                    </thead>
                    <tbody>
        """

        reentry_labels = self._reentry_labels(trades)

        for trade in sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True):
            symbol = trade.get("symbol", "N/A")
            entry_price = trade.get("entry_price") or 0
            exit_price = trade.get("exit_price") or 0
            qty = trade.get("qty", 0)
            pl = trade.get("pl", 0)
            pl_pct = trade.get("pl_pct", 0)
            exit_reason = trade.get("exit_reason", "Unknown")
            entry_method = trade.get("entry_method") or "N/A"
            sig = trade.get("signal_pct")
            signal_str = f"{sig:+.2f}%" if isinstance(sig, (int, float)) else "n/a"
            burst_logic = trade.get("burst_logic") or "n/a"
            price_source = trade.get("price_source") or "unknown"
            pe_pct, pe_note = trade.get("post_exit_pct"), trade.get("post_exit_note")
            after_exit = (f"{pe_pct:+.2f}% - {pe_note}"
                          if isinstance(pe_pct, (int, float)) else "n/a")
            mfe, mae = trade.get("mfe_pct"), trade.get("mae_pct")
            mfe_str = f"{mfe:+.2f}%" if isinstance(mfe, (int, float)) else "N/A"
            mae_str = f"{mae:+.2f}%" if isinstance(mae, (int, float)) else "N/A"
            entry_rsi = trade.get("entry_rsi")
            exit_rsi = trade.get("exit_rsi")
            entry_rsi_str = f"{entry_rsi:.1f}" if isinstance(entry_rsi, (int, float)) else "N/A"
            exit_rsi_str = f"{exit_rsi:.1f}" if isinstance(exit_rsi, (int, float)) else "N/A"
            stop_loss_str = "Yes" if trade.get("stop_loss_used") else "No"
            side_label = "SHORT" if trade.get("side") == "short" else "LONG"

            pl_class = "profit" if pl >= 0 else "loss"
            pl_sign = "+" if pl >= 0 else ""

            html += f"""
                        <tr>
                            <td class="symbol">{symbol}</td>
                            <td>{side_label}</td>
                            <td>${entry_price:.2f}</td>
                            <td><span class="exit-reason">{entry_method}</span></td>
                            <td>{signal_str}</td>
                            <td><span class="exit-reason">{burst_logic}</span></td>
                            <td><span class="exit-reason">{price_source}</span></td>
                            <td><span class="exit-reason">{after_exit}</span></td>
                            <td>{entry_rsi_str}</td>
                            <td>${exit_price:.2f}</td>
                            <td>{exit_rsi_str}</td>
                            <td>{qty}</td>
                            <td class="{pl_class}">{pl_sign}{pl_pct:.2f}%</td>
                            <td class="{pl_class}"><strong>{pl_sign}${pl:,.2f}</strong></td>
                            <td class="profit">{mfe_str}</td>
                            <td class="loss">{mae_str}</td>
                            <td><span class="exit-reason">{exit_reason}</span></td>
                            <td><span class="exit-reason">{reentry_labels.get(id(trade), "1st")}</span></td>
                            <td>{stop_loss_str}</td>
                        </tr>
            """

        # Closing P&L block. Realized and unrealized are kept strictly apart:
        # a midday report where the two are added together reads as a settled
        # result when half of it is still moving, and unrealized P&L on an open
        # position is an opinion, not money.
        total_color = "#10b981" if (total_pl + unrealized_pl) >= 0 else "#ef4444"
        unreal_color = "#10b981" if unrealized_pl >= 0 else "#ef4444"
        combined_note = (
            "Realized only - every position is closed."
            if not open_positions else
            f"{len(open_positions)} position(s) still open, so the combined figure "
            f"will move until they close."
        )
        html += f"""
                    </tbody>
                </table>

                <h2 style="margin-top:30px;border-bottom:2px solid #1a1f2e;padding-bottom:10px;">
                    Profit &amp; Loss
                </h2>
                <table class="trades-table">
                    <tbody>
                        <tr>
                            <td style="width:60%;"><strong>Realized P&amp;L</strong>
                                <div class="exit-reason">{len(trades)} closed trade(s) - booked, final</div></td>
                            <td style="text-align:right;font-size:20px;font-weight:bold;color:{pl_color};">
                                ${total_pl:,.2f}</td>
                        </tr>
                        <tr>
                            <td><strong>Unrealized P&amp;L</strong>
                                <div class="exit-reason">{len(open_positions)} open position(s) - marked to current price, not booked</div></td>
                            <td style="text-align:right;font-size:20px;font-weight:bold;color:{unreal_color};">
                                ${unrealized_pl:,.2f}</td>
                        </tr>
                        <tr style="background:#f5f7fa;">
                            <td><strong>Combined</strong>
                                <div class="exit-reason">{combined_note}</div></td>
                            <td style="text-align:right;font-size:22px;font-weight:bold;color:{total_color};">
                                ${total_pl + unrealized_pl:,.2f}</td>
                        </tr>
                    </tbody>
                </table>

                <div class="footer">
                    <p>This is an automated email from your Trading Bot.</p>
                    <p>Paper Trading Account | All trades are simulated</p>
                </div>
            </div>
        </body>
        </html>
        """

        return html

    def _send_email(self, subject, html_content):
        """Send email via SMTP"""
        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.sender_email
            msg["To"] = self.recipient_email

            # Attach HTML
            html_part = MIMEText(html_content, "html")
            msg.attach(html_part)

            # Send via SMTP
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.sendmail(self.sender_email, self.recipient_email, msg.as_string())

            logger.info(f"✓ Email sent successfully to {self.recipient_email}")

        except smtplib.SMTPAuthenticationError:
            logger.error("SMTP Authentication failed. Check email credentials.")
            raise
        except Exception as e:
            logger.error(f"SMTP error: {e}")
            raise
