import os
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Trading Symbols
#  Add/remove symbols. Bot scans all of them.
#  Deriv synthetics use their exact MT5 names.
# ─────────────────────────────────────────────
SYMBOLS = [
    "XAUUSD",           # Headway — Gold
    "EURUSD",           # Headway — Euro
    "GBPUSD",           # Headway — Cable
    "US100",            # Headway — Nasdaq (check exact name)
    "Volatility 75 Index",   # Deriv synthetic
    "Volatility 25 Index",   # Deriv synthetic
    "Boom 1000 Index",       # Deriv synthetic — longs only
    "Crash 1000 Index",      # Deriv synthetic — shorts only
]

# Symbols restricted to one direction (Boom/Crash)
LONG_ONLY_SYMBOLS  = ["Boom 1000 Index"]
SHORT_ONLY_SYMBOLS = ["Crash 1000 Index"]

# Synthetics — no session filter, trade 24/7
SYNTHETIC_SYMBOLS = [
    "Volatility 75 Index",
    "Volatility 25 Index",
    "Volatility 100 Index",
    "Boom 1000 Index",
    "Crash 1000 Index",
    "Step Index",
]

# ─────────────────────────────────────────────
#  Timeframe Architecture
#  Heiken Ashi used for Daily/H4 bias reading.
#  Regular candles for all structure + entry work.
# ─────────────────────────────────────────────
TF_DAILY   = mt5.TIMEFRAME_D1    # HA bias — bullish/bearish colour run
TF_H4      = mt5.TIMEFRAME_H4    # HA bias — trend continuation
TF_H1      = mt5.TIMEFRAME_H1    # Regular — OB / FVG / structure
TF_M15     = mt5.TIMEFRAME_M15   # Regular — liquidity sweep detection
TF_M5      = mt5.TIMEFRAME_M5    # Regular — entry trigger + BOS
TF_M1      = mt5.TIMEFRAME_M1    # Regular — CHoCH confirmation

# Candle counts per timeframe
BARS_DAILY = 10
BARS_H4    = 20
BARS_H1    = 50
BARS_M15   = 40
BARS_M5    = 100
BARS_M1    = 60

# ─────────────────────────────────────────────
#  Risk Tiers — auto-selected by balance
# ─────────────────────────────────────────────
RISK_TIERS = [
    (10,   0.5),    # $0–$10    → 0.5% per trade
    (50,   1.0),    # $10–$50   → 1.0%
    (200,  1.5),    # $50–$200  → 1.5%
    (1000, 2.0),    # $200–$1000→ 2.0%
    (float("inf"), 1.5),  # $1000+  → 1.5% (protect gains)
]

def get_risk_percent(balance: float) -> float:
    for threshold, pct in RISK_TIERS:
        if balance < threshold:
            return pct
    return 1.5

# ─────────────────────────────────────────────
#  Daily Loss Limits — auto-selected by balance
# ─────────────────────────────────────────────
DAILY_LOSS_TIERS = [
    (10,   3.0),
    (50,   4.0),
    (500,  5.0),
    (float("inf"), 6.0),
]

def get_daily_loss_limit(balance: float) -> float:
    for threshold, pct in DAILY_LOSS_TIERS:
        if balance < threshold:
            return pct
    return 6.0

# ─────────────────────────────────────────────
#  Trade Limits
# ─────────────────────────────────────────────
MAX_CONCURRENT_TRADES    = 3
MAX_TRADES_PER_SYMBOL    = 3       # per day
MIN_MINUTES_BETWEEN      = 30      # min gap between trades on same symbol
WEEKLY_DRAWDOWN_LIMIT    = 10.0    # % — pause bot, require manual restart
PEAK_EQUITY_DROP_LIMIT   = 20.0    # % from peak — emergency close all

# ─────────────────────────────────────────────
#  APA / SMC Signal Settings
# ─────────────────────────────────────────────
MIN_SIGNAL_SCORE         = 7.0     # out of 10 to fire a trade
MIN_SIGNAL_SCORE_HALF    = 5.0     # half lot if score 5–6
OB_LOOKBACK              = 5       # H1 candles to find OB
FVG_LOOKBACK             = 10      # H1 candles to find FVG
SWING_LOOKBACK           = 15      # bars for swing high/low
CHOCH_LOOKBACK           = 20      # M1 bars for CHoCH
ORDER_EXPIRY_BARS        = 3       # cancel pending after N M5 candles
MAX_HOLD_MINUTES         = 120     # close real-market trade after 2hrs

