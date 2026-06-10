"""
strategy.py — APA (Area, Pattern, Action) strategy with sniper entry logic.

Improvements over v1:
  - ATR-based volatility gating (no trades in dead or news-spike markets)
  - Swing High / Low detection for precise SL placement
  - CHoCH (Change of Character) M1 confirmation before entry
  - RSI slope / divergence filter
  - Spread-to-ATR ratio guard
  - Tighter signal scoring (max 7, threshold 4)
"""

import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
#  Low-level indicators
# ─────────────────────────────────────────────────────────────────────────────

def get_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()


def get_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"]
    low  = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────────────────────
#  Swing High / Low — precise SL anchor
# ─────────────────────────────────────────────────────────────────────────────

def find_swing_low(df: pd.DataFrame, lookback: int = 10) -> float:
    """Return the lowest low in the last `lookback` bars (for bull SL)."""
    return df["low"].iloc[-lookback:].min()


def find_swing_high(df: pd.DataFrame, lookback: int = 10) -> float:
    """Return the highest high in the last `lookback` bars (for bear SL)."""
    return df["high"].iloc[-lookback:].max()


# ─────────────────────────────────────────────────────────────────────────────
#  AOL — Area of Liquidity (engulfing zones)
# ─────────────────────────────────────────────────────────────────────────────

def identify_aol(df: pd.DataFrame) -> list:
    """
    Identify Areas of Liquidity via engulfing patterns.
    Returns list of dicts with keys: type, time, high, low.
    """
    aols = []
    if len(df) < 2:
        return aols

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        prev_bull = prev["close"] > prev["open"]
        prev_bear = prev["close"] < prev["open"]
        curr_bull = curr["close"] > curr["open"]
        curr_bear = curr["close"] < curr["open"]

        # Bullish engulfing: sweeps below prev low, closes above prev close
        if prev_bull and curr_bull and curr["low"] < prev["low"] and curr["close"] > prev["close"]:
            aols.append({"type": "bullish_engulfing", "time": curr["time"],
                         "high": curr["high"], "low": curr["low"]})

        # Bearish engulfing: sweeps above prev high, closes below prev close
        elif prev_bear and curr_bear and curr["high"] > prev["high"] and curr["close"] < prev["close"]:
            aols.append({"type": "bearish_engulfing", "time": curr["time"],
                         "high": curr["high"], "low": curr["low"]})

    return aols


# ─────────────────────────────────────────────────────────────────────────────
#  CHoCH — Change of Character (M1 entry trigger)
# ─────────────────────────────────────────────────────────────────────────────

def detect_choch(df: pd.DataFrame, trend: str) -> dict:
    """
    Detect a Change of Character on the given df (should be M1).

    For a BULLISH setup we want:
      - A down-leg (series of lower lows) followed by a candle that closes
        ABOVE the most recent swing high — structural break to the upside.

    For a BEARISH setup we want:
      - An up-leg followed by a candle closing BELOW the most recent swing low.

    Returns: {"detected": bool, "choch_price": float | None}
    """
    if len(df) < 5:
        return {"detected": False, "choch_price": None}

    last = df.iloc[-5:]   # look at last 5 M1 candles

    if trend == "bullish":
        # Find the recent swing high within the last 5 bars (excluding the last candle)
        recent_high = last["high"].iloc[:-1].max()
        # CHoCH: the latest candle closes above that swing high
        if df.iloc[-1]["close"] > recent_high:
            return {"detected": True, "choch_price": df.iloc[-1]["close"]}

    else:  # bearish
        recent_low = last["low"].iloc[:-1].min()
        if df.iloc[-1]["close"] < recent_low:
            return {"detected": True, "choch_price": df.iloc[-1]["close"]}

    return {"detected": False, "choch_price": None}


# ─────────────────────────────────────────────────────────────────────────────
#  Volatility Gate — ATR-based
# ─────────────────────────────────────────────────────────────────────────────

def check_volatility(df: pd.DataFrame, symbol_point: float,
                     atr_period: int = 14,
                     atr_min_points: float = 200,
                     atr_max_points: float = 2500) -> dict:
    """
    Calculate ATR on the given df and check if market volatility is
    in a tradeable range.

    Returns: {"ok": bool, "atr_points": float, "reason": str}
    """
    atr_series = get_atr(df, atr_period)
    if atr_series.isna().all():
        return {"ok": False, "atr_points": 0, "reason": "ATR unavailable"}

    atr_price = atr_series.iloc[-1]
    atr_points = atr_price / symbol_point if symbol_point else 0

    if atr_points < atr_min_points:
        return {"ok": False, "atr_points": round(atr_points),
                "reason": f"ATR too low ({atr_points:.0f} pts) — dead market"}

    if atr_points > atr_max_points:
        return {"ok": False, "atr_points": round(atr_points),
                "reason": f"ATR too high ({atr_points:.0f} pts) — news spike"}

    return {"ok": True, "atr_points": round(atr_points), "reason": "OK"}


