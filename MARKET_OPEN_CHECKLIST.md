# Market Open Ready - Complete Overview

**Market opens:** Tomorrow 9:30 AM ET

---

## ✅ What's Integrated Into the Bot

### Core Strategy
- **Entry Signal (9:35 AM ET)**: Volume spike (1.5x avg) + price above open
- **Exit Signals (throughout day)**:
  1. Final exit (-1.0% loss) - safety net
  2. First scale-out (-0.5% loss, sell 33%)
  3. Momentum fade (after 10 AM)
  4. Resistance rejection
  5. Trailing stop (0.75% below high)
  6. Time stop (4:00 PM ET)

### Dynamic Stock Screener ⭐ NEW
- Pre-market (9:30 AM): Analyzes 53 candidate stocks
- Scores each on: gap, momentum, volume, volatility, bullish trend
- Selects top 15 most likely to spike
- **Only trades high-probability setups** (not fixed list)

### Key Components
1. **Market Data Manager** - Fetches real-time bars, calculates averages
2. **Strategy Engine** - Evaluates entry/exit signals
3. **Executor** - Places orders, tracks P&L, enforces daily loss limit
4. **Stock Screener** - Identifies best candidates daily
5. **Alpaca Broker** - Connects to paper/live trading account

### Logging & Tracking
- Real-time logs: `logs/trading.log`
- All trades saved: `logs/trades.json`
- Results summary after each day

---

## ⚙️ Configuration Files

### 1. `config.yaml` - Main Trading Config
```yaml
trading:
  volume_spike_multiplier: 1.5      # Entry: volume must be 1.5x avg
  entry_time: "09:35"               # Check signals at 9:35 AM ET
  
  # Exit parameters
  trailing_stop_pct: 0.75           # Trail 0.75% below high
  first_exit_pct: 0.33              # Sell 33% at first pullback
  first_exit_loss_pct: -0.5         # At -0.5% loss
  final_exit_loss_pct: -1.0         # Exit all at -1.0% loss
  time_stop_hour: 16                # Close all at 4:00 PM
  
  # Position sizing & risk
  max_position_per_stock_usd: 10000 # Max $10k per stock
  max_daily_loss_usd: 1000          # Stop if daily loss > $1k
  
  # Screener
  use_daily_screener: true          # ENABLE dynamic stock selection
  candidates_file: "candidates.txt" # 53 candidate stocks
  num_stocks_to_trade: 15           # Top 15 selected each day
  min_screener_score: 35            # Quality threshold
  min_momentum: 0                   # Must be bullish (positive)
  trend_days: 5                     # Check last 5 days

broker:
  paper_trading: true               # PAPER TRADING (not real money)
  base_url: "https://paper-api.alpaca.markets"
```

### 2. `.env` - API Credentials
```
APCA_API_KEY_ID=PKW7UAK7RYRUHDLIITPCA2GNEQ
APCA_API_SECRET_KEY=BHtZm6jaegvUnFG6oMLzPzufpKKTy3YSm7X5tEZwWUWx
```
✅ Already set up

### 3. `candidates.txt` - Stock Universe
53 high-volume stocks to screen daily:
SPY, QQQ, AAPL, MSFT, GOOGL, GOOG, AMZN, NVDA, META, TSLA, ... (full list in file)

### 4. `screener_test_config.yaml` - Backtesting Config
Used for testing only (not needed for live trading)

---

## 🚀 Running the Bot Tomorrow

### Step 1: Verify Setup (Before Market Opens)
```bash
# Check credentials are loaded
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(f'API Key loaded: {bool(os.getenv(\"APCA_API_KEY_ID\")})')"

# Verify config is valid
python -c "import yaml; print(yaml.safe_load(open('config.yaml'))['trading']['use_daily_screener'])"
```

### Step 2: Start the Bot (9:25 AM ET)
Run 5 minutes before market opens:
```bash
python src/main.py
```

The bot will:
1. Connect to Alpaca (paper trading)
2. Wait for 9:30 AM (market open)
3. Show: "Market is open, monitoring for signals..."

### Step 3: Pre-Market Screener (9:30 AM ET)
Bot automatically runs screener:
```
===== PRE-MARKET SCREENER (9:30 ET) =====
Loaded 53 candidate symbols
Screening 53 stocks...
STOCK SCREENER RESULTS - 2026-08-10
NVDA   | Score: 87.5 | Gap: 2.3% | 5d Return: 5.2% | Vol Ratio: 1.8x | Price: $120.45
TSLA   | Score: 82.1 | Gap: 1.8% | 5d Return: 3.1% | Vol Ratio: 1.5x | Price: $245.30
...
Selected 15 stocks for trading today
Stocks: NVDA, TSLA, META, ...
```

### Step 4: Entry Check (9:35 AM ET)
Bot checks only the 15 selected stocks for entry signals