# ─────────────────────────────────────────────
#  Partial Take Profit
# ─────────────────────────────────────────────
TP1_RR      = 1.0     # close 30% at 1:1
TP2_RR      = 2.0     # close 40% at 1:2
TP3_RR      = 3.0     # trail remainder to 1:3+
TP1_PCT     = 30.0
TP2_PCT     = 40.0

# ─────────────────────────────────────────────
#  SL / TP
# ─────────────────────────────────────────────
MIN_SL_POINTS       = 400     # XAUUSD absolute floor
DEFAULT_SL_POINTS   = 600     # fallback if no OB found
SL_BUFFER_POINTS    = 50      # buffer beyond OB wick

# Per-symbol SL buffers (points)
SYMBOL_SL_BUFFER = {
    "XAUUSD":              80,
    "EURUSD":               5,
    "GBPUSD":               5,
    "US100":               20,
    "Volatility 75 Index": 200,
    "Volatility 25 Index": 100,
    "Boom 1000 Index":     150,
    "Crash 1000 Index":    150,
    "Step Index":           30,
}

# Per-symbol max spread (points)
SYMBOL_MAX_SPREAD = {
    "XAUUSD":              250,
    "EURUSD":               15,
    "GBPUSD":               20,
    "US100":                50,
    "Volatility 75 Index":   0,   # 0 = no limit (synthetics have fixed spread)
    "Volatility 25 Index":   0,
    "Boom 1000 Index":       0,
    "Crash 1000 Index":      0,
    "Step Index":             0,
}

# ─────────────────────────────────────────────
#  Volatility Gate (candle-based, no ATR)
# ─────────────────────────────────────────────
# Market dead if avg H1 candle body < this many points
MARKET_DEAD_BODY_POINTS   = 10
# Market choppy if >6 of last 8 H1 candles overlap previous
MARKET_CHOPPY_OVERLAP_MIN = 6

# ─────────────────────────────────────────────
#  Session Times (UTC — MT5 server is usually UTC)
# ─────────────────────────────────────────────
ASIAN_START    = 21    # 21:00 UTC (previous day) = 00:00 EAT
ASIAN_END      = 6     # 06:00 UTC = 09:00 EAT
DEAD_ZONE_START= 6     # 06:00 UTC = 09:00 EAT
DEAD_ZONE_END  = 8     # 08:00 UTC = 11:00 EAT
LONDON_START   = 8     # 08:00 UTC = 11:00 EAT
LONDON_END     = 17    # 17:00 UTC = 20:00 EAT
NY_START       = 12    # 12:30 UTC = 15:30 EAT (NY open)
NY_END         = 21    # 21:00 UTC = 00:00 EAT

# ─────────────────────────────────────────────
#  Heiken Ashi Bias Settings
# ─────────────────────────────────────────────
HA_TREND_BARS      = 3    # consecutive same-colour HA candles for bias
HA_STRONG_BARS     = 5    # no opposing wick = strong trend signal

# ─────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_FLUSH_SECS = 60   # batch messages, send max once per minute

# ─────────────────────────────────────────────
#  Misc
# ─────────────────────────────────────────────
MAGIC_NUMBER        = 202400
SCAN_INTERVAL_SECS  = 10
DASHBOARD_PORT      = 5000
BALANCE_MILESTONES  = [10, 20, 50, 100, 200, 500, 1000]

# ─────────────────────────────────────────────────────────────────────────────
#  SNIPER SCALP MODE (ICT 2-candle exits)
# ─────────────────────────────────────────────────────────────────────────────
# When enabled, ICT trades target a quick 2-candle exit:
#   - Check every candle after entry
#   - If trade is in profit at candle close → close immediately
#   - If not in profit → hold, check again next candle
#   - Hard candle limit: close anyway after MAX_SNIPER_CANDLES regardless
#   - Never close at a loss within the sniper window
#   - After sniper window expires → hand off to normal management

SNIPER_MODE_ENABLED    = True   # Enable 2-candle profit-only exits for ICT
SNIPER_CANDLE_TF       = 5      # M5 candles (same as scan TF)
MAX_SNIPER_CANDLES     = 6      # Max candles to hold before normal management
MIN_SNIPER_PROFIT_PTS  = 10     # Minimum profit in points to trigger close
                                 # Prevents closing on 0.1 pip gain
SNIPER_SYMBOLS = [              # Only these symbols use sniper mode
    "XAUUSD", "XAUUSDm",
    "EURUSD", "GBPUSD",
    "Volatility 75 Index",
    "Volatility 25 Index",
    "Boom 1000 Index",
    "Crash 1000 Index",
]
