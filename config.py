import os
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Trading Pairs
# ─────────────────────────────────────────────
SYMBOLS = ["XAUUSD"]

# ─────────────────────────────────────────────
#  Timeframe Settings  (5M-only APA)
# ─────────────────────────────────────────────
TIMEFRAME_CYCLE = {
    "constant":      mt5.TIMEFRAME_M5,    # Trend bias (200 bars of M5)
    "situational_1": mt5.TIMEFRAME_M5,    # Momentum / FVG confirmation
    "situational_2": mt5.TIMEFRAME_M1,    # CHoCH entry trigger
    "entry":         mt5.TIMEFRAME_M1,
}

# ─────────────────────────────────────────────
#  Risk Management  — conservative compounding
# ─────────────────────────────────────────────
# Stage-based risk: bot uses MAX_RISK_PERCENT.
# Manually increase as account grows:
#   $0–$50   → 2%
#   $50–$200 → 3%
#   $200+    → 5%
MAX_RISK_PERCENT        = 2.0    # % of balance risked per trade (START CONSERVATIVE)
DEFAULT_LOT_SIZE        = 0.01   # Micro lot baseline (minimum)
MAX_CONCURRENT_TRADES   = 1      # Only 1 trade at a time

# Daily hard-stop: halt if day's drawdown hits this %
MAX_DAILY_LOSS_PERCENT  = 6.0    # 3× risk per trade = stop for the day

# ─────────────────────────────────────────────
#  SL / TP  (XAUUSD: 1 point = $0.01 per 0.01 lot)
# ─────────────────────────────────────────────
# Spread on XAUUSD is typically 350–450 points.
MIN_SL_POINTS       = 400    # Absolute floor — must clear spread
DEFAULT_SL_POINTS   = 600    # Fallback SL (swing-based overrides this)
TP_RATIO            = 2.5    # TP = 2.5× SL — sniper entries deserve better RR

# Partial close at 1:1 R:R — bank 50%, let rest run to full TP
PARTIAL_CLOSE_RATIO = 0.5

# ─────────────────────────────────────────────
#  Trailing Stop
# ─────────────────────────────────────────────
TRAILING_ACTIVATION_POINTS = 250   # Activate trail after +250pts profit
TRAILING_STEP_POINTS       = 120   # Trail SL this many pts behind price

# ─────────────────────────────────────────────
#  Signal Quality — SNIPER threshold
# ─────────────────────────────────────────────
MIN_SIGNAL_STRENGTH     = 5      # Raised from 4 → 5 (CHoCH now required for 5+)
REQUIRE_CHOCH           = True   # Only enter on structural confirmation

# Max spread before skipping trade
MAX_SPREAD_POINTS       = 500    # Tighter than before (was 600)

# ATR volatility gate (on M5, period 14)
ATR_PERIOD              = 14
ATR_MIN_POINTS          = 150    # Skip if market is dead
ATR_MAX_POINTS          = 2000   # Skip during news spikes

# ─────────────────────────────────────────────
#  Killzone Filter  (UTC) — sniper windows only
# ─────────────────────────────────────────────
# London open killzone: 07:00–08:30 UTC
LONDON_KZ_START     = 7
LONDON_KZ_END       = 9     # hour only — minute handled in code

# NY open killzone: 13:00–14:30 UTC
NY_KZ_START         = 13
NY_KZ_END           = 15

# Legacy session bounds (used as outer guard)
LONDON_SESSION_START    = 7
LONDON_SESSION_END      = 16
NY_SESSION_START        = 13
NY_SESSION_END          = 20

# ─────────────────────────────────────────────
#  FVG Entry Settings
# ─────────────────────────────────────────────
USE_FVG_ENTRY           = True   # Wait for price to tap into FVG before entering
FVG_LOOKBACK_BARS       = 10     # How many M5 bars back to search for FVG
FVG_EXPIRY_BARS         = 5      # Cancel FVG order if not filled within N bars

# ─────────────────────────────────────────────
#  Profit Management
# ─────────────────────────────────────────────
PROFIT_TARGET_PERCENT   = 5.0    # Close at 5% profit of balance (per trade)
TIME_LIMIT_SECONDS      = 900    # 15 min max in a losing trade (was 10)

# ─────────────────────────────────────────────
#  Bot Identification
# ─────────────────────────────────────────────
MAGIC_NUMBER            = 123456

# ─────────────────────────────────────────────
#  Operational Settings
# ─────────────────────────────────────────────
SCAN_INTERVAL_SECONDS   = 10
DASHBOARD_PORT          = 5000

# ─────────────────────────────────────────────
#  Milestone alerts (USD balances)
# ─────────────────────────────────────────────
BALANCE_MILESTONES = [10, 20, 50, 100, 200, 500, 1000]
