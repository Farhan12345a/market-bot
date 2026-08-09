# Quick Start Guide

Get the bot running in 5 minutes.

## Step 1: Set Up Credentials

```bash
# Copy the example env file
cp .env.example .env

# Edit .env with your Alpaca API keys
nano .env
```

Get your keys from: https://alpaca.markets/dashboard/api-keys

## Step 2: Install & Run

```bash
# Install dependencies
pip install -r requirements.txt

# Run paper trading
python src/main.py
```

The bot will connect and wait for 9:30 AM ET market open.

## Step 3: Test Your Strategy (Optional, Recommended)

```bash
# Backtest on recent data
python backtest/backtest.py \
  --start 2024-06-01 \
  --end 2024-08-01 \
  --symbols SPY QQQ AAPL

# Check the output for win rate, P&L
```

## Monitoring While Running

In another terminal:

```bash
# Watch the live log
tail -f logs/trading.log

# View trades as JSON
cat logs/trades.json
```

## Adjust Settings

All parameters are in `config.yaml`:

```yaml
trading:
  volume_spike_multiplier: 1.5  # Lower = more trades
  trailing_stop_pct: 0.75       # Exit earlier if lower
  first_exit_loss_pct: -0.5     # 33% scale-out threshold
  final_exit_loss_pct: -1.0     # Final exit threshold
  max_daily_loss_usd: 1000      # Kill switch limit
```

Change values, re-run backtest, repeat.

## Common First Run Issues

| Problem | Solution |
|---------|----------|
| "No API credentials" | Check .env file has correct keys from alpaca.markets |
| "No data for SYMBOL" | Try SPY instead to verify setup works |
| "Paper account has no cash" | Reset account in Alpaca dashboard |
| "No trades generated" | Try lower volume_spike_multiplier (1.2 instead of 1.5) |

## Next: Go to Live Trading

1. Run paper trading for 2+ weeks
2. Review logs and `logs/trades.json`
3. Backtest your settings against 6 months of data
4. If confident: change `config.yaml` `broker.paper_trading: false`
5. Start small ($1-5k positions) on first day
6. Monitor closely

---

Questions? See README.md for more details.
