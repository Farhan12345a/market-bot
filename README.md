# Market Opening Trading Bot

An algorithmic trading bot that identifies volume spikes at market open and executes trades with trailing stops and scale-out exits. **Paper trading ready** — test your strategy without real money.

## Strategy Overview

**Entry Signal** (9:35 AM ET):
- Volume spike: Current bar volume > 1.5x 20-day average
- Price above open: 5-minute bar close > open
- Auto-size position based on max position size config

**Exit Logic** (throughout the day):
- First scale-out: Sell 33% at -0.5% loss from entry
- Final exit: Sell remaining 67% at -1.0% loss from entry
- Trailing stop: Trail 0.75% below highest price since entry
- Time stop: Close all at 4:00 PM ET

## Setup

### 1. Install Dependencies

```bash
cd ~/market-bot
pip install -r requirements.txt
```

### 2. Get Alpaca API Credentials

1. Go to [alpaca.markets](https://alpaca.markets) and sign up (free)
2. On your dashboard, find your API keys
3. Set environment variables:

```bash
export APCA_API_KEY_ID="your_api_key"
export APCA_API_SECRET_KEY="your_api_secret"
```

Or save them in a `.env` file:
```
APCA_API_KEY_ID=your_api_key
APCA_API_SECRET_KEY=your_api_secret
```

Then in Python:
```python
from dotenv import load_dotenv
load_dotenv()
```

### 3. Configure Trading Parameters

Edit `config.yaml` to adjust:

```yaml
trading:
  volume_spike_multiplier: 1.5  # Volume must be 1.5x average
  entry_time: "09:35"  # Check for entry at 9:35 AM ET
  trailing_stop_pct: 0.75  # Trail stop by 0.75%
  first_exit_pct: 0.33  # Sell 33% at first pullback
  first_exit_loss_pct: -0.5  # First exit at -0.5% loss
  final_exit_loss_pct: -1.0  # Final exit at -1.0% loss
  max_position_per_stock_usd: 10000  # Max $ per stock
  max_daily_loss_usd: 1000  # Stop trading if daily loss > $1000
  stock_universe:
    - "SPY"
    - "QQQ"
    - "AAPL"
    # ... add more symbols
```

## Usage

### Paper Trading (Recommended First)

Run the live bot against paper trading (no real money):

```bash
python src/main.py
```

The bot will:
1. Connect to Alpaca paper trading
2. Wait for 9:30 AM ET market open
3. Check all configured stocks at 9:35 for entry signals
4. Monitor for exits throughout the day
5. Log all trades to `logs/trading.log`

### Backtest Against Historical Data

Before risking real money, test your strategy on past data:

```bash
# Backtest for 6 months, specific symbols
python backtest/backtest.py \
  --start 2024-01-01 \
  --end 2024-08-01 \
  --symbols AAPL MSFT SPY QQQ NVDA

# Backtest using default stock universe
python backtest/backtest.py \
  --start 2024-02-01 \
  --end 2024-08-01
```

Output:
- Trade-by-trade results (entry, exit, P&L)
- Win rate, average win/loss
- Results broken down by symbol

## File Structure

```
market-bot/
├── src/
│   ├── main.py              # Entry point - live trading loop
│   ├── broker/
│   │   └── alpaca_broker.py # Alpaca API wrapper
│   ├── strategy/
│   │   └── strategy.py      # Entry/exit logic
│   ├── executor/
│   │   └── executor.py      # Order submission & tracking
│   ├── data/
│   │   └── market_data.py   # Data fetching & calculations
│   └── utils/
├── backtest/
│   └── backtest.py          # Historical data testing
├── logs/                    # Trades and debug logs
├── config.yaml              # All tuning parameters
├── requirements.txt
└── README.md
```

## Key Parameters to Tune

**Entry Sensitivity:**
- `volume_spike_multiplier`: Lower = more trades, higher = fewer, higher-quality trades
- `entry_time`: When to check signals (9:35 is after first 5 min of chaos)

**Exit Aggressiveness:**
- `trailing_stop_pct`: Smaller = quicker to exit on minor pullbacks
- `first_exit_loss_pct` / `final_exit_loss_pct`: How much loss before exiting

**Risk Management:**
- `max_position_per_stock_usd`: Position size
- `max_daily_loss_usd`: Daily loss limit before shutting down

## Recommended Workflow

1. **Test the strategy on historical data**
   ```bash
   python backtest/backtest.py --start 2023-01-01 --end 2024-08-01
   ```
   Look for: win rate, average P&L, whether results make sense

2. **Run paper trading for 1-2 weeks**
   ```bash
   python src/main.py
   ```
   Check: Are fills reasonable? Does it trade the right stocks? Is slippage what you expected?

3. **Tune parameters based on results**
   - Edit `config.yaml`
   - Re-run backtest and paper trades
   - Iterate until you're confident

4. **Switch to live trading** (only after confident)
   - Change `broker.paper_trading: false` in `config.yaml`
   - Start with small position sizes
   - Monitor daily

## Logging

Logs go to both console and `logs/trading.log`:

```
2024-08-07 09:31:02 - __main__ - INFO - Market is open, monitoring for signals...
2024-08-07 09:35:15 - strategy - INFO - AAPL: Entry signal triggered. Volume: 5.2M (avg: 3.2M, spike: 1.5x)
2024-08-07 09:35:16 - executor - INFO - Entry order submitted for AAPL: 10 shares
2024-08-07 09:47:33 - strategy - INFO - AAPL: Exiting 3 shares (FIRST_EXIT_-0.5%)
```

All trades are also saved to `logs/trades.json` for analysis.

## Emergency: a position is stuck / untracked at the broker

If `RECONCILE` logs keep showing a symbol the bot is no longer tracking but
the broker still holds (e.g. `WLY: broker holds 18 but the bot is not
tracking it`), or you just want everything closed right now:

```
python3 ops/flatten-now.py --yes
```

This sells every position the broker actually holds, regardless of what the
bot's own tracking believes - do this first and ask questions after; a stuck
position with no active stop is the most dangerous state the bot can be in.
`retry_unconfirmed_exits()` in `Executor` is meant to catch this automatically
now (see `src/executor/executor.py`), but don't wait on it if you're already
looking at a live loss - close it by hand.

## Common Issues

**"No API credentials"**
- Make sure you've set `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` environment variables
- Or create a `.env` file and load it

**"No data for symbol"**
- The symbol may not be in Alpaca's data feed
- Try a major name like SPY, AAPL, MSFT first to verify setup

**"Paper account has no cash"**
- Alpaca starts paper accounts with $10k. If it's been depleted, reset the account on their dashboard.

**Orders not filling**
- Check market hours: 9:30 AM - 4:00 PM ET
- Wide spreads at open can cause slippage. Limit orders help, but bot uses market orders for speed.

## Next Steps

1. Try it on paper trading this week while traveling
2. Collect 1-2 weeks of paper trades
3. Analyze which symbols/conditions work best
4. Tune `volume_spike_multiplier`, exit thresholds, position size
5. Backtest the tuned parameters against 3-6 months of data
6. Only then consider live trading with small size

## Questions?

- Alpaca docs: https://alpaca.markets/docs
- Trading strategy discussion: see the logs and backtest results to understand what's working

---

**⚠️ Disclaimer**: This is a real trading bot. Test thoroughly on paper trading before using real money. No guarantees of profitability.
