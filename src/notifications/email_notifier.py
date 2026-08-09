import smtplib
import json
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os

logger = logging.getLogger(__name__)


class EmailNotifier:
    """Send daily trading summary emails"""

    def __init__(self, config):
        self.config = config
        self.email_config = config.get("notifications", {}).get("email", {})
        self.enabled = self.email_config.get("enabled", False)

        if not self.enabled:
            logger.info("Email notifications disabled")
            return

        self.sender_email = self.email_config.get("sender_email")
        self.sender_password = self.email_config.get("sender_password")
        self.recipient_email = self.email_config.get("recipient_email")
        self.smtp_server = self.email_config.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = self.email_config.get("smtp_port", 587)

        if not all([self.sender_email, self.sender_password, self.recipient_email]):
            logger.warning("Email notifications enabled but credentials missing")
            self.enabled = False

    def send_daily_summary(self, trades_file="logs/trades.json"):
        """Send daily trading summary email"""
        if not self.enabled:
            return False

        try:
            # Load trades
            if not os.path.exists(trades_file):
                logger.warning(f"No trades file found: {trades_file}")
                return False

            with open(trades_file) as f:
                trades_data = json.load(f)

            if not trades_data:
                logger.info("No trades to report")
                return False

            # Parse trades
            trades = trades_data if isinstance(trades_data, list) else trades_data.get("trades", [])

            # Generate HTML email
            html_content = self._generate_html_summary(trades)

            # Send email
            subject = f"Trading Bot Daily Summary - {datetime.now().strftime('%Y-%m-%d')}"
            self._send_email(subject, html_content)

            logger.info(f"✓ Daily summary emailed to {self.recipient_email}")
            return True

        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return False

    def _generate_html_summary(self, trades):
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

                <h2 style="margin-top: 30px; border-bottom: 2px solid #3b82f6; padding-bottom: 10px;">Trade Details</h2>
                <table class="trades-table">
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Entry</th>
                            <th>Exit</th>
                            <th>Qty</th>
                            <th>P&L</th>
                            <th>Exit Reason</th>
                        </tr>
                    </thead>
                    <tbody>
        """

        for trade in sorted(trades, key=lambda x: x.get("exit_time", ""), reverse=True):
            symbol = trade.get("symbol", "N/A")
            entry_price = trade.get("entry_price", 0)
            exit_price = trade.get("exit_price", 0)
            qty = trade.get("qty", 0)
            pl = trade.get("pl", 0)
            exit_reason = trade.get("exit_reason", "Unknown")

            pl_class = "profit" if pl >= 0 else "loss"
            pl_sign = "+" if pl >= 0 else ""

            html += f"""
                        <tr>
                            <td class="symbol">{symbol}</td>
                            <td>${entry_price:.2f}</td>
                            <td>${exit_price:.2f}</td>
                            <td>{qty}</td>
                            <td class="{pl_class}"><strong>{pl_sign}${pl:,.2f}</strong></td>
                            <td><span class="exit-reason">{exit_reason}</span></td>
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
