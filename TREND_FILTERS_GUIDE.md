# Bullish Trend Filters - Configuration Guide

## Currently Active Filter

### 1. Simple Momentum Filter ✅ (DEFAULT)
**What it does:** Checks if price is higher now than N days ago
- **Config:** `min_momentum` and `trend_days` in `screener_test_config.yaml`
- **Status:** ACTIVE by default
- **Example:**
  ```yaml
  min_momentum: 0      # 0 = positive momentum required
  trend_days: 5        # Check last 5 days
  ```

---

## Optional Filters (COMMENTED OUT)

### 2. Moving Average Crossover ⏸️
**What it does:** Price must be above N-day Simple Moving Average (SMA)
- **File:** `run_screener_tests.py` around line 130
- **Status:** Commented out, ready to uncomment
- **How to enable:**
  1. Open `run_screener_tests.py`
  2. Find the comment: `# FILTER 1: Moving Average Crossover`
  3. Uncomment the code block below it
- **What it filters:**
  - Confirms uptrend (price above average = bullish)
  - Removes stocks in downtrends

**Example:**
```python
# Uncomment this block:
if len(df) >= 10:
    sma_10 = df.tail(10)['close'].mean()
    if df.iloc[-1]['close'] < sma_10:
        details['status'] = 'REJECTED_BELOW_SMA10'
        return 0, details
```

---

### 3. Higher Lows Pattern ⏸️
**What it does:** Checks if recent lows are increasing (textbook uptrend pattern)
- **File:** `run_screener_tests.py` around line 139
- **Status:** Commented out, ready to uncomment
- **What it filters:**
  - Only selects stocks with improving lows
  - Rejects stocks making lower lows (downtrend)
- **Good for:** Identifying sustained uptrends

**Example:**
```python
# Uncomment this block:
if len(df) >= 5:
    recent_lows = df.tail(5)['low'].tolist()
    is_higher_lows = all(recent_lows[i] < recent_lows[i+1] for i in range(len(recent_lows)-1))
    if not is_higher_lows:
        details['status'] = 'REJECTED_LOWER_LOWS'
        return 0, details
```

---

### 4. Bullish Days Ratio ⏸️
**What it does:** Requires X% of recent days to be up days (close > open)
- **File:** `run_screener_tests.py` around line 148
- **Status:** Commented out, ready to uncomment
- **What it filters:**
  - Only selects if 60%+ of recent days are green candles
  - Rejects stocks with too many red days
- **Good for:** Finding consistently bullish action

**Example:**
```python
# Uncomment this block:
if len(df) >= 5:
    recent_days = df.tail(5)
    up_days = (recent_days['close'] > recent_days['open']).sum()
    bullish_ratio = (up_days / len(recent_days)) * 100
    min_bullish_ratio = 60  # Require 60% of days to be up
    if bullish_ratio < min_bullish_ratio:
        details['status'] = 'REJECTED_LOW_BULLISH_RATIO'
        return 0, details
```

---

### 5. RSI > 50 Momentum Filter ⏸️
**What it does:** Uses Relative Strength Index to confirm bullish momentum
- **File:** `run_screener_tests.py` around line 160
- **Status:** Commented out, ready to uncomment
- **What it filters:**
  - RSI > 50 = bullish momentum
  - RSI < 50 = bearish momentum
- **Good for:** Technical momentum confirmation
- **Note:** More sophisticated than simple price comparison

**Example:**
```python
# Uncomment this block:
if len(df) >= 14:
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss if loss.iloc[-1] != 0 else 0
    rsi = 100 - (100 / (1 + rs))
    if rsi.iloc[-1] < 50:
        details['status'] = 'REJECTED_LOW_RSI'
        return 0, details
```

---

### 6. Consecutive Up Days ⏸️
**What it does:** Requires N consecutive green candles (bullish momentum)
- **File:** `run_screener_tests.py` around line 174
- **Status:** Commented out, ready to uncomment
- **What it filters:**
  - Only selects stocks with 2+ green candles in a row
  - Rejects stocks without recent momentum
- **Good for:** Finding stocks with immediate bullish action

**Example:**
```python
# Uncomment this block:
min_consecutive_up = 2  # Require 2+ consecutive green candles
if len(df) >= min_consecutive_up:
    recent_days = df.tail(min_consecutive_up)
    consecutive_up = (recent_days['close'] > recent_days['open']).all()
    if not consecutive_up:
        details['status'] = 'REJECTED_NO_CONSECUTIVE_UP'
        return 0, details
```

---

## How to Enable Multiple Filters

You can **combine multiple filters** by uncommenting several blocks. Example:

```python
# Enable Moving Average:
# Uncomment FILTER 1 (lines 130-134)

# Enable RSI check:
# Uncomment FILTER 4 (lines 160-171)

# Now stocks must pass BOTH:
# 1. Price > 10-day SMA
# 2. RSI > 50
```

Each uncommented filter adds another requirement (AND logic).

---

## Recommended Combinations

### Conservative (Strict)
```
- Simple Momentum (5 days, +0%)
- Moving Average (above 10-day SMA)
- Higher Lows (improving support levels)
```
Result: Very high-confidence bullish setups only

### Moderate (Balanced)
```
- Simple Momentum (5 days, +1%)
- Bullish Days Ratio (60%+ green days)
```
Result: Good balance of confirmation and flexibility

### Aggressive (Catch Early Moves)
```
- Simple Momentum (3 days, +0%)
- Consecutive Up Days (2+ green)
```
Result: Fast-trending stocks, more false signals

---

## Testing Different Filters

To test different filter combinations:

1. Edit `run_screener_tests.py` to uncomment desired filters
2. Run backtest: `python run_screener_tests.py`
3. Compare results to see which filters improve trade quality
4. Adjust `min_momentum` and `trend_days` in config file

---

## Default Behavior

Right now, **only the Simple Momentum Filter is active**:
```yaml
min_momentum: 0      # Stock must be up (positive momentum)
trend_days: 5        # Over last 5 days
```

This is sufficient for most use cases. The optional filters are available when you want more sophisticated trend detection.
