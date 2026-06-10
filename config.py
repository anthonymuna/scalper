import MetaTrader5 as mt5

# Bot Configuration

# Trading Pairs
SYMBOLS = ["XAUUSD"]

# Timeframe Settings — fast scalping on M1/M5
TIMEFRAME_CYCLE = {
    "constant": mt5.TIMEFRAME_M15,       # Higher bias frame
    "situational_1": mt5.TIMEFRAME_M5,   # Trend confirmation
    "situational_2": mt5.TIMEFRAME_M1,   # Entry timing
    "entry": mt5.TIMEFRAME_M1
}

# Risk Management (Aggressive for account flipping $32 -> $1000)
MAX_RISK_PERCENT = 10.0      # Risk 10% per trade (aggressive but survivable)
DEFAULT_LOT_SIZE = 0.01      # Micro lot baseline
MAX_CONCURRENT_TRADES = 2    # Max open positions at once

# SL/TP in points (XAUUSD: 1 point = $0.01, so 500 points = $5.00)
# Spread is ~350-400 points, so SL must be wider than spread
MIN_SL_POINTS = 600          # Minimum SL = $6.00 (wider than spread)
DEFAULT_SL_POINTS = 800      # Default SL = $8.00
TP_RATIO = 1.5               # TP = 1.5x SL (risk:reward 1:1.5)

# Profit management
PROFIT_TARGET_USD = 0.50     # Close at $0.50 profit (fast scalp)
TIME_LIMIT_SECONDS = 300     # Close after 5 min if no profit

# Magic Number to identify bot's trades
MAGIC_NUMBER = 123456

# Scan interval
SCAN_INTERVAL_SECONDS = 10   # Check every 10 seconds

# Dashboard Config
DASHBOARD_PORT = 5000
