import MetaTrader5 as mt5

# Bot Configuration

# Trading Pairs
SYMBOLS = ["GBPUSD", "XAUUSD", "US30"] # Note: MT5 symbol names can vary by broker, e.g., GBPUSDm or XAUUSD.pro

# Timeframe Settings (Scalping Cycle)
TIMEFRAME_CYCLE = {
    "constant": mt5.TIMEFRAME_M15,
    "situational_1": mt5.TIMEFRAME_M5,
    "situational_2": mt5.TIMEFRAME_M1,
    "entry": mt5.TIMEFRAME_M1
}

# Risk Management (Aggressive for account flipping)
MAX_RISK_PERCENT = 20.0  # Extremely aggressive risk to flip small account
DEFAULT_LOT_SIZE = 0.01  # Micro lot baseline

# Magic Number to identify bot's trades
MAGIC_NUMBER = 123456

# Dashboard Config
DASHBOARD_PORT = 5000
