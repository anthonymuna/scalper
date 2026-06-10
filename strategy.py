"""
strategy.py — APA (Area, Pattern, Action) strategy — 5M Sniper Edition

Changes from v2:
  - 5M-only architecture (M5 bias + M5 momentum + M1 CHoCH)
  - Fixed AOL/engulfing detection (was checking wrong candle directions)
  - CHoCH lookback expanded from 5 → 20 M1 bars
  - FVG (Fair Value Gap) detection for precision entry zones
  - Killzone awareness passed into scoring
  - REQUIRE_CHOCH gate: score ≥ 5 only awarded when CHoCH confirmed
  - Cleaner signal scoring with explicit reasons per gate
"""

import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
#  Low-level indicators
# ─────────────────────────────────────────────────────────────────────────────

def get_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()


def get_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta    = df["close"].diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high       = df["high"]
    low        = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────────────────────
#  Swing High / Low — SL anchor
# ─────────────────────────────────────────────────────────────────────────────

def find_swing_low(df: pd.DataFrame, lookback: int = 15) -> float:
    """Lowest low in the last `lookback` bars — used as bull SL anchor."""
    return df["low"].iloc[-lookback:].min()


def find_swing_high(df: pd.DataFrame, lookback: int = 15) -> float:
    """Highest high in the last `lookback` bars — used as bear SL anchor."""
    return df["high"].iloc[-lookback:].max()


# ─────────────────────────────────────────────────────────────────────────────
#  AOL — Area of Liquidity  (FIXED engulfing logic)
# ─────────────────────────────────────────────────────────────────────────────

