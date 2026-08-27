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
            closed = [t for t in trades if t.get("exit_price") is not None]
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

    def _generate_html_summary(self, trades, burst_summary="", label="Daily Summary",
                               open_positions=None):
        """Generate the HTML report: closed trades, and any still-open positions."""
        open_positions = open_positions or []
        total_pl = sum(t.get("pl", 0) for t in trades)
        winning_trades = [t for t in trades if t.get("pl", 0) > 0]
        losing_trades = [t for t in trades if t.get("pl", 0) < 0]
        win_rate = (len(winning_trades) / len(trades) * 100) if trades else 0

        # Color coding
        pl_color = "#10b981" if total_pl >= 0 else "#ef4444"

        run_context_html = self._run_context_html()
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
                {run_context_html}
                {open_summary_html}

                <div style="background:#f5f7fa;border-left:4px solid #6366f1;padding:12px 15px;border-radius:8px;margin-bottom:20px;">
                    <h3 style="margin:0 0 4px 0;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;">Bursting Logic</h3>
                    <div style="font-size:14px;">{burst_summary or 'Not recorded for this session.'}</div>
                </div>

                {open_positions_html}

                <h2 style="margin-top: 30px; border-bottom: 2px solid #3b82f6; padding-bottom: 10px;">Closed Trades</h2>
                <table class="trades-table">
                    <thead>
                        <tr>
                            <th>Symbol</th>
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

            pl_class = "profit" if pl >= 0 else "loss"
            pl_sign = "+" if pl >= 0 else ""

            html += f"""
                        <tr>
                            <td class="symbol">{symbol}</td>
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
