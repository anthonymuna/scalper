import MetaTrader5 as mt5

# Bot Configuration

# Trading Pairs
SYMBOLS = ["GBPUSD", "XAUUSD", "US30"] # Note: MT5 symbol names can vary by broker, e.g., GBPUSDm or XAUUSD.pro

# Timeframe Settings
TIMEFRAME_CYCLE = {
    "constant": mt5.TIMEFRAME_W1,
    "situational_1": mt5.TIMEFRAME_H4,
    "situational_2": mt5.TIMEFRAME_M30,
    "entry": mt5.TIMEFRAME_M5
}

# Risk Management
MAX_RISK_PERCENT = 1.0  # Max risk per trade
DEFAULT_LOT_SIZE = 0.01  # Micro lot

# Magic Number to identify bot's trades
MAGIC_NUMBER = 123456

# Dashboard Config
DASHBOARD_PORT = 5000
