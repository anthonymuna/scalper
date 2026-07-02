"""
strategy.py — NGAO Scalper | Pure Price Action | APA/SMC Engine
================================================================
Architecture:
  Heiken Ashi  → Daily + H4  : bias direction only
  Regular candles → H1/M15/M5/M1 : all structure, entry, exit work

Signal Flow:
  1. HA Daily bias
  2. HA H4 bias (must agree with Daily)
  3. H1 BOS/CHoCH — structure confirms direction
  4. H1 OB + FVG detection
  5. M15 liquidity sweep detection
  6. M5 BOS confirmation
  7. M1 CHoCH entry trigger
  8. Confluence scoring 0–10 (min 7 to trade)

Zero indicators. Every function reads raw candle or HA data only.
"""

import pandas as pd
import numpy as np
from config import (
    OB_LOOKBACK, FVG_LOOKBACK, SWING_LOOKBACK, CHOCH_LOOKBACK,
    HA_TREND_BARS, HA_STRONG_BARS,
    MIN_SIGNAL_SCORE, MIN_SIGNAL_SCORE_HALF,
    LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS,
    MARKET_DEAD_BODY_POINTS, MARKET_CHOPPY_OVERLAP_MIN,
)


# ─────────────────────────────────────────────────────────────────────────────
#  HEIKEN ASHI CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def to_heiken_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert OHLC dataframe to Heiken Ashi candles.
    HA is used ONLY for Daily and H4 bias — never for entry or SL.

    HA Close = (O + H + L + C) / 4
    HA Open  = (prev_HA_Open + prev_HA_Close) / 2
    HA High  = max(High, HA_Open, HA_Close)
    HA Low   = min(Low,  HA_Open, HA_Close)
    """
    ha = df.copy()
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0

    ha_open = [0.0] * len(df)
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha["ha_close"].iloc[i - 1]) / 2.0

    ha["ha_open"]  = ha_open
    ha["ha_high"]  = ha[["high", "ha_open", "ha_close"]].max(axis=1)
    ha["ha_low"]   = ha[["low",  "ha_open", "ha_close"]].min(axis=1)
    ha["ha_bull"]  = ha["ha_close"] > ha["ha_open"]
    ha["ha_no_lower_wick"] = ha["ha_low"]  == ha[["ha_open", "ha_close"]].min(axis=1)
    ha["ha_no_upper_wick"] = ha["ha_high"] == ha[["ha_open", "ha_close"]].max(axis=1)
    return ha


def get_ha_bias(df: pd.DataFrame) -> int:
    """
    Read Heiken Ashi bias from last N candles.

    Strong bull  (+2): HA_STRONG_BARS consecutive bull candles,
                       last candle has no lower wick
    Bull         (+1): HA_TREND_BARS consecutive bull candles
    Strong bear  (-2): HA_STRONG_BARS consecutive bear + no upper wick
    Bear         (-1): HA_TREND_BARS consecutive bear candles
    Neutral       (0): mixed or transitioning

    Returns: 2, 1, 0, -1, -2
    """
    if len(df) < HA_STRONG_BARS + 1:
        return 0

    ha = to_heiken_ashi(df)
    recent = ha.tail(HA_STRONG_BARS)

    all_bull = recent["ha_bull"].all()
    all_bear = (~recent["ha_bull"]).all()

    last = ha.iloc[-1]

    if all_bull:
        if last["ha_no_lower_wick"]:
            return 2   # Strong bullish
        return 1       # Bullish

    if all_bear:
        if last["ha_no_upper_wick"]:
            return -2  # Strong bearish
        return -1      # Bearish

    # Check shorter window
    short = ha.tail(HA_TREND_BARS)
    if short["ha_bull"].all():
        return 1
    if (~short["ha_bull"]).all():
        return -1

    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET CONDITION CLASSIFIER — Pure candle behaviour, no ATR
# ─────────────────────────────────────────────────────────────────────────────

def classify_market(df_h1: pd.DataFrame, symbol_point: float) -> str:
    """
    Returns: 'trending_bull' | 'trending_bear' | 'ranging' | 'choppy' | 'dead'

    Uses H1 regular candles — candle body size and HH/HL structure.
    Zero indicators.
    """
    if len(df_h1) < 10:
        return "dead"

    recent = df_h1.tail(10).reset_index(drop=True)

    # Dead: average candle range too small
    avg_range = (recent["high"] - recent["low"]).mean()
    if symbol_point > 0 and (avg_range / symbol_point) < MARKET_DEAD_BODY_POINTS:
        return "dead"

    # Overlap count — choppy if most candles overlap previous
    overlap_count = 0
    for i in range(1, len(recent)):
        curr_h = recent.loc[i, "high"]
        curr_l = recent.loc[i, "low"]
        prev_h = recent.loc[i-1, "high"]
        prev_l = recent.loc[i-1, "low"]
        if curr_h > prev_l and curr_l < prev_h:
            overlap_count += 1

    if overlap_count >= MARKET_CHOPPY_OVERLAP_MIN:
        return "choppy"

    # Trend: count HH/HL (bull) or LL/LH (bear)
    bull_count = 0
    bear_count = 0
    for i in range(1, len(recent)):
        hh = recent.loc[i, "high"]  > recent.loc[i-1, "high"]
        hl = recent.loc[i, "low"]   > recent.loc[i-1, "low"]
        ll = recent.loc[i, "low"]   < recent.loc[i-1, "low"]
        lh = recent.loc[i, "high"]  < recent.loc[i-1, "high"]
        if hh and hl:
            bull_count += 1
        if ll and lh:
            bear_count += 1

    # Strong body conviction
    strong_bodies = sum(
        1 for i in range(len(recent))
        if (recent.loc[i, "high"] - recent.loc[i, "low"]) > 0 and
           abs(recent.loc[i, "close"] - recent.loc[i, "open"]) /
           (recent.loc[i, "high"] - recent.loc[i, "low"]) > 0.5
    )

    if bull_count >= 5 and strong_bodies >= 3:
        return "trending_bull"
    if bear_count >= 5 and strong_bodies >= 3:
        return "trending_bear"

    return "ranging"


# ─────────────────────────────────────────────────────────────────────────────
#  BOS / CHoCH DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_bos_choch(df: pd.DataFrame, lookback: int = 15) -> dict:
    """
    Detect Break of Structure and Change of Character.

    Scans the last `lookback` candles for swing highs/lows,
    then checks if current price broke above/below those swings.

    Returns:
      {
        "bos_bull":   bool,  # bullish BOS
        "bos_bear":   bool,  # bearish BOS
        "choch_bull": bool,  # bullish CHoCH (trend reversal up)
        "choch_bear": bool,  # bearish CHoCH (trend reversal down)
        "last_swing_high": float,
        "last_swing_low":  float,
        "prev_swing_high": float,
        "prev_swing_low":  float,
      }
    """
    result = {
        "bos_bull": False, "bos_bear": False,
        "choch_bull": False, "choch_bear": False,
        "last_swing_high": 0.0, "last_swing_low": 0.0,
        "prev_swing_high": 0.0, "prev_swing_low": 0.0,
    }

    if len(df) < lookback + 2:
        return result

    window = df.iloc[-(lookback + 2):-1].reset_index(drop=True)
    current_close = df.iloc[-1]["close"]

    swing_highs, swing_lows = [], []

    for i in range(1, len(window) - 1):
        if window.loc[i, "high"] > window.loc[i-1, "high"] and \
           window.loc[i, "high"] > window.loc[i+1, "high"]:
            swing_highs.append(window.loc[i, "high"])
        if window.loc[i, "low"] < window.loc[i-1, "low"] and \
           window.loc[i, "low"] < window.loc[i+1, "low"]:
            swing_lows.append(window.loc[i, "low"])

    if len(swing_highs) >= 2:
        result["last_swing_high"] = swing_highs[-1]
        result["prev_swing_high"] = swing_highs[-2]
    elif len(swing_highs) == 1:
        result["last_swing_high"] = swing_highs[0]

    if len(swing_lows) >= 2:
        result["last_swing_low"] = swing_lows[-1]
        result["prev_swing_low"] = swing_lows[-2]
    elif len(swing_lows) == 1:
        result["last_swing_low"] = swing_lows[0]

    last_sh = result["last_swing_high"]
    last_sl = result["last_swing_low"]
    prev_sh = result["prev_swing_high"]
    prev_sl = result["prev_swing_low"]

    # BOS: price closes beyond a previous swing
    if last_sh > 0 and current_close > last_sh:
        result["bos_bull"] = True
    if last_sl > 0 and current_close < last_sl:
        result["bos_bear"] = True

    # CHoCH: trend change — HH followed by LL break (or vice versa)
    if prev_sh > 0 and last_sh > 0 and prev_sl > 0 and last_sl > 0:
        # Bullish CHoCH: was making LL/LH, now breaks above last SH
        if last_sh > prev_sh and last_sl > prev_sl and current_close > last_sh:
            result["choch_bull"] = True
        # Bearish CHoCH: was making HH/HL, now breaks below last SL
        if last_sh < prev_sh and last_sl < prev_sl and current_close < last_sl:
            result["choch_bear"] = True

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER BLOCK DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def find_order_block(df: pd.DataFrame, direction: int,
                     lookback: int = 5) -> dict | None:
    """
    Find the most recent unmitigated Order Block.

    Bullish OB: last bearish candle before a strong bullish impulse
                that broke structure above it. Price is now returning to it.
    Bearish OB: last bullish candle before a strong bearish impulse.

    Returns: {high, low, mid, mitigated} or None
    """
    if len(df) < lookback + 2:
        return None

    current_price = df.iloc[-1]["close"]
    window = df.iloc[-(lookback + 2):].reset_index(drop=True)

    for i in range(1, len(window) - 1):
        o = window.loc[i, "open"]
        c = window.loc[i, "close"]
        h = window.loc[i, "high"]
        l = window.loc[i, "low"]
        next_c = window.loc[i+1, "close"]

        if direction == 1:
            # Bullish OB: bearish candle (c < o) followed by break above its high
            is_bearish = c < o
            impulse_up = next_c > h
            if is_bearish and impulse_up:
                # Not yet mitigated: current price still above OB low
                mitigated = current_price < l
                return {
                    "high": float(h), "low": float(l),
                    "mid":  float((h + l) / 2.0),
                    "mitigated": mitigated
                }

        elif direction == -1:
            # Bearish OB: bullish candle (c > o) followed by break below its low
            is_bullish = c > o
            impulse_dn = next_c < l
            if is_bullish and impulse_dn:
                mitigated = current_price > h
                return {
                    "high": float(h), "low": float(l),
                    "mid":  float((h + l) / 2.0),
                    "mitigated": mitigated
                }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  FAIR VALUE GAP (FVG)
# ─────────────────────────────────────────────────────────────────────────────

def find_fvg(df: pd.DataFrame, direction: int,
             lookback: int = 10) -> dict | None:
    """
    Detect the most recent Fair Value Gap.

    Bullish FVG: candle[i-2].high < candle[i].low  (upside gap)
    Bearish FVG: candle[i-2].low  > candle[i].high (downside gap)

    Returns: {top, bottom, mid, filled} or None
    """
    if len(df) < lookback + 3:
        return None

    window = df.iloc[-lookback:].reset_index(drop=True)
    current_price = df.iloc[-1]["close"]

    for i in range(len(window) - 1, 1, -1):
        c0 = window.iloc[i - 2]
        c2 = window.iloc[i]

        if direction == 1:
            if c0["high"] < c2["low"]:
                bottom = float(c0["high"])
                top    = float(c2["low"])
                filled = current_price < bottom
                return {
                    "top": top, "bottom": bottom,
                    "mid": (top + bottom) / 2.0,
                    "filled": filled
                }

        elif direction == -1:
            if c0["low"] > c2["high"]:
                top    = float(c0["low"])
                bottom = float(c2["high"])
                filled = current_price > top
                return {
                    "top": top, "bottom": bottom,
                    "mid": (top + bottom) / 2.0,
                    "filled": filled
                }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  LIQUIDITY SWEEP DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_liquidity_sweep(df: pd.DataFrame, direction: int,
                           lookback: int = 8) -> bool:
    """
    Detect a liquidity sweep on M15 — wick beyond swing then close back.

    Bullish sweep: wick went below swing low but candle closed above it.
    Bearish sweep: wick went above swing high but closed below it.
    """
    if len(df) < lookback + 3:
        return False

    ref_window = df.iloc[-(lookback + 3):-3]
    if len(ref_window) == 0:
        return False

    swing_high = ref_window["high"].max()
    swing_low  = ref_window["low"].min()

    # Check last 3 candles for the sweep
    recent = df.iloc[-3:].reset_index(drop=True)

    for i in range(len(recent)):
        c_low   = recent.loc[i, "low"]
        c_high  = recent.loc[i, "high"]
        c_close = recent.loc[i, "close"]

        if direction == 1:
            # Bullish sweep: wick below swing low, closes back above
            if c_low < swing_low and c_close > swing_low:
                return True

        elif direction == -1:
            # Bearish sweep: wick above swing high, closes back below
            if c_high > swing_high and c_close < swing_high:
                return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
#  REJECTION CANDLE (Pin Bar / Engulfing)
# ─────────────────────────────────────────────────────────────────────────────

def has_rejection_candle(df: pd.DataFrame, direction: int) -> bool:
    """
    Check last closed M5 candle for rejection pattern.

    Bullish: pin bar (long lower wick > 2× body) or bullish engulfing
    Bearish: pin bar (long upper wick > 2× body) or bearish engulfing
    """
    if len(df) < 3:
        return False

    last = df.iloc[-2]   # last CLOSED candle
    prev = df.iloc[-3]

    o = last["open"];  c = last["close"]
    h = last["high"];  l = last["low"]

    body      = abs(c - o)
    top_wick  = h - max(o, c)
    bot_wick  = min(o, c) - l
    total_rng = h - l

    if total_rng == 0:
        return False

    if direction == 1:
        pin_bar  = (bot_wick > body * 2) and (c > o)
        engulf   = (c > o) and (c > prev["high"]) and (o < prev["low"])
        return pin_bar or engulf

    if direction == -1:
        pin_bar  = (top_wick > body * 2) and (c < o)
        engulf   = (c < o) and (c < prev["low"]) and (o > prev["high"])
        return pin_bar or engulf

    return False


# ─────────────────────────────────────────────────────────────────────────────
#  SWING SL ANCHOR
# ─────────────────────────────────────────────────────────────────────────────

def find_swing_low(df: pd.DataFrame, lookback: int = 15) -> float:
    return float(df["low"].iloc[-lookback:].min())


def find_swing_high(df: pd.DataFrame, lookback: int = 15) -> float:
    return float(df["high"].iloc[-lookback:].max())


# ─────────────────────────────────────────────────────────────────────────────
#  STRUCTURE TRAILING STOP — pure price action
# ─────────────────────────────────────────────────────────────────────────────

def get_structure_trail_sl(df_m5: pd.DataFrame, direction: int) -> float:
    """
    Trail SL using M5 swing structure.
    Bull: trail below most recent Higher Low.
    Bear: trail above most recent Lower High.
    Returns 0 if no valid swing found.
    """
    if len(df_m5) < 10:
        return 0.0

    recent = df_m5.tail(10).reset_index(drop=True)

    if direction == 1:
        for i in range(len(recent) - 2, 0, -1):
            if (recent.loc[i, "low"] < recent.loc[i-1, "low"] and
                    recent.loc[i, "low"] < recent.loc[i+1, "low"]):
                return float(recent.loc[i, "low"])

    elif direction == -1:
        for i in range(len(recent) - 2, 0, -1):
            if (recent.loc[i, "high"] > recent.loc[i-1, "high"] and
                    recent.loc[i, "high"] > recent.loc[i+1, "high"]):
                return float(recent.loc[i, "high"])

    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  PREVIOUS DAY HIGH / LOW
# ─────────────────────────────────────────────────────────────────────────────

def get_pdh_pdl(df_d1: pd.DataFrame) -> tuple:
    """Returns (PDH, PDL) — previous day's high and low."""
    if len(df_d1) < 2:
        return 0.0, 0.0
    prev = df_d1.iloc[-2]
    return float(prev["high"]), float(prev["low"])


