# Email Notifications Setup

Get daily trading summaries emailed to you automatically after each trading day.

---

## Step 1: Generate Gmail App Password

Gmail doesn't allow regular passwords for third-party apps. You need an "App Password".

### Instructions:

1. **Go to Google Account Settings:**
   - Open https://myaccount.google.com/apppasswords
   - You may need to log in

2. **Select App & Device:**
   - App: Select **Mail**
   - Device: Select **Windows Computer** (or your device type)

3. **Generate Password:**
   - Click **Generate**
   - Google will create a 16-character password
   - **Copy this password** (you'll use it in the next step)

4. **Important:**
   - This password is unique to your device
   - Keep it secret (don't commit it to Git)
   - You can revoke it anytime from Account Settings

---

## Step 2: Add Password to Config

Edit `config.yaml`:

```yaml
notifications:
  email:
    enabled: true
    sender_email: "shahbazfarhan25@gmail.com"
    sender_password: "xxxx xxxx xxxx xxxx"  # ← Paste your 16-char app password here
    recipient_email: "shahbazfarhan25@gmail.com"
```

**Example:**
```yaml
sender_password: "abcd efgh ijkl mnop"  # (spaces included as Google provides them)
```

---

## Step 3: Verify Setup

Test the email configuration before market opens:

```bash
python -c "
from dotenv import load_dotenv
import yaml
load_dotenv()

config = yaml.safe_load(open('config.yaml'))
email_config = config['notifications']['email']

print('Email Notifications Status:')
print(f'  Enabled: {email_config[\"enabled\"]}')
print(f'  From: {email_config[\"sender_email\"]}')
print(f'  To: {email_config[\"recipient_email\"]}')
print(f'  Password set: {bool(email_config[\"sender_password\"] and \"xxxx\" not in email_config[\"sender_password\"])}')

if email_config['enabled'] and 'xxxx' in email_config.get('sender_password', ''):
    print('\n⚠️  WARNING: App password not configured!')
    print('See EMAIL_SETUP.md for instructions')
else:
    print('\n✓ Email setup looks good!')
"
```

---

## What You'll Receive

After each trading day (4:00 PM ET), you'll get an email like:

### Subject:
```
Trading Bot Daily Summary - 2026-08-10
```

### Content:
```
TRADING BOT DAILY SUMMARY
Saturday, August 10, 2026

┌─────────────────────────────────────────┐
│ Total P&L: +$247.50                    │
│ Total Trades: 4                        │
│ Win Rate: 75.0%                        │
│ Wins / Losses: 3 / 1                   │
└─────────────────────────────────────────┘

TRADE DETAILS
─────────────────────────────────────────

Symbol  Entry      Exit       Qty   P&L       Exit Reason
─────────────────────────────────────────
NVDA    $120.50    $121.25    10    +$7.50    TRAILING_STOP
TSLA    $245.00    $243.50    5     -$7.50    FIRST_EXIT_-0.5%
META    $350.00    $352.00    8     +$16.00   MOMENTUM_FADE
QQQ     $310.00    $311.50    10    +$15.00   TRAILING_STOP

This is an automated email from your Trading Bot.
Paper Trading Account | All trades are simulated
```

---

## Troubleshooting

### Email Not Sending?

**"SMTP Authentication failed"**
- App password is incorrect
- Check you copied all 16 characters (with spaces)
- Re-generate a new app password

**"Connection refused"**
- Internet connection issue
- Check your network

**"No emails received"**
- Check spam/promotions folder
- Make sure `enabled: true` in config.yaml
- Verify recipient email is correct

### To Disable Email Notifications

Edit `config.yaml`:
```yaml
notifications:
  email:
    enabled: false  # ← Set to false
```

---

## Security Notes

1. **App passwords are safer than regular passwords**
   - Each app gets its own password
   - Can be revoked independently
   - Gmail never sees this in their app

2. **Don't commit config.yaml to Git**
   - The config with your password shouldn't be pushed
   - Add to `.gitignore` if needed

3. **The password is read-only**
   - Bot can only send email, not access your Gmail

---

## FAQ

**Q: Can I use a different email address?**
A: Yes, change `recipient_email` in config.yaml. Sender must be Gmail (due to app password requirement).

**Q: Will this work on paper trading accounts?**
A: Yes, you'll get emails regardless of paper/live. Emails note "Paper Trading Account".

**Q: Can I customize the email template?**
A: Edit `src/notifications/email_notifier.py` method `_generate_html_summary()` to customize the email format.

**Q: What if there are no trades that day?**
A: Email is sent but says "No trades to report".

---

## Ready?

1. ✅ Get app password from myaccount.google.com/apppasswords
2. ✅ Paste into config.yaml
3. ✅ Run `python src/main.py` tomorrow at 9:25 AM
4. ✅ Check email at 4:01 PM for daily summary

That's it!
