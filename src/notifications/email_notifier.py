import smtplib
import json
import logging
import glob
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import os

logger = logging.getLogger(__name__)

REPORT_DIR = "logs/reports"
REPORT_RETENTION_DAYS = 7
_REPORT_NAME_RE = re.compile(r"^trading-report-(\d{4}-\d{2}-\d{2})\.html$")


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

        if not self.enabled:
            logger.info("Email notifications disabled (daily report will still be saved to disk)")
            return

        self.sender_email = self.email_config.get("sender_email")
        self.sender_password = self.email_config.get("sender_password")
        self.recipient_email = self.email_config.get("recipient_email")
        self.smtp_server = self.email_config.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = self.email_config.get("smtp_port", 587)

        if not all([self.sender_email, self.sender_password, self.recipient_email]):
            logger.warning("Email notifications enabled but credentials missing")
            self.enabled = False

    def send_daily_summary(self, trades_file="logs/trades.json", burst_summary=""):
        """
        Build the daily report, save it to disk, then try to email it.

        Returns True only if the EMAIL was sent. The saved-to-disk report is
        independent of that return value and of self.enabled - check the log
        line for the path, or just look in self.report_dir.
        """
        try:
            if not os.path.exists(trades_file):
                logger.warning(f"No trades file found: {trades_file}")
                return False

            with open(trades_file) as f:
                trades_data = json.load(f)

            if not trades_data:
                logger.info("No trades to report")
                return False

            trades = trades_data if isinstance(trades_data, list) else trades_data.get("trades", [])

            html_content = self._generate_html_summary(trades, burst_summary=burst_summary)
        except Exception as e:
            logger.error(f"Error building daily report: {e}")
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

        if not self.enabled:
            return False

        try:
            subject = f"Trading Bot Daily Summary - {datetime.now().strftime('%Y-%m-%d')}"
            self._send_email(subject, html_content)
            logger.info(f"✓ Daily summary emailed to {self.recipient_email}")
            return True
        except Exception as e:
            logger.error(f"Error sending email (report is still saved to disk): {e}")
            return False

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

    def _generate_html_summary(self, trades, burst_summary=""):
        """Generate HTML email with trading summary"""
        total_pl = sum(t.get("pl", 0) for t in trades)
        winning_trades = [t for t in trades if t.get("pl", 0) > 0]
        losing_trades = [t for t in trades if t.get("pl", 0) < 0]
        win_rate = (len(winning_trades) / len(trades) * 100) if trades else 0

        # Color coding
        pl_color = "#10b981" if total_pl >= 0 else "#ef4444"

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
                    <h1 style="margin: 0;">Trading Bot Daily Summary</h1>
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

                <div style="background:#f5f7fa;border-left:4px solid #6366f1;padding:12px 15px;border-radius:8px;margin-bottom:20px;">
                    <h3 style="margin:0 0 4px 0;font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;">Bursting Logic</h3>
                    <div style="font-size:14px;">{burst_summary or 'Not recorded for this session.'}</div>
                </div>

                <h2 style="margin-top: 30px; border-bottom: 2px solid #3b82f6; padding-bottom: 10px;">Trade Details</h2>
                <table class="trades-table">
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Entry</th>
                            <th>Entry Method</th>
                            <th>Bursting Logic</th>
                            <th>Entry RSI</th>
                            <th>Exit</th>
                            <th>Exit RSI</th>
                            <th>Qty</th>
                            <th>% Change</th>
                            <th>P&L</th>
                            <th>Peak (MFE)</th>
                            <th>Trough (MAE)</th>
                            <th>Exit Reason</th>
                            <th>Stop Loss?</th>
                        </tr>
                    </thead>
                    <tbody>
        """

        for trade in sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True):
            symbol = trade.get("symbol", "N/A")
            entry_price = trade.get("entry_price") or 0
            exit_price = trade.get("exit_price") or 0
            qty = trade.get("qty", 0)
            pl = trade.get("pl", 0)
            pl_pct = trade.get("pl_pct", 0)
            exit_reason = trade.get("exit_reason", "Unknown")
            entry_method = trade.get("entry_method") or "N/A"
            burst_logic = trade.get("burst_logic") or "n/a"
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
                            <td><span class="exit-reason">{burst_logic}</span></td>
                            <td>{entry_rsi_str}</td>
                            <td>${exit_price:.2f}</td>
                            <td>{exit_rsi_str}</td>
                            <td>{qty}</td>
                            <td class="{pl_class}">{pl_sign}{pl_pct:.2f}%</td>
                            <td class="{pl_class}"><strong>{pl_sign}${pl:,.2f}</strong></td>
                            <td class="profit">{mfe_str}</td>
                            <td class="loss">{mae_str}</td>
                            <td><span class="exit-reason">{exit_reason}</span></td>
                            <td>{stop_loss_str}</td>
                        </tr>
            """

        html += """
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
