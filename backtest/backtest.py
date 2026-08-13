#!/usr/bin/env python3
"""
Backtest the strategy against historical data without live broker connection.

Usage:
  python backtest/backtest.py --start 2024-01-01 --end 2024-08-01 --symbols AAPL MSFT SPY
"""

import sys
import os
import argparse
import pandas as pd
from datetime import datetime, timedelta
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.broker.alpaca_broker import AlpacaBroker
from src.strategy.strategy import Strategy
import yaml

def load_config(config_file="config.yaml"):
    """Load config from YAML"""
    with open(config_file, "r") as f:
        return yaml.safe_load(f)

def simulate_day(broker, strategy, symbol, date, config):
    """
    Simulate a single trading day using the rapid-increase entry window:
    scan minute bars from entry_window_start to entry_window_end and buy on
    the first sample where price has risen >= rapid_increase_pct within the
    trailing rapid_increase_lookback_minutes, then simulate exits with the
    existing exit rules for the rest of the day.
    """
    et = pytz.timezone("America/New_York")

    start_h, start_m = map(int, config["trading"]["entry_window_start"].split(":"))
    end_h, end_m = map(int, config["trading"]["entry_window_end"].split(":"))
    lookback = timedelta(minutes=config["trading"]["rapid_increase_lookback_minutes"])
    pct_threshold = config["trading"]["rapid_increase_pct"]

    entry_start = et.localize(datetime(date.year, date.month, date.day, start_h, start_m))
    entry_end = et.localize(datetime(date.year, date.month, date.day, end_h, end_m))
    day_close = et.localize(datetime(date.year, date.month, date.day, 16, 0))

    try:
        # Pull 1-minute bars for the whole session so we can both scan the
        # entry window and simulate exits from the same dataframe.
        bars = broker.get_historical_bars(symbol, entry_start, day_close, "1Min")
        if symbol not in bars or bars[symbol].empty:
            return None

        df = bars[symbol].sort_values("timestamp").reset_index(drop=True)
        window_df = df[df["timestamp"] < entry_end]

        # Scan the entry window for the first qualifying rapid-increase sample
        entry_idx = None
        for i in range(len(window_df)):
            now_ts = window_df.iloc[i]["timestamp"]
            now_price = window_df.iloc[i]["close"]
            cutoff = now_ts - lookback
            earlier = window_df[
                (window_df["timestamp"] >= cutoff) & (window_df["timestamp"] <= now_ts)
            ]
            if len(earlier) < 2:
                continue

            price_then = earlier.iloc[0]["close"]
            qty, pct_change = strategy.check_rapid_increase_entry(symbol, now_price, price_then)
            if qty > 0:
                entry_idx = i
                break

        if entry_idx is None:
            return None

        entry_row = window_df.iloc[entry_idx]
        entry_price = entry_row["close"]
        entry_time = entry_row["timestamp"]

        signal = strategy.enter_trade(symbol, entry_price, qty)
        if not signal:
            return None
        entry_qty = signal["qty"]

        # Simulate exits using bars after the entry point
        intra_df = df[df["timestamp"] > entry_time]

        exits = []
        for idx, row in intra_df.iterrows():
            current_price = row["close"]

            exit_qty = strategy.trades[symbol].check_final_exit(current_price)
            if exit_qty > 0:
                exits.append({
                    "time": row["timestamp"],
                    "qty": exit_qty,
                    "price": current_price,
                    "reason": "FINAL_EXIT",
                })
                break

            if not exits:
                exit_qty = strategy.trades[symbol].check_first_exit(current_price)
                if exit_qty > 0:
                    exits.append({
                        "time": row["timestamp"],
                        "qty": exit_qty,
                        "price": current_price,
                        "reason": "FIRST_EXIT",
                    })

            if not exits:
                exit_qty = strategy.trades[symbol].update_trailing_stop(current_price)
                if exit_qty > 0:
                    exits.append({
                        "time": row["timestamp"],
                        "qty": exit_qty,
                        "price": current_price,
                        "reason": "TRAILING_STOP",
                    })
                    break

        if exits:
            first_exit = exits[0]
            exit_price = first_exit["price"]
            exit_qty = first_exit["qty"]

            pnl = (exit_price - entry_price) * exit_qty
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100

            return {
                "date": date,
                "symbol": symbol,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "entry_qty": entry_qty,
                "exit_time": first_exit["time"],
                "exit_price": exit_price,
                "exit_qty": exit_qty,
                "reason": first_exit["reason"],
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }

        return None
    finally:
        # Backtest reuses one Strategy across every symbol/day - always clear
        # any open trade so a symbol that never fully exited in the exit-bar
        # window doesn't stay permanently "open" and block future days.
        if symbol in strategy.trades:
            del strategy.trades[symbol]

def backtest(symbols, start_date, end_date, config):
    """Run backtest"""
    broker = AlpacaBroker(paper=True)
    strategy = Strategy(config)

    results = []
    current_date = start_date

    total_days = (end_date - start_date).days
    processed = 0

    print(f"\nBacktesting {len(symbols)} symbols from {start_date} to {end_date}")
    print("=" * 80)

    while current_date < end_date:
        # Skip weekends
        if current_date.weekday() < 5:
            for symbol in symbols:
                try:
                    trade = simulate_day(broker, strategy, symbol, current_date, config)
                    if trade:
                        results.append(trade)
                        print(
                            f"{trade['date']} | {trade['symbol']:6} | "
                            f"Entry: ${trade['entry_price']:.2f} | "
                            f"Exit: ${trade['exit_price']:.2f} ({trade['reason']:15}) | "
                            f"P&L: ${trade['pnl']:8.2f} ({trade['pnl_pct']:6.2f}%)"
                        )
                except Exception as e:
                    pass

        current_date += timedelta(days=1)
        processed += 1
        if processed % 20 == 0:
            print(f"  ... processing {processed}/{total_days} days")

    # Print summary
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS")
    print("=" * 80)

    if not results:
        print("No trades generated in backtest period")
        return

    df = pd.DataFrame(results)

    total_trades = len(df)
    winning_trades = len(df[df["pnl"] > 0])
    losing_trades = len(df[df["pnl"] <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    total_pnl = df["pnl"].sum()
    avg_win = df[df["pnl"] > 0]["pnl"].mean() if winning_trades > 0 else 0
    avg_loss = df[df["pnl"] <= 0]["pnl"].mean() if losing_trades > 0 else 0

    print(f"\nTotal Trades: {total_trades}")
    print(f"Winning Trades: {winning_trades} ({win_rate:.1f}%)")
    print(f"Losing Trades: {losing_trades}")
    print(f"\nTotal P&L: ${total_pnl:.2f}")
    print(f"Avg Win: ${avg_win:.2f}")
    print(f"Avg Loss: ${avg_loss:.2f}")
    print(f"Avg Trade: ${df['pnl'].mean():.2f}")

    # By symbol
    print("\n" + "-" * 80)
    print("Results by symbol:")
    for symbol in symbols:
        symbol_trades = df[df["symbol"] == symbol]
        if len(symbol_trades) > 0:
            print(f"  {symbol:6} | Trades: {len(symbol_trades):3} | P&L: ${symbol_trades['pnl'].sum():10.2f}")

def main():
    parser = argparse.ArgumentParser(description="Backtest trading strategy")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="+", help="Symbols to backtest")

    args = parser.parse_args()

    config = load_config()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    symbols = args.symbols or config["trading"]["stock_universe"]

    backtest(symbols, start_date, end_date, config)

if __name__ == "__main__":
    main()