def identify_aol(df: pd.DataFrame) -> list:
    """
    Identify Areas of Liquidity via engulfing patterns (SMC definition).

    Bullish engulfing (demand zone):
      - Previous candle is BEARISH
      - Current candle is BULLISH
      - Current low < previous low  (sweeps below)
      - Current close > previous open  (full body engulf)

    Bearish engulfing (supply zone):
      - Previous candle is BULLISH
      - Current candle is BEARISH
      - Current high > previous high  (sweeps above)
      - Current close < previous open  (full body engulf)

    Returns list of dicts: {type, time, high, low}
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

        # ── Bullish engulfing: bearish prev, bullish curr ─────────────────
        if (prev_bear and curr_bull
                and curr["low"]   < prev["low"]
                and curr["close"] > prev["open"]):
            aols.append({
                "type": "bullish_engulfing",
                "time": curr["time"],
                "high": curr["high"],
                "low":  curr["low"],
            })

        # ── Bearish engulfing: bullish prev, bearish curr ─────────────────
        elif (prev_bull and curr_bear
                and curr["high"]  > prev["high"]
                and curr["close"] < prev["open"]):
            aols.append({
                "type": "bearish_engulfing",
                "time": curr["time"],
                "high": curr["high"],
                "low":  curr["low"],
            })

    return aols


# ─────────────────────────────────────────────────────────────────────────────
#  FVG — Fair Value Gap  (sniper entry zone)
# ─────────────────────────────────────────────────────────────────────────────

def find_fvg(df: pd.DataFrame, trend: str, lookback: int = 10) -> dict | None:
    """
    Detect the most recent Fair Value Gap in the last `lookback` bars.

    A bullish FVG exists between candles i-2 and i when:
        candle[i-2].high < candle[i].low  (gap between them)
        candle[i-1] is the impulse candle driving through

    A bearish FVG:
        candle[i-2].low > candle[i].high

    Returns dict: {type, top, bottom, time} or None.
    """
    window = df.iloc[-lookback:].reset_index(drop=True)

    # Scan newest-first so we get the most recent FVG
    for i in range(len(window) - 1, 1, -1):
        c0 = window.iloc[i - 2]   # candle before impulse
        c2 = window.iloc[i]       # candle after impulse

        if trend == "bullish":
            # Gap: high of c0 < low of c2
            if c0["high"] < c2["low"]:
                return {
                    "type":   "bullish_fvg",
                    "top":    float(c2["low"]),
                    "bottom": float(c0["high"]),
                    "mid":    float((c2["low"] + c0["high"]) / 2),
                    "time":   c2["time"],
                }
        else:  # bearish
            # Gap: low of c0 > high of c2
            if c0["low"] > c2["high"]:
                return {
                    "type":   "bearish_fvg",
                    "top":    float(c0["low"]),
                    "bottom": float(c2["high"]),
                    "mid":    float((c0["low"] + c2["high"]) / 2),
                    "time":   c2["time"],
                }

    return None


def price_in_fvg(current_price: float, fvg: dict) -> bool:
    """Return True if current price is inside the FVG zone."""
    if fvg is None:
        return False
    return fvg["bottom"] <= current_price <= fvg["top"]


# ─────────────────────────────────────────────────────────────────────────────
#  CHoCH — Change of Character  (EXPANDED lookback)
# ─────────────────────────────────────────────────────────────────────────────

def detect_choch(df: pd.DataFrame, trend: str, lookback: int = 20) -> dict:
    """
    Detect a Change of Character on M1 using the last `lookback` bars.

    Bullish CHoCH: price closes above the most recent swing high within
                   the lookback window — structural break to the upside.
    Bearish CHoCH: price closes below the most recent swing low.

    lookback=20 (was 5) gives meaningful structural context on M1.

    Returns: {"detected": bool, "choch_price": float | None}
    """
    if len(df) < lookback + 1:
        return {"detected": False, "choch_price": None}

    window = df.iloc[-(lookback + 1):-1]   # last N bars excluding latest

    if trend == "bullish":
        recent_high = window["high"].max()
        if df.iloc[-1]["close"] > recent_high:
            return {"detected": True, "choch_price": float(df.iloc[-1]["close"])}

    else:  # bearish
        recent_low = window["low"].min()
        if df.iloc[-1]["close"] < recent_low:
            return {"detected": True, "choch_price": float(df.iloc[-1]["close"])}

    return {"detected": False, "choch_price": None}


# ─────────────────────────────────────────────────────────────────────────────
#  Volatility Gate
# ─────────────────────────────────────────────────────────────────────────────

def check_volatility(df: pd.DataFrame, symbol_point: float,
                     atr_period: int = 14,
                     atr_min_points: float = 150,
                     atr_max_points: float = 2000) -> dict:
    atr_series = get_atr(df, atr_period)
    if atr_series.isna().all():
        return {"ok": False, "atr_points": 0, "reason": "ATR unavailable"}

    atr_price  = atr_series.iloc[-1]
    atr_points = atr_price / symbol_point if symbol_point else 0

    if atr_points < atr_min_points:
        return {"ok": False, "atr_points": round(atr_points),
                "reason": f"ATR too low ({atr_points:.0f}pts) — dead market"}

    if atr_points > atr_max_points:
        return {"ok": False, "atr_points": round(atr_points),
                "reason": f"ATR too high ({atr_points:.0f}pts) — news spike"}

    return {"ok": True, "atr_points": round(atr_points), "reason": "OK"}


# ─────────────────────────────────────────────────────────────────────────────
#  RSI Slope — momentum direction
# ─────────────────────────────────────────────────────────────────────────────

def rsi_slope_ok(rsi_series: pd.Series, trend: str, lookback: int = 3) -> bool:
    if len(rsi_series) < lookback + 1:
        return True
    recent = rsi_series.iloc[-lookback:]
    slope  = (recent.iloc[-1] - recent.iloc[0]) / lookback
    return slope > 0 if trend == "bullish" else slope < 0


# ─────────────────────────────────────────────────────────────────────────────
#  Main signal detector — 5M Sniper
# ─────────────────────────────────────────────────────────────────────────────

def detect_scalp_signal(symbol_data: dict, symbol_point: float,
                        spread_points: float,
                        in_killzone: bool = False) -> tuple:
    """
    5M-only sniper signal detection.

    Flow:
      1. Volatility gate (M5 ATR)
      2. Spread-to-ATR ratio guard
      3. M5 Trend bias  (EMA 50 vs EMA 200 — wider view on same TF)
      4. M5 Momentum    (EMA 9/21 cross + RSI zone + slope)
      5. M1 CHoCH       (structural confirmation — required for score ≥ 5)
      6. FVG presence   (bonus point for precision entry zone)
      7. Killzone bonus (extra point if in London/NY open window)

    Score max: 8
    Threshold: MIN_SIGNAL_STRENGTH (default 5, CHoCH required)

    Returns: (direction, signal_strength, details_dict)
    """
    from config import (ATR_PERIOD, ATR_MIN_POINTS, ATR_MAX_POINTS,
                        FVG_LOOKBACK_BARS, REQUIRE_CHOCH, MIN_SIGNAL_STRENGTH)

    m5 = symbol_data.get("m5")
    m1 = symbol_data.get("m1")

    if m5 is None or m1 is None:
        return None, 0, {"reason": "Missing OHLCV data"}

    # ── 1. Volatility gate ─────────────────────────────────────────────────
    vol = check_volatility(m5, symbol_point, ATR_PERIOD,
                           ATR_MIN_POINTS, ATR_MAX_POINTS)
    if not vol["ok"]:
        return None, 0, {"reason": vol["reason"], "atr": vol["atr_points"]}

    # ── 2. Spread-to-ATR guard ─────────────────────────────────────────────
    if vol["atr_points"] > 0 and (spread_points / vol["atr_points"]) > 0.25:
        return None, 0, {
            "reason": f"Spread too wide vs ATR "
                      f"({spread_points:.0f}/{vol['atr_points']:.0f}pts)"
        }

    # ── 3. M5 Trend bias  (EMA 50 vs EMA 200) ─────────────────────────────
    # Using wider EMAs on M5 gives the same "higher timeframe" context
    # that M15 EMAs provided before, but stays on a single timeframe.
    m5 = m5.copy()
    m5["ema50"]  = get_ema(m5, 50)
    m5["ema200"] = get_ema(m5, 200)
    m5["ema9"]   = get_ema(m5, 9)
    m5["ema21"]  = get_ema(m5, 21)
    m5["rsi"]    = get_rsi(m5, 14)

    latest = m5.iloc[-1]
    prev   = m5.iloc[-2]

    m5_bull = (latest["ema50"] > latest["ema200"] and
               latest["close"] > latest["ema50"])
    m5_bear = (latest["ema50"] < latest["ema200"] and
               latest["close"] < latest["ema50"])

    if not m5_bull and not m5_bear:
        return None, 0, {"reason": "No M5 trend — price between EMA50/200"}

    trend = "bullish" if m5_bull else "bearish"

    # ── 4. M5 Momentum scoring ─────────────────────────────────────────────
    score = 0

    if trend == "bullish":
        if latest["ema9"] > latest["ema21"]:           score += 1
        if prev["ema9"] <= prev["ema21"] and latest["ema9"] > latest["ema21"]:
            score += 1   # fresh cross — stronger signal
        if 45 < latest["rsi"] < 68:                    score += 1
        if latest["close"] > latest["ema9"]:           score += 1
        if rsi_slope_ok(m5["rsi"], trend):             score += 1
    else:
        if latest["ema9"] < latest["ema21"]:           score += 1
        if prev["ema9"] >= prev["ema21"] and latest["ema9"] < latest["ema21"]:
            score += 1
        if 32 < latest["rsi"] < 55:                    score += 1
        if latest["close"] < latest["ema9"]:           score += 1
        if rsi_slope_ok(m5["rsi"], trend):             score += 1

    if score < 2:
        return None, score, {"reason": f"Weak M5 momentum ({score}/5)"}

    # ── 5. M1 CHoCH — structural confirmation ─────────────────────────────
    choch = detect_choch(m1, trend, lookback=20)
    choch_bonus = 0
    if choch["detected"]:
        choch_bonus = 1
        score += 1
    elif REQUIRE_CHOCH:
        # Without CHoCH we can't reach MIN_SIGNAL_STRENGTH=5 anyway,
        # but make it explicit so the reason is clear in logs.
        return None, score, {
            "reason": f"No M1 CHoCH confirmation (score {score}/5 without it)"
        }

    # ── 6. FVG bonus — precision entry zone ────────────────────────────────
    fvg = find_fvg(m5, trend, FVG_LOOKBACK_BARS)
    fvg_bonus = 0
    current_price = float(latest["close"])

    if fvg is not None:
        fvg_bonus = 1
        score += 1   # FVG exists — worth targeting
        # Note: actual FVG-tap entry is handled in main.py place_trade

    # ── 7. Killzone bonus ──────────────────────────────────────────────────
    kz_bonus = 0
    if in_killzone:
        kz_bonus = 1
        score += 1

    # ── Swing SL anchor ───────────────────────────────────────────────────
    if trend == "bullish":
        swing_sl_price = find_swing_low(m1, lookback=15)
    else:
        swing_sl_price = find_swing_high(m1, lookback=15)

    # ── Build details ──────────────────────────────────────────────────────
    details = {
        "trend":          trend,
        "score":          score,
        "m5_ema50":       round(float(latest["ema50"]),  2),
        "m5_ema200":      round(float(latest["ema200"]), 2),
        "m5_ema9":        round(float(latest["ema9"]),   2),
        "m5_ema21":       round(float(latest["ema21"]),  2),
        "m5_rsi":         round(float(latest["rsi"]),    1),
        "m5_ema_cross":   (latest["ema9"] > latest["ema21"]) if trend == "bullish"
                          else (latest["ema9"] < latest["ema21"]),
        "choch":          choch["detected"],
        "choch_price":    choch["choch_price"],
        "fvg":            fvg,
        "fvg_bonus":      bool(fvg_bonus),
        "killzone_bonus": bool(kz_bonus),
        "swing_sl_price": round(float(swing_sl_price), 2),
        "atr_points":     vol["atr_points"],
    }

    if score >= MIN_SIGNAL_STRENGTH:
        return trend, score, details

    return None, score, {"reason": f"Signal too weak ({score}/8)"}


# ─────────────────────────────────────────────────────────────────────────────
#  Timeframe Coordination helpers  (AOL alignment — unchanged API)
# ─────────────────────────────────────────────────────────────────────────────

def detect_market_shift(df: pd.DataFrame, shift_point: float,
                        current_trend: str) -> dict:
    for i in range(len(df)):
        curr = df.iloc[i]
        if current_trend == "bullish" and curr["close"] < shift_point:
            return {"shift_detected": True, "direction": "bearish",
                    "time": curr["time"]}
        elif current_trend == "bearish" and curr["close"] > shift_point:
            return {"shift_detected": True, "direction": "bullish",
                    "time": curr["time"]}
    return {"shift_detected": False, "direction": None}


def detect_liquidity_engineering(df: pd.DataFrame, level: float,
                                 current_trend: str) -> dict:
    le_detected = False
    fmd         = None
    choch_det   = False
    choch_price = None

    for i in range(1, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]

        if current_trend == "bullish":
            if curr["low"] < level:
                fmd = curr["low"] if fmd is None else min(fmd, curr["low"])
                if curr["close"] > prev["high"]:
                    choch_det   = True
                    choch_price = curr["close"]
                    le_detected = True
                    break
        elif current_trend == "bearish":
            if curr["high"] > level:
                fmd = curr["high"] if fmd is None else max(fmd, curr["high"])
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
            "reason": "No Shift on Situational TF"}