def pdh_pdl_aligned(direction: int, current_price: float,
                    pdh: float, pdl: float) -> bool:
    """True if PDH/PDL provides a clean target in direction of trade."""
    if pdh == 0 or pdl == 0:
        return False
    rng = pdh - pdl
    if rng <= 0:
        return False
    if direction == 1:
        return current_price < pdh and (pdh - current_price) > rng * 0.3
    if direction == -1:
        return current_price > pdl and (current_price - pdl) > rng * 0.3
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  CONFLUENCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def calculate_confluence_score(
    symbol: str,
    direction: int,
    ha_daily_bias: int,
    ha_h4_bias: int,
    h1_bos_choch: dict,
    ob: dict | None,
    fvg: dict | None,
    sweep_m15: bool,
    m5_bos: dict,
    rejection: bool,
    pdh: float, pdl: float,
    current_price: float,
) -> tuple:
    """
    Score the setup 0–10.
    Returns (score, breakdown_dict)
    """
    score = 0.0
    breakdown = {}

    # 1. Daily HA bias agrees (+2)
    if (direction == 1 and ha_daily_bias > 0) or \
       (direction == -1 and ha_daily_bias < 0):
        score += 2.0
        breakdown["daily_ha"] = 2.0
    else:
        breakdown["daily_ha"] = 0.0

    # 2. H4 HA bias agrees (+2)
    if (direction == 1 and ha_h4_bias > 0) or \
       (direction == -1 and ha_h4_bias < 0):
        score += 2.0
        breakdown["h4_ha"] = 2.0
    else:
        breakdown["h4_ha"] = 0.0

    # 3. H1 OB present and not mitigated (+2)
    if ob and not ob.get("mitigated", True):
        score += 2.0
        breakdown["ob"] = 2.0
    else:
        breakdown["ob"] = 0.0

    # 4. H1 FVG present and not filled (+1)
    if fvg and not fvg.get("filled", True):
        score += 1.0
        breakdown["fvg"] = 1.0
    else:
        breakdown["fvg"] = 0.0

    # 5. M15 liquidity sweep (+1)
    if sweep_m15:
        score += 1.0
        breakdown["sweep"] = 1.0
    else:
        breakdown["sweep"] = 0.0

    # 6. M5 BOS in direction (+1)
    m5_bos_ok = (direction == 1 and m5_bos.get("bos_bull")) or \
                (direction == -1 and m5_bos.get("bos_bear"))
    if m5_bos_ok:
        score += 1.0
        breakdown["m5_bos"] = 1.0
    else:
        breakdown["m5_bos"] = 0.0

    # 7. Rejection candle at OB/FVG zone (+1)
    if rejection:
        score += 1.0
        breakdown["rejection"] = 1.0
    else:
        breakdown["rejection"] = 0.0

    # 8. PDH/PDL aligned as target (+1) — bonus, not gate
    pdh_ok = pdh_pdl_aligned(direction, current_price, pdh, pdl)
    if pdh_ok:
        score += 0.5
        breakdown["pdh_pdl"] = 0.5
    else:
        breakdown["pdh_pdl"] = 0.0

    return score, breakdown


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN SIGNAL DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