# ─────────────────────────────────────────────────────────────────────────────
#  RSI Slope — momentum direction confirmation
# ─────────────────────────────────────────────────────────────────────────────

def rsi_slope_ok(rsi_series: pd.Series, trend: str, lookback: int = 3) -> bool:
    """
    Check that RSI is moving in the direction of the trade over the
    last `lookback` bars. Avoids fading momentum.
    """
    if len(rsi_series) < lookback + 1:
        return True   # can't confirm, be permissive
    recent = rsi_series.iloc[-lookback:]
    slope = (recent.iloc[-1] - recent.iloc[0]) / lookback
    if trend == "bullish":
        return slope > 0    # RSI rising
    else:
        return slope < 0    # RSI falling


# ─────────────────────────────────────────────────────────────────────────────
#  Main signal detector
# ─────────────────────────────────────────────────────────────────────────────

def detect_scalp_signal(symbol_data: dict, symbol_point: float,
                        spread_points: float) -> tuple:
    """
    Multi-timeframe sniper signal detection (M15 → M5 → M1).

    Parameters
    ----------
    symbol_data   : {"m15": DataFrame, "m5": DataFrame, "m1": DataFrame}
    symbol_point  : symbol.point from MT5
    spread_points : current spread in points

    Returns
    -------
    (direction, signal_strength, details_dict)
    direction      : "bullish" | "bearish" | None
    signal_strength: int 0-7
    details        : dict with debug info
    """
    m15 = symbol_data.get("m15")
    m5  = symbol_data.get("m5")
    m1  = symbol_data.get("m1")

    if m15 is None or m5 is None or m1 is None:
        return None, 0, {"reason": "Missing OHLCV data"}

    # ── 1. Volatility gate (on M5) ─────────────────────────────────────────
    from config import ATR_PERIOD, ATR_MIN_POINTS, ATR_MAX_POINTS
    vol = check_volatility(m5, symbol_point, ATR_PERIOD,
                           ATR_MIN_POINTS, ATR_MAX_POINTS)
    if not vol["ok"]:
        return None, 0, {"reason": vol["reason"], "atr": vol["atr_points"]}

    # ── 2. Spread-to-ATR ratio guard ───────────────────────────────────────
    # Reject if spread is more than 25 % of the current ATR
    if vol["atr_points"] > 0 and (spread_points / vol["atr_points"]) > 0.25:
        return None, 0, {
            "reason": f"Spread too wide relative to ATR "
                      f"({spread_points:.0f}/{vol['atr_points']:.0f} pts)"
        }

    # ── 3. M15 Trend bias (EMA 20 vs EMA 50) ──────────────────────────────
    m15["ema20"] = get_ema(m15, 20)
    m15["ema50"] = get_ema(m15, 50)
    latest_m15   = m15.iloc[-1]

    m15_bull = (latest_m15["ema20"] > latest_m15["ema50"] and
                latest_m15["close"] > latest_m15["ema20"])
    m15_bear = (latest_m15["ema20"] < latest_m15["ema50"] and
                latest_m15["close"] < latest_m15["ema20"])

    if not m15_bull and not m15_bear:
        return None, 0, {"reason": "No M15 trend alignment"}

    trend = "bullish" if m15_bull else "bearish"

    # ── 4. M5 Momentum (EMA cross + RSI zone + RSI slope) ─────────────────
    m5["ema9"]  = get_ema(m5, 9)
    m5["ema21"] = get_ema(m5, 21)
    m5["rsi"]   = get_rsi(m5, 14)

    latest_m5 = m5.iloc[-1]
    prev_m5   = m5.iloc[-2]
    score = 0

    if trend == "bullish":
        # EMA alignment
        if latest_m5["ema9"] > latest_m5["ema21"]:
            score += 1
        # Fresh cross (strongest signal)
        if prev_m5["ema9"] <= prev_m5["ema21"] and latest_m5["ema9"] > latest_m5["ema21"]:
            score += 2
        # RSI in bullish momentum zone (not overbought)
        if 45 < latest_m5["rsi"] < 68:
            score += 1
        # Price above EMA9 (immediate momentum)
        if latest_m5["close"] > latest_m5["ema9"]:
            score += 1
        # RSI slope rising
        if rsi_slope_ok(m5["rsi"], trend):
            score += 1
    else:
        if latest_m5["ema9"] < latest_m5["ema21"]:
            score += 1
        if prev_m5["ema9"] >= prev_m5["ema21"] and latest_m5["ema9"] < latest_m5["ema21"]:
            score += 2
        # RSI in bearish zone (not oversold)
        if 32 < latest_m5["rsi"] < 55:
            score += 1
        if latest_m5["close"] < latest_m5["ema9"]:
            score += 1
        if rsi_slope_ok(m5["rsi"], trend):
            score += 1

    if score < 2:
        return None, score, {"reason": f"Weak M5 momentum ({score}/6)"}

    # ── 5. M1 CHoCH entry confirmation ─────────────────────────────────────
    choch = detect_choch(m1, trend)
    if choch["detected"]:
        score += 1   # bonus point for structural confirmation

    # ── 6. Swing SL level ──────────────────────────────────────────────────
    if trend == "bullish":
        swing_sl_price = find_swing_low(m1, lookback=10)
    else:
        swing_sl_price = find_swing_high(m1, lookback=10)

    # ── Build details dict ─────────────────────────────────────────────────
    details = {
        "trend":          trend,
        "score":          score,
        "m15_ema20":      round(float(latest_m15["ema20"]), 2),
        "m15_ema50":      round(float(latest_m15["ema50"]), 2),
        "m5_rsi":         round(float(latest_m5["rsi"]), 1),
        "m5_ema_cross":   (latest_m5["ema9"] > latest_m5["ema21"])
                          if trend == "bullish"
                          else (latest_m5["ema9"] < latest_m5["ema21"]),
        "choch":          choch["detected"],
        "choch_price":    choch["choch_price"],
        "swing_sl_price": round(float(swing_sl_price), 2),
        "atr_points":     vol["atr_points"],
    }

    if score >= 4:   # sniper threshold
        return trend, score, details

    return None, score, {"reason": f"Signal too weak ({score}/7)"}


