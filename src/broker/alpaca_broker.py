import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import pandas as pd
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class AlpacaBroker:
    def __init__(self, api_key=None, api_secret=None, paper=True):
        self.api_key = api_key or os.getenv("APCA_API_KEY_ID")
        self.api_secret = api_secret or os.getenv("APCA_API_SECRET_KEY")

        if not self.api_key or not self.api_secret:
            raise ValueError("APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set")

        self.paper = paper
        base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"

        self.trading_client = TradingClient(
            api_key=self.api_key,
            secret_key=self.api_secret,
            base_url=base_url
        )
        self.data_client = StockHistoricalDataClient(self.api_key, self.api_secret)

    def get_account(self):
        """Get account info"""
        return self.trading_client.get_account()

    def get_positions(self):
        """Get all open positions"""
        try:
            positions = self.trading_client.get_all_positions()
            return {p.symbol: p for p in positions}
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return {}

    def get_position(self, symbol):
        """Get position for a specific symbol"""
        try:
            return self.trading_client.get_open_position(symbol)
        except Exception:
            return None

    def submit_market_order(self, symbol, qty, side="buy"):
        """Submit market order"""
        try:
            order_request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            order = self.trading_client.submit_order(order_request)
            logger.info(f"Market order submitted: {symbol} {qty} {side.upper()}")
            return order
        except Exception as e:
            logger.error(f"Error submitting market order: {e}")
            raise

    def submit_limit_order(self, symbol, qty, limit_price, side="buy"):
        """Submit limit order"""
        try:
            order_request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                limit_price=limit_price,
                side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            order = self.trading_client.submit_order(order_request)
            logger.info(f"Limit order submitted: {symbol} {qty} {side.upper()} @ {limit_price}")
            return order
        except Exception as e:
            logger.error(f"Error submitting limit order: {e}")
            raise

    def get_historical_bars(self, symbols, start, end, timeframe="1Day"):
        """Get historical bar data"""
        try:
            if isinstance(symbols, str):
                symbols = [symbols]

            tf_map = {
                "1Min": TimeFrame.Minute,
                "5Min": TimeFrame.FiveMin,
                "15Min": TimeFrame.FifteenMin,
                "1Hour": TimeFrame.Hour,
                "1Day": TimeFrame.Day,
            }

            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                start=start,
                end=end,
                timeframe=tf_map.get(timeframe, TimeFrame.Day)
            )

            bars = self.data_client.get_stock_bars(request)

            # Convert to DataFrame per symbol
            result = {}
            for symbol in symbols:
                if symbol in bars:
                    bar_list = bars[symbol]
                    data = [
                        {
                            "timestamp": bar.timestamp,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        }
                        for bar in bar_list
                    ]
                    result[symbol] = pd.DataFrame(data)

            return result
        except Exception as e:
            logger.error(f"Error fetching historical bars: {e}")
            return {}

    def get_latest_bars(self, symbols, timeframe="1Min"):
        """Get the latest bar for symbols (useful for real-time checks)"""
        try:
            if isinstance(symbols, str):
                symbols = [symbols]

            end = datetime.now()
            start = end - pd.Timedelta(hours=1)

            bars_data = self.get_historical_bars(symbols, start, end, timeframe)

            result = {}
            for symbol, df in bars_data.items():
                if not df.empty:
                    result[symbol] = df.iloc[-1].to_dict()

            return result
        except Exception as e:
            logger.error(f"Error fetching latest bars: {e}")
            return {}
