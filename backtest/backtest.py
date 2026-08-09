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
    """Simulate a single trading day"""
    # Get daily bar
    bars = broker.get_historical_bars(symbol, date, date + timedelta(days=1), "1Day")
    if symbol not in bars or bars[symbol].empty:
        return None

    daily_bar = bars[symbol].iloc[0]

    # Get 20-day average volume
    start_20d = date - timedelta(days=30)
    bars_20d = broker.get_historical_bars(symbol, start_20d, date, "1Day")
    if symbol not in bars_20d or bars_20d[symbol].empty:
        avg_volume = 1000000
    else:
        avg_volume = bars_20d[symbol]["volume"].tail(20).mean()

    # Get opening bar (first 5 minutes)
    open_time = datetime(date.year, date.month, date.day, 9, 30, tzinfo=pytz.timezone("America/New_York"))
    close_time = open_time + timedelta(minutes=5)

    bars_open = broker.get_historical_bars(symbol, open_time, close_time, "5Min")
    if symbol not in bars_open or bars_open[symbol].empty:
        return None

    open_bar = bars_open[symbol].iloc[0].to_dict()

    # Check entry signal
    signal = strategy.process_bar(symbol, open_bar, avg_volume)

    if signal and signal.get("action") == "ENTRY":
        entry_price = open_bar["close"]
        entry_qty = signal["qty"]
        entry_time = open_time

        # Simulate the rest of the day with intraday bars
        bars_intra = broker.get_historical_bars(symbol, open_time, datetime(date.year, date.month, date.day, 16, 0), "1Min")
        if symbol not in bars_intra or bars_intra[symbol].empty:
            return None

        intra_df = bars_intra[symbol]

        # Simulate exits throughout the day
        exits = []
        for idx, row in intra_df.iterrows():
            current_price = row["close"]

            # Check exit conditions
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