# ─────────────────────────────────────────────────────────────────────────────
#  Timeframe Coordination (AOL alignment check) — unchanged API
# ─────────────────────────────────────────────────────────────────────────────

def detect_market_shift(df: pd.DataFrame, shift_point: float,
                        current_trend: str) -> dict:
    for i in range(len(df)):
        curr = df.iloc[i]
        if current_trend == "bullish" and curr["close"] < shift_point:
            return {"shift_detected": True, "direction": "bearish", "time": curr["time"]}
        elif current_trend == "bearish" and curr["close"] > shift_point:
            return {"shift_detected": True, "direction": "bullish", "time": curr["time"]}
    return {"shift_detected": False, "direction": None}


def detect_liquidity_engineering(df: pd.DataFrame, level: float,
                                 current_trend: str) -> dict:
    le_detected  = False
    fmd          = None
    choch_det    = False
    choch_price  = None

    for i in range(1, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]

        if current_trend == "bullish":
            if curr["low"] < level:
                if fmd is None or curr["low"] < fmd:
                    fmd = curr["low"]
                if curr["close"] > prev["high"]:
                    choch_det   = True
                    choch_price = curr["close"]
                    le_detected = True
                    break
        elif current_trend == "bearish":
            if curr["high"] > level:
                if fmd is None or curr["high"] > fmd:
                    fmd = curr["high"]
                if curr["close"] < prev["low"]:
                    choch_det   = True
                    choch_price = curr["close"]
                    le_detected = True
                    break

    if le_detected:
        return {"le_detected": True, "fmd": fmd, "choch": choch_det,
                "choch_price": choch_price}
    return {"le_detected": False, "fmd": None, "choch": False,
            "choch_price": None}


def analyze_timeframe_coordination(constant_df: pd.DataFrame,
                                   situational_df: pd.DataFrame,
                                   current_trend: str) -> dict:
    constant_aols = identify_aol(constant_df)
    if not constant_aols:
        return {"aligned": False, "bias": "neutral",
                "reason": "No AOL on Constant TF"}

    latest_aol  = constant_aols[-1]
    shift_point = (latest_aol["low"]  if current_trend == "bullish"
                   else latest_aol["high"])

    shift = detect_market_shift(situational_df, shift_point, current_trend)
    if shift["shift_detected"]:
        return {"aligned": True, "bias": shift["direction"],
                "shift_time": shift["time"], "aol_level": shift_point}

    return {"aligned": False, "bias": "neutral",
            "reason": "No Shift Detected on Situational TF"}