def detect_scalp_signal(symbol: str, symbol_data: dict,
                        symbol_point: float,
                        spread_points: float,
                        in_session: bool = True) -> tuple:
    """
    Full APA/SMC signal detection with HA bias layer.

    symbol_data keys: d1, h4, h1, m15, m5, m1
    All must be regular OHLC DataFrames.
    HA conversion is done internally for D1 and H4.

    Returns: (direction_int, score, details_dict)
      direction_int: 1=long, -1=short, 0=no signal
    """
    d1  = symbol_data.get("d1")
    h4  = symbol_data.get("h4")
    h1  = symbol_data.get("h1")
    m15 = symbol_data.get("m15")
    m5  = symbol_data.get("m5")
    m1  = symbol_data.get("m1")

    if any(df is None or len(df) < 5 for df in [d1, h4, h1, m15, m5, m1]):
        return 0, 0.0, {"reason": "Insufficient data on one or more timeframes"}

    current_price = float(m5.iloc[-1]["close"])

    # ── GATE 1: HA Daily Bias ─────────────────────────────────────────────
    ha_daily = get_ha_bias(d1)
    if ha_daily == 0:
        return 0, 0.0, {"reason": "No clear Daily HA bias — market transitioning"}

    daily_direction = 1 if ha_daily > 0 else -1

    # ── GATE 2: HA H4 Bias — must agree with Daily ───────────────────────
    ha_h4 = get_ha_bias(h4)
    if ha_h4 == 0:
        return 0, 0.0, {"reason": "No clear H4 HA bias"}

    h4_direction = 1 if ha_h4 > 0 else -1

    if h4_direction != daily_direction:
        return 0, 0.0, {
            "reason": f"H4 HA ({h4_direction}) disagrees with Daily HA ({daily_direction})"
        }

    direction = daily_direction

    # ── SYMBOL DIRECTION OVERRIDE (Boom/Crash) ───────────────────────────
    if symbol in LONG_ONLY_SYMBOLS and direction == -1:
        return 0, 0.0, {"reason": f"{symbol} is LONG ONLY — skipping short"}
    if symbol in SHORT_ONLY_SYMBOLS and direction == 1:
        return 0, 0.0, {"reason": f"{symbol} is SHORT ONLY — skipping long"}

    # ── GATE 3: Market Condition ──────────────────────────────────────────
    condition = classify_market(h1, symbol_point)
    if condition in ("dead", "choppy"):
        return 0, 0.0, {"reason": f"Market condition: {condition} — standing down"}

    # ── GATE 4: H1 Structure ─────────────────────────────────────────────
    h1_structure = detect_bos_choch(h1, lookback=SWING_LOOKBACK)
    h1_confirms = (direction == 1 and (h1_structure["bos_bull"] or h1_structure["choch_bull"])) or \
                  (direction == -1 and (h1_structure["bos_bear"] or h1_structure["choch_bear"]))

    if not h1_confirms:
        return 0, 0.0, {"reason": "H1 structure does not confirm direction"}

    # ── GATE 5: H1 OB ────────────────────────────────────────────────────
    ob = find_order_block(h1, direction, OB_LOOKBACK)

    # ── H1 FVG ───────────────────────────────────────────────────────────
    fvg = find_fvg(h1, direction, FVG_LOOKBACK)

    # At least OB or FVG must exist
    if ob is None and fvg is None:
        return 0, 0.0, {"reason": "No H1 OB or FVG found — no entry zone"}

    # ── GATE 6: M15 Liquidity Sweep ──────────────────────────────────────
    sweep = detect_liquidity_sweep(m15, direction, lookback=8)

    # ── GATE 7: M5 BOS ───────────────────────────────────────────────────
    m5_bos = detect_bos_choch(m5, lookback=10)
    m5_confirms = (direction == 1 and m5_bos["bos_bull"]) or \
                  (direction == -1 and m5_bos["bos_bear"])

    if not m5_confirms:
        return 0, 0.0, {"reason": "M5 BOS not yet confirmed in direction"}

    # ── GATE 8: M1 CHoCH entry trigger ───────────────────────────────────
    m1_struct = detect_bos_choch(m1, lookback=CHOCH_LOOKBACK)
    m1_choch = (direction == 1 and (m1_struct["choch_bull"] or m1_struct["bos_bull"])) or \
               (direction == -1 and (m1_struct["choch_bear"] or m1_struct["bos_bear"]))

    if not m1_choch:
        return 0, 0.0, {"reason": "M1 CHoCH/BOS not confirmed — waiting for trigger"}

    # ── Rejection candle ─────────────────────────────────────────────────
    rejection = has_rejection_candle(m5, direction)

    # ── PDH/PDL ──────────────────────────────────────────────────────────
    pdh, pdl = get_pdh_pdl(d1)

    # ── HA Daily / H4 for scoring ─────────────────────────────────────────
    score, breakdown = calculate_confluence_score(
        symbol, direction,
        ha_daily, ha_h4,
        h1_structure, ob, fvg,
        sweep, m5_bos,
        rejection,
        pdh, pdl, current_price,
    )

    # ── Swing SL anchor from M1 ──────────────────────────────────────────
    swing_sl = (find_swing_low(m1, SWING_LOOKBACK)  if direction == 1
                else find_swing_high(m1, SWING_LOOKBACK))

    details = {
        "direction":       direction,
        "score":           score,
        "breakdown":       breakdown,
        "condition":       condition,
        "ha_daily":        ha_daily,
        "ha_h4":           ha_h4,
        "h1_bos_bull":     h1_structure["bos_bull"],
        "h1_bos_bear":     h1_structure["bos_bear"],
        "h1_choch_bull":   h1_structure["choch_bull"],
        "h1_choch_bear":   h1_structure["choch_bear"],
        "ob":              ob,
        "fvg":             fvg,
        "sweep_m15":       sweep,
        "m5_bos_bull":     m5_bos["bos_bull"],
        "m5_bos_bear":     m5_bos["bos_bear"],
        "m1_choch":        m1_choch,
        "rejection":       rejection,
        "pdh":             pdh,
        "pdl":             pdl,
        "swing_sl":        swing_sl,
        "current_price":   current_price,
    }

    if score < MIN_SIGNAL_SCORE_HALF:
        return 0, score, {"reason": f"Score too low: {score:.1f}/10"}

    return direction, score, details


# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME PROFILE INTEGRATION WRAPPER — called from detect_scalp_signal
# ─────────────────────────────────────────────────────────────────────────────

def apply_vp_to_apa_signal(details: dict, vp_stack: dict,
                            sym_point: float) -> tuple[float, dict]:
    """
    Score an APA signal against Volume Profile levels.
    Called after detect_scalp_signal passes all gates.

    Returns (vp_score_delta, vp_breakdown).
    Caller adds vp_score_delta to the base score.
    """
    from volume_profile import get_vp_confluence, get_vp_tp_target

    entry_price = details.get("current_price", 0.0)
    direction   = details.get("direction", 0)
    ob          = details.get("ob")
    fvg         = details.get("fvg")

    # Use OB/FVG midpoint as entry price if available
    if ob and not ob.get("mitigated"):
        entry_price = ob["mid"]
    elif fvg and not fvg.get("filled"):
        entry_price = fvg["mid"]

    vp_score, vp_bd = get_vp_confluence(
        vp_stack    = vp_stack,
        entry_price = entry_price,
        direction   = direction,
        sym_point   = sym_point,
    )

    # Nearest VP TP level — used in place_trade if closer than 1:3 RR
    vp_tp = get_vp_tp_target(vp_stack, entry_price, direction)

    return vp_score, {**vp_bd, "vp_tp_level": vp_tp}
