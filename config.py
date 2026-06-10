import os
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Trading Pairs
# ─────────────────────────────────────────────
SYMBOLS = ["XAUUSD"]

# ─────────────────────────────────────────────
#  Timeframe Settings
# ─────────────────────────────────────────────
TIMEFRAME_CYCLE = {
    "constant":      mt5.TIMEFRAME_M15,   # Higher bias frame
    "situational_1": mt5.TIMEFRAME_M5,    # Trend confirmation
    "situational_2": mt5.TIMEFRAME_M1,    # Entry timing
    "entry":         mt5.TIMEFRAME_M1,
}

# ─────────────────────────────────────────────
#  Risk Management
# ─────────────────────────────────────────────
# For a $6 account we risk 15% per trade (aggressive compounding).
# As balance grows the lot calculator automatically scales up.
MAX_RISK_PERCENT        = 15.0   # % of balance risked per trade
DEFAULT_LOT_SIZE        = 0.01   # Micro lot baseline (minimum)
MAX_CONCURRENT_TRADES   = 1      # Only 1 trade at a time on a micro account

# Daily hard-stop: if we lose this much of the STARTING daily balance, halt.
MAX_DAILY_LOSS_PERCENT  = 25.0   # 25% daily drawdown limit

# ─────────────────────────────────────────────
#  SL / TP  (XAUUSD: 1 point = $0.01 per 0.01 lot)
# ─────────────────────────────────────────────
# XAUUSD spread is typically 350–450 points.  SL must clear the spread.
MIN_SL_POINTS       = 500    # Absolute floor
DEFAULT_SL_POINTS   = 700    # Starting SL (overridden by swing-based calc)
TP_RATIO            = 2.0    # TP = 2× SL  (minimum 1:2 R:R for compounding)

# Partial close at 1:1 — close half the position, let the rest run
PARTIAL_CLOSE_RATIO = 0.5    # Close 50 % of position at 1:1 R:R

# ─────────────────────────────────────────────
#  Trailing Stop
# ─────────────────────────────────────────────
# Trailing activates once price is THIS MANY points in profit beyond entry.
TRAILING_ACTIVATION_POINTS = 300   # Start trailing after +300 pts profit
TRAILING_STEP_POINTS       = 150   # Trail SL by this distance behind price

# ─────────────────────────────────────────────
#  Signal Quality Filter
# ─────────────────────────────────────────────
MIN_SIGNAL_STRENGTH = 4      # Out of 7 — sniper threshold (was 2)

# Max spread allowed before refusing to enter (avoids news spikes)
MAX_SPREAD_POINTS   = 600    # Skip trade if spread > 600 points

# ATR settings for volatility filter (computed on M5, period 14)
ATR_PERIOD          = 14
ATR_MIN_POINTS      = 200    # Market too quiet below this — skip
ATR_MAX_POINTS      = 2500   # Market too chaotic above this (news) — skip

# ─────────────────────────────────────────────
#  Session Filter  (all times in UTC/GMT)
# ─────────────────────────────────────────────
# Gold is most liquid and predictable during London + NY overlap.
LONDON_SESSION_START    = 7    # 07:00 UTC
LONDON_SESSION_END      = 16   # 16:00 UTC
NY_SESSION_START        = 13   # 13:00 UTC
NY_SESSION_END          = 20   # 20:00 UTC

# ─────────────────────────────────────────────
#  Profit Management
# ─────────────────────────────────────────────
# Scale profit target as a % of balance (compounds automatically).
PROFIT_TARGET_PERCENT   = 8.0    # Close at 8 % profit of balance (per trade)

# Hard time-limit: close losing trade after this many seconds
TIME_LIMIT_SECONDS      = 600    # 10 minutes max in a losing trade

# ─────────────────────────────────────────────
#  Bot Identification
# ─────────────────────────────────────────────
MAGIC_NUMBER            = 123456

# ─────────────────────────────────────────────
#  Operational Settings
# ─────────────────────────────────────────────
SCAN_INTERVAL_SECONDS   = 10    # How often main loop ticks
DASHBOARD_PORT          = 5000

# ─────────────────────────────────────────────
#  Milestone alerts (USD balances to celebrate 🎯)
# ─────────────────────────────────────────────
BALANCE_MILESTONES = [10, 20, 50, 100, 200, 500, 1000]