### Step 5: Monitor Until 4:00 PM
Logs show:
```
2026-08-10 09:35:15 - NVDA: Entry signal triggered. Volume: 5.2M (avg: 3.2M)
2026-08-10 09:35:16 - Entry order submitted for NVDA: 10 shares
2026-08-10 09:47:33 - NVDA: Exiting 3 shares (FIRST_EXIT_-0.5%)
2026-08-10 16:00:00 - Market closing, flattening all positions
```

### Step 6: Monitor Logs
In another terminal:
```bash
# Watch live trades
tail -f logs/trading.log

# View completed trades
cat logs/trades.json
```

---

## 📋 Pre-Market Checklist (Tomorrow 9:20 AM)

- [ ] Verify API credentials in `.env`
- [ ] Check `config.yaml` settings (especially `paper_trading: true`)
- [ ] Ensure screener is enabled (`use_daily_screener: true`)
- [ ] Verify `candidates.txt` exists with 53 stocks
- [ ] Run `python src/main.py` at 9:25 AM
- [ ] Monitor logs at 9:30-9:35 AM
- [ ] Watch positions open/close in real-time

---

## 🎯 Key Settings to Know

### Entry Strictness
```yaml
volume_spike_multiplier: 1.5   # Lower = more trades, higher = fewer, better quality
```
Current: **1.5x** (moderate - good for first day)

### Exit Speed
```yaml
trailing_stop_pct: 0.75        # Lower = exit faster on pullbacks
first_exit_loss_pct: -0.5      # Tighter = closer to entry
```
Current: **0.75% trail** (balanced)

### Position Size
```yaml
max_position_per_stock_usd: 10000  # Risk per stock
```
Current: **$10k** (moderate - adjust down if nervous)

### Daily Loss Limit (Circuit Breaker)
```yaml
max_daily_loss_usd: 1000       # Stop trading if losing
```
Current: **$1k loss limit** (stops bot if down $1k for the day)

### Screener Quality
```yaml
min_screener_score: 35         # Higher = fewer, better stocks
```
Current: **35** (selects top 15 out of 53)

---

## 📊 What to Expect

### Best Case (Profitable Day)
- Pre-market: Screener identifies 15 momentum stocks
- 9:35: 2-4 entry signals trigger
- Throughout day: Scale-outs at first pullback, trailing stop exits
- 4:00 PM: Close remaining, log P&L

### Realistic Case (Learning Day)
- Screener works, selects 12-15 stocks
- 9:35: 1-2 entries (slower than expected)
- Exits mix of profitable and small losses
- End of day: Break-even or small loss

### Worst Case (No Trades)
- Screener selects stocks but momentum doesn't hit at 9:35
- Volume spikes don't materialize
- No entries, no losses, 0 P&L day

---

## 🔧 If Something Goes Wrong

### Bot won't connect
```
Error: "APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set"
→ Check .env file exists and has credentials
```

### No entry signals
```
Entry checks complete. Monitoring exits...
(But no entries)
→ Lower volume_spike_multiplier from 1.5 to 1.2
→ Run backtest first to see if signals exist
```

### Paper account has no cash
```
Error: Insufficient funds
→ Reset paper account on alpaca.markets dashboard
→ Alpaca gives $10k to start
```

### Market data errors
```
Error fetching data for symbol
→ Symbol might be delisted or invalid
→ Check candidates.txt for typos
```

---

## ⏰ Timeline for Tomorrow

| Time | Action |
|------|--------|
| 9:20 AM ET | Run final checklist |
| 9:25 AM ET | Start `python src/main.py` |
| 9:30 AM ET | Screener runs, selects 15 stocks |
| 9:35 AM ET | Entry signal check, watch for trades |
| 9:36-4:00 PM | Monitor exits, watch logs |
| 4:00 PM ET | Bot closes all positions, logs results |
| After hours | Review `logs/trades.json`, analyze results |

---

## 📝 Important Notes

1. **Paper trading = NO REAL MONEY**
   - This is a test run
   - No actual funds at risk
   - Perfect for validation

2. **Bot runs continuously**
   - Checks for exits every 1 minute
   - Logs position updates
   - Keeps running until 4 PM or error

3. **Screener changes daily**
   - Different stocks selected each day
   - Based on today's gap/momentum patterns
   - NOT the same list as yesterday

4. **Orders are MARKET orders**
   - Executed immediately at market price
   - May have slippage at open
   - Necessary for speed at 9:35 AM

5. **All positions close at 4 PM**
   - No overnight holding
   - Clean reset each day

---

## 🎬 Ready to Go!

Everything is set up. Tomorrow at 9:25 AM, just run:

```bash
python src/main.py
```

Monitor `logs/trading.log` and watch it work!

Questions before market open? I'm here to help.
