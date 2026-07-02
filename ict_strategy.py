"""
ict_strategy.py — ICT Engine for NGAO Scalper v4.0
====================================================
Implements all core ICT concepts independently from APA engine.
Either engine can trigger a trade — whichever fires first wins.

Concepts implemented:
  1. IPDA Data Ranges       — 20/40/60-day institutional reference levels
  2. Power of 3 (AMD)       — Accumulation, Manipulation, Distribution phases
  3. ICT Killzones          — London, NY AM, Silver Bullet, NY PM windows
  4. Optimal Trade Entry    — Fibonacci 0.62–0.79 zone, 0.705 precise level
  5. Displacement           — Aggressive institutional candle detection
  6. Breaker Blocks         — Failed OB that flips polarity (supply→demand)
  7. Mitigation Blocks      — Last opposing candle before structural move
  8. Silver Bullet          — FVG entry during 3 precise 1-hour windows

Signal flow:
  Daily HA bias (from APA engine)
  → IPDA reference level nearest in bias direction = draw on liquidity
  → AMD phase identification (accumulation / manipulation / distribution)
  → Killzone timing check
  → OTE zone calculation on displacement leg
  → Breaker/Mitigation block at OTE zone = maximum confluence
  → Score 0–10, min 7 to fire

Zero indicators. Pure price, time, and Fibonacci math.
"""

from __future__ import annotations
import pandas as pd
import numpy as np
from datetime import datetime, timezone, time as dtime


# ─────────────────────────────────────────────────────────────────────────────
#  ICT KILLZONE WINDOWS  (UTC)
#  Mapped from NY/ET times to UTC for server compatibility
# ─────────────────────────────────────────────────────────────────────────────

# London Open Killzone:     02:00–05:00 ET  = 07:00–10:00 UTC
# NY AM Killzone:           07:00–10:00 ET  = 12:00–15:00 UTC
# Silver Bullet Window 1:   03:00–04:00 ET  = 08:00–09:00 UTC
# Silver Bullet Window 2:   10:00–11:00 ET  = 15:00–16:00 UTC
# Silver Bullet Window 3:   14:00–15:00 ET  = 19:00–20:00 UTC
# NY PM / Close:            14:00–16:00 ET  = 19:00–21:00 UTC

ICT_KILLZONES_UTC = {
    "london_open":    (7,  10),   # 07:00–10:00 UTC
    "ny_am":          (12, 15),   # 12:00–15:00 UTC
    "silver_bullet_1":(8,  9),    # 08:00–09:00 UTC (London SB)
    "silver_bullet_2":(15, 16),   # 15:00–16:00 UTC (NY AM SB)
    "silver_bullet_3":(19, 20),   # 19:00–20:00 UTC (NY PM SB)
    "ny_pm":          (19, 21),   # 19:00–21:00 UTC
}

# NY Midnight open (reference for AMD daily open) = 05:00 UTC
NY_MIDNIGHT_OPEN_UTC = 5

# OTE Fibonacci levels
OTE_LOW  = 0.62
OTE_MID  = 0.705   # Precise algorithmic level
OTE_HIGH = 0.79

# IPDA lookback days
IPDA_RANGES = [20, 40, 60]

# Displacement: minimum candle body as fraction of total range
DISPLACEMENT_BODY_RATIO = 0.6

# Minimum displacement size (bars) — consecutive strong candles
DISPLACEMENT_MIN_BARS = 1

# AMD accumulation: range defines as % of daily range
AMD_ACCUM_MAX_RANGE_PCT = 0.30   # accumulation zone ≤ 30% of daily range

# Breaker: failed OB lookback
BREAKER_LOOKBACK = 10

# ICT signal scoring weights
ICT_WEIGHTS = {
    "killzone":          2.0,
    "silver_bullet":     1.0,   # bonus on top of killzone
    "ipda_draw":         2.0,
    "amd_distribution":  2.0,
    "ote_zone":          1.5,
    "breaker_block":     1.0,
    "mitigation_block":  0.5,
    "displacement_fvg":  1.0,
}
ICT_MAX_SCORE = sum(ICT_WEIGHTS.values())   # 11.0
ICT_MIN_SCORE = 7.0
ICT_MIN_SCORE_HALF = 5.0


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER: UTC HOUR
# ─────────────────────────────────────────────────────────────────────────────

def utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def utc_minute() -> int:
    return datetime.now(timezone.utc).minute


# ─────────────────────────────────────────────────────────────────────────────
#  1. ICT KILLZONE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def get_active_killzone() -> tuple[str | None, bool]:
    """
    Returns (killzone_name, is_silver_bullet).
    silver_bullet is True only during the three 1-hour SB windows.
    """
    hour = utc_hour()

    # Silver Bullet windows (subset of killzones)
    for name in ("silver_bullet_1", "silver_bullet_2", "silver_bullet_3"):
        start, end = ICT_KILLZONES_UTC[name]
        if start <= hour < end:
            return name, True

    # Broader killzones
    for name in ("london_open", "ny_am", "ny_pm"):
        start, end = ICT_KILLZONES_UTC[name]
        if start <= hour < end:
            return name, False

    return None, False


def in_any_killzone() -> bool:
    kz, _ = get_active_killzone()
    return kz is not None


# ─────────────────────────────────────────────────────────────────────────────
#  2. IPDA DATA RANGES — 20/40/60-day reference levels
# ─────────────────────────────────────────────────────────────────────────────

def get_ipda_levels(df_d1: pd.DataFrame) -> dict:
    """
    Calculate IPDA 20, 40, 60-day highs and lows.
    These are institutional reference levels — price is drawn to them.

    Returns:
      {
        20: {"high": float, "low": float},
        40: {"high": float, "low": float},
        60: {"high": float, "low": float},
        "nearest_draw": {"direction": 1/-1, "level": float, "range": 20/40/60}
      }
    """
    result = {}
    current_price = float(df_d1.iloc[-1]["close"])

    for days in IPDA_RANGES:
        if len(df_d1) < days + 1:
            continue
        window = df_d1.iloc[-(days + 1):-1]
        result[days] = {
            "high": float(window["high"].max()),
            "low":  float(window["low"].min()),
        }

    # Find the nearest draw on liquidity in each direction
    nearest_bull = None   # nearest high above price
    nearest_bear = None   # nearest low below price

    for days in IPDA_RANGES:
        if days not in result:
            continue
        h = result[days]["high"]
        l = result[days]["low"]

        # Nearest above price (bullish draw — buy-side liquidity)
        if h > current_price:
            if nearest_bull is None or h < nearest_bull["level"]:
                nearest_bull = {"level": h, "range": days, "direction": 1}

        # Nearest below price (bearish draw — sell-side liquidity)
        if l < current_price:
            if nearest_bear is None or l > nearest_bear["level"]:
                nearest_bear = {"level": l, "range": days, "direction": -1}

    result["nearest_bull_draw"] = nearest_bull
    result["nearest_bear_draw"] = nearest_bear
    return result


def ipda_aligned_with_bias(ipda: dict, direction: int) -> bool:
    """True if the nearest IPDA draw is in the direction of bias."""
    key = "nearest_bull_draw" if direction == 1 else "nearest_bear_draw"
    draw = ipda.get(key)
    return draw is not None


# ─────────────────────────────────────────────────────────────────────────────
#  3. DISPLACEMENT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def find_displacement(df: pd.DataFrame, direction: int,
                      lookback: int = 10) -> dict | None:
    """
    Find the most recent genuine displacement move.

    Displacement = aggressive, one-sided institutional move that:
      - Creates large-bodied candles (body > 60% of total range)
      - Moves in one direction with minimal opposing wicks
      - Leaves at least one FVG (three-candle gap)

    Returns: {start_price, end_price, fvg_top, fvg_bottom, bar_index} or None
    """
    if len(df) < lookback + 3:
        return None

    window = df.iloc[-lookback:].reset_index(drop=True)

    for i in range(1, len(window) - 1):
        o = window.loc[i, "open"]
        h = window.loc[i, "high"]
        l = window.loc[i, "low"]
        c = window.loc[i, "close"]

        total_range = h - l
        if total_range == 0:
            continue

        body = abs(c - o)
        body_ratio = body / total_range

        # Check displacement criteria
        if body_ratio < DISPLACEMENT_BODY_RATIO:
            continue

        is_bull_disp = (c > o) and direction == 1
        is_bear_disp = (c < o) and direction == -1

        if not (is_bull_disp or is_bear_disp):
            continue

        # Check for FVG created by this displacement
        if i >= 2:
            c_prev = window.iloc[i - 2]
            c_curr = window.iloc[i]

            if direction == 1 and c_prev["high"] < c_curr["low"]:
                return {
                    "start_price": float(o),
                    "end_price":   float(c),
                    "swing_low":   float(l),
                    "swing_high":  float(h),
                    "fvg_bottom":  float(c_prev["high"]),
                    "fvg_top":     float(c_curr["low"]),
                    "fvg_mid":     float((c_prev["high"] + c_curr["low"]) / 2),
                    "bar_index":   i,
                }
            elif direction == -1 and c_prev["low"] > c_curr["high"]:
                return {
                    "start_price": float(o),
                    "end_price":   float(c),
                    "swing_low":   float(l),
                    "swing_high":  float(h),
                    "fvg_bottom":  float(c_curr["high"]),
                    "fvg_top":     float(c_prev["low"]),
                    "fvg_mid":     float((c_curr["high"] + c_prev["low"]) / 2),
                    "bar_index":   i,
                }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  4. OPTIMAL TRADE ENTRY (OTE) — Fibonacci 0.62–0.79
# ─────────────────────────────────────────────────────────────────────────────

def calculate_ote_zone(swing_low: float, swing_high: float,
                       direction: int) -> dict:
    """
    Calculate OTE Fibonacci zone from a swing's body-to-body range.

    Bullish OTE: draw from swing_low to swing_high
      OTE zone = 62%–79% retracement = discount zone (buy here)
      Precise level = 70.5% (0.705)

    Bearish OTE: draw from swing_high to swing_low
      OTE zone = 62%–79% retracement = premium zone (sell here)

    Returns: {ote_low, ote_705, ote_high, in_zone_fn}
    """
    rng = swing_high - swing_low

    if direction == 1:
        # Bullish: OTE is in the discount (lower) portion of the range
        ote_high = swing_high - rng * OTE_LOW    # 62% from top = near low
        ote_705  = swing_high - rng * OTE_MID    # 70.5% precise
        ote_low  = swing_high - rng * OTE_HIGH   # 79% from top = deepest

    else:
        # Bearish: OTE is in the premium (upper) portion of the range
        ote_low  = swing_low + rng * OTE_LOW     # 62% from bottom = near high
        ote_705  = swing_low + rng * OTE_MID     # 70.5% precise
        ote_high = swing_low + rng * OTE_HIGH    # 79% from bottom = deepest

    return {
        "ote_low":  float(min(ote_low, ote_high)),
        "ote_705":  float(ote_705),
        "ote_high": float(max(ote_low, ote_high)),
    }


def price_in_ote(current_price: float, ote: dict) -> bool:
    """True if current price is inside the OTE zone."""
    return ote["ote_low"] <= current_price <= ote["ote_high"]


# ─────────────────────────────────────────────────────────────────────────────
#  5. BREAKER BLOCK DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def find_breaker_block(df: pd.DataFrame, direction: int,
                       lookback: int = BREAKER_LOOKBACK) -> dict | None:
    """
    Breaker Block = a failed Order Block that flips polarity.

    Bullish Breaker: a bearish OB (supply zone) that was broken above
    by price — now acts as support (demand) on return.
    Price broke through it bullishly, came back, and should hold.

    Bearish Breaker: a bullish OB (demand zone) broken below
    — now acts as resistance (supply) on return.

    Returns: {high, low, mid, direction} or None
    """
    if len(df) < lookback + 3:
        return None

    current_price = float(df.iloc[-1]["close"])
    window = df.iloc[-(lookback + 3):].reset_index(drop=True)

    for i in range(1, len(window) - 2):
        o = window.loc[i, "open"]
        c = window.loc[i, "close"]
        h = window.loc[i, "high"]
        l = window.loc[i, "low"]

        # Bullish breaker: a previously bearish candle that price broke above
        if direction == 1:
            was_bearish = c < o
            if was_bearish:
                # Was price at some point above this candle's high after it?
                future_highs = window.loc[i+1:, "close"]
                broke_above  = (future_highs > h).any()

                if broke_above:
                    # Is current price now returning to this zone?
                    in_zone = l <= current_price <= h
                    if in_zone:
                        return {
                            "high": float(h), "low": float(l),
                            "mid":  float((h + l) / 2.0),
                            "type": "bullish_breaker"
                        }

        # Bearish breaker: a previously bullish candle that price broke below
        elif direction == -1:
            was_bullish = c > o
            if was_bullish:
                future_lows = window.loc[i+1:, "close"]
                broke_below = (future_lows < l).any()

                if broke_below:
                    in_zone = l <= current_price <= h
                    if in_zone:
                        return {
                            "high": float(h), "low": float(l),
                            "mid":  float((h + l) / 2.0),
                            "type": "bearish_breaker"
                        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  6. MITIGATION BLOCK DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def find_mitigation_block(df: pd.DataFrame, direction: int,
                          lookback: int = 10) -> dict | None:
    """
    Mitigation Block = the last opposing candle before a significant move.
    Price often returns to this area to "mitigate" the orders left there.

    Bullish Mitigation: last bearish candle before a strong bullish impulse.
    Price returns to this candle's body zone — institutions fill remaining orders.

    Bearish Mitigation: last bullish candle before a strong bearish impulse.

    Similar to OB but the key difference:
      - OB is the LAST opposing candle before displacement
      - Mitigation Block is any opposing candle whose range price revisits
        specifically to fill remaining institutional orders

    Returns: {high, low, mid, type} or None
    """
    if len(df) < lookback + 2:
        return None

    current_price = float(df.iloc[-1]["close"])
    window = df.iloc[-lookback:].reset_index(drop=True)

    for i in range(len(window) - 2, 0, -1):
        o = window.loc[i, "open"]
        c = window.loc[i, "close"]
        h = window.loc[i, "high"]
        l = window.loc[i, "low"]
        next_c = window.loc[i + 1, "close"]

        if direction == 1:
            # Bearish candle followed by bullish impulse
            if c < o and next_c > h:
                in_zone = l <= current_price <= h
                if in_zone:
                    return {
                        "high": float(h), "low": float(l),
                        "mid":  float((h + l) / 2.0),
                        "type": "bullish_mitigation"
                    }

        elif direction == -1:
            # Bullish candle followed by bearish impulse
            if c > o and next_c < l:
                in_zone = l <= current_price <= h
                if in_zone:
                    return {
                        "high": float(h), "low": float(l),
                        "mid":  float((h + l) / 2.0),
                        "type": "bearish_mitigation"
                    }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  7. POWER OF 3 (AMD) PHASE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_open_price(df_m5: pd.DataFrame) -> float:
    """
    Get the NY Midnight open price (05:00 UTC).
    This is the AMD reference price for the day.
    Falls back to first bar of current day's M5 data.
    """
    if df_m5 is None or len(df_m5) == 0:
        return 0.0

    # Find bars from today's NY midnight open (05:00 UTC)
    now_utc = datetime.now(timezone.utc)
    today_open_utc = now_utc.replace(
        hour=NY_MIDNIGHT_OPEN_UTC, minute=0, second=0, microsecond=0
    )

    for _, row in df_m5.iterrows():
        bar_time = row["time"]
        if hasattr(bar_time, "to_pydatetime"):
            bar_time = bar_time.to_pydatetime()
        if bar_time.replace(tzinfo=timezone.utc) >= today_open_utc:
            return float(row["open"])

    return float(df_m5.iloc[0]["open"])


def detect_amd_phase(df_m5: pd.DataFrame,
                     df_h1: pd.DataFrame,
                     direction: int) -> dict:
    """
    Identify which AMD phase the market is currently in.

    Returns:
      {
        "phase":          "accumulation" | "manipulation" | "distribution" | "unknown",
        "daily_open":     float,
        "accum_high":     float,
        "accum_low":      float,
        "manip_swept":    bool,
        "manip_extreme":  float,
        "in_distribution":bool,
        "ready_to_enter": bool,
      }
    """
    result = {
        "phase": "unknown",
        "daily_open": 0.0,
        "accum_high": 0.0,
        "accum_low":  0.0,
        "manip_swept": False,
        "manip_extreme": 0.0,
        "in_distribution": False,
        "ready_to_enter": False,
    }

    if df_m5 is None or len(df_m5) < 20:
        return result

    daily_open = get_daily_open_price(df_m5)
    result["daily_open"] = daily_open

    if daily_open == 0.0:
        return result

    current_price = float(df_m5.iloc[-1]["close"])

    # ── Find accumulation range (first ~2 hours after daily open) ─────────
    # Accumulation = tight horizontal range near daily open
    # Look at first 24 bars of M5 = 2 hours
    early_bars = df_m5.head(min(24, len(df_m5)))
    accum_high = float(early_bars["high"].max())
    accum_low  = float(early_bars["low"].min())
    accum_range = accum_high - accum_low

    result["accum_high"] = accum_high
    result["accum_low"]  = accum_low

    # ── Detect manipulation ───────────────────────────────────────────────
    # Manipulation = price sweeps opposite side of accumulation then reverses
    # Bull AMD: price sweeps below accum_low then comes back above
    # Bear AMD: price sweeps above accum_high then comes back below

    manip_swept   = False
    manip_extreme = 0.0

    if direction == 1:
        # Look for price going below accum_low and returning above it
        for _, row in df_m5.iterrows():
            if row["low"] < accum_low:
                manip_swept   = True
                manip_extreme = float(row["low"])
                break

    elif direction == -1:
        # Look for price going above accum_high and returning below it
        for _, row in df_m5.iterrows():
            if row["high"] > accum_high:
                manip_swept   = True
                manip_extreme = float(row["high"])
                break

    result["manip_swept"]   = manip_swept
    result["manip_extreme"] = manip_extreme

    # ── Determine phase ───────────────────────────────────────────────────
    if not manip_swept:
        # Still in accumulation or haven't seen manipulation yet
        result["phase"] = "accumulation"
        return result

    # Manipulation happened — check if we're now in distribution
    if direction == 1:
        # After bearish manipulation, bullish distribution should be happening
        # Distribution = price above daily open and expanding upward
        if current_price > daily_open:
            result["phase"] = "distribution"
            result["in_distribution"] = True
            result["ready_to_enter"] = True
        else:
            result["phase"] = "manipulation"

    elif direction == -1:
        if current_price < daily_open:
            result["phase"] = "distribution"
            result["in_distribution"] = True
            result["ready_to_enter"] = True
        else:
            result["phase"] = "manipulation"

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  8. SILVER BULLET FVG ENTRY
# ─────────────────────────────────────────────────────────────────────────────

def find_silver_bullet_fvg(df_m5: pd.DataFrame,
                            direction: int) -> dict | None:
    """
    Silver Bullet: FVG formed by displacement during a killzone window.

    Must:
    1. Be within a Silver Bullet time window (checked separately)
    2. Have a genuine displacement candle (large body, no opposing wick)
    3. Leave a clear 3-candle FVG

    Entry at FVG 50% (midpoint). SL below/above displacement wick.

    Returns: {fvg_top, fvg_bottom, fvg_mid, displacement_high, displacement_low}
    """
    if len(df_m5) < 5:
        return None

    # Look at last 12 bars (1 hour of M5)
    window = df_m5.tail(12).reset_index(drop=True)

    for i in range(2, len(window)):
        c0 = window.iloc[i - 2]
        c1 = window.iloc[i - 1]   # potential displacement candle
        c2 = window.iloc[i]

        o1 = c1["open"];  c1c = c1["close"]
        h1 = c1["high"];  l1  = c1["low"]

        body1       = abs(c1c - o1)
        total_rng1  = h1 - l1
        if total_rng1 == 0:
            continue

        body_ratio = body1 / total_rng1

        # Must be a displacement candle
        if body_ratio < DISPLACEMENT_BODY_RATIO:
            continue

        is_bull = c1c > o1 and direction == 1
        is_bear = c1c < o1 and direction == -1

        if not (is_bull or is_bear):
            continue

        if is_bull and c0["high"] < c2["low"]:
            return {
                "fvg_top":          float(c2["low"]),
                "fvg_bottom":       float(c0["high"]),
                "fvg_mid":          float((c2["low"] + c0["high"]) / 2.0),
                "displacement_high":float(h1),
                "displacement_low": float(l1),
                "type":             "bullish_sb_fvg",
            }

        if is_bear and c0["low"] > c2["high"]:
            return {
                "fvg_top":          float(c0["low"]),
                "fvg_bottom":       float(c2["high"]),
                "fvg_mid":          float((c0["low"] + c2["high"]) / 2.0),
                "displacement_high":float(h1),
                "displacement_low": float(l1),
                "type":             "bearish_sb_fvg",
            }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  9. ICT CONFLUENCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_ict_setup(
    direction:       int,
    killzone:        str | None,
    is_silver_bullet:bool,
    ipda_aligned:    bool,
    amd:             dict,
    ote:             dict | None,
    current_price:   float,
    breaker:         dict | None,
    mitigation:      dict | None,
    displacement_fvg:dict | None,
) -> tuple[float, dict]:
    """
    Score the ICT setup 0–11.
    Returns (score, breakdown).
    """
    score     = 0.0
    breakdown = {}

    # Killzone (2 pts)
    if killzone is not None:
        score += ICT_WEIGHTS["killzone"]
        breakdown["killzone"] = ICT_WEIGHTS["killzone"]
    else:
        breakdown["killzone"] = 0.0

    # Silver Bullet bonus (1 pt additional)
    if is_silver_bullet:
        score += ICT_WEIGHTS["silver_bullet"]
        breakdown["silver_bullet"] = ICT_WEIGHTS["silver_bullet"]
    else:
        breakdown["silver_bullet"] = 0.0

    # IPDA draw aligned (2 pts)
    if ipda_aligned:
        score += ICT_WEIGHTS["ipda_draw"]
        breakdown["ipda_draw"] = ICT_WEIGHTS["ipda_draw"]
    else:
        breakdown["ipda_draw"] = 0.0

    # AMD distribution phase (2 pts)
    if amd.get("in_distribution"):
        score += ICT_WEIGHTS["amd_distribution"]
        breakdown["amd_distribution"] = ICT_WEIGHTS["amd_distribution"]
    else:
        breakdown["amd_distribution"] = 0.0

    # OTE zone (1.5 pts)
    if ote and price_in_ote(current_price, ote):
        score += ICT_WEIGHTS["ote_zone"]
        breakdown["ote_zone"] = ICT_WEIGHTS["ote_zone"]
    else:
        breakdown["ote_zone"] = 0.0

    # Breaker block (1 pt)
    if breaker:
        score += ICT_WEIGHTS["breaker_block"]
        breakdown["breaker_block"] = ICT_WEIGHTS["breaker_block"]
    else:
        breakdown["breaker_block"] = 0.0

    # Mitigation block (0.5 pt)
    if mitigation:
        score += ICT_WEIGHTS["mitigation_block"]
        breakdown["mitigation_block"] = ICT_WEIGHTS["mitigation_block"]
    else:
        breakdown["mitigation_block"] = 0.0

    # Displacement FVG (1 pt)
    if displacement_fvg:
        score += ICT_WEIGHTS["displacement_fvg"]
        breakdown["displacement_fvg"] = ICT_WEIGHTS["displacement_fvg"]
    else:
        breakdown["displacement_fvg"] = 0.0

    return score, breakdown


# ─────────────────────────────────────────────────────────────────────────────
#  10. MAIN ICT SIGNAL DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

def detect_ict_signal(
    symbol:       str,
    symbol_data:  dict,
    symbol_point: float,
    ha_daily:     int,
    ha_h4:        int,
) -> tuple[int, float, dict]:
    """
    Full ICT signal detection — runs independently from APA engine.
    Uses HA bias from APA engine for direction only.

    symbol_data keys: d1, h4, h1, m15, m5, m1

    Returns: (direction_int, score, details_dict)
      direction_int: 1=long, -1=short, 0=no signal
    """
    d1  = symbol_data.get("d1")
    h1  = symbol_data.get("h1")
    m15 = symbol_data.get("m15")
    m5  = symbol_data.get("m5")
    m1  = symbol_data.get("m1")

    if any(df is None or len(df) < 5 for df in [d1, h1, m5]):
        return 0, 0.0, {"reason": "ICT: insufficient data"}

    current_price = float(m5.iloc[-1]["close"])

    # ── GATE 1: Need clear HA bias from both Daily and H4 ────────────────
    if ha_daily == 0 or ha_h4 == 0:
        return 0, 0.0, {"reason": "ICT: no HA bias — cannot determine direction"}

    if (ha_daily > 0) != (ha_h4 > 0):
        return 0, 0.0, {
            "reason": f"ICT: Daily/H4 HA conflict ({ha_daily}/{ha_h4})"
        }

    direction = 1 if ha_daily > 0 else -1

    # ── GATE 2: Must be in a killzone ────────────────────────────────────
    killzone, is_silver_bullet = get_active_killzone()
    if killzone is None:
        return 0, 0.0, {"reason": "ICT: outside killzone — no institutional window"}

    # ── IPDA Reference Levels ─────────────────────────────────────────────
    ipda = get_ipda_levels(d1)
    ipda_ok = ipda_aligned_with_bias(ipda, direction)

    # ── AMD Phase ─────────────────────────────────────────────────────────
    amd = detect_amd_phase(m5, h1, direction)

    # ── Displacement on M5 ───────────────────────────────────────────────
    displacement = find_displacement(m5, direction, lookback=12)

    # ── OTE Zone ─────────────────────────────────────────────────────────
    ote = None
    if displacement:
        ote = calculate_ote_zone(
            displacement["swing_low"],
            displacement["swing_high"],
            direction
        )

    # ── Breaker Block on H1 ───────────────────────────────────────────────
    breaker = find_breaker_block(h1, direction, BREAKER_LOOKBACK)

    # ── Mitigation Block on M15 ──────────────────────────────────────────
    mitigation = None
    if m15 is not None and len(m15) >= 5:
        mitigation = find_mitigation_block(m15, direction, lookback=10)

    # ── Silver Bullet FVG ─────────────────────────────────────────────────
    sb_fvg = None
    if is_silver_bullet:
        sb_fvg = find_silver_bullet_fvg(m5, direction)

    # ── ICT SNIPER SHORT ADDITIONS (direction == -1) ─────────────────────
    judas        = detect_judas_swing(m5, direction)
    in_premium   = is_in_premium_array(current_price, d1, direction)
    bearish_ote  = detect_bearish_ote(h1, m5) if direction == -1 else None
    bearish_fvg  = detect_bearish_fvg_entry(m5) if direction == -1 else None

    short_score, short_bd = score_short_ict_additions(
        judas, in_premium, bearish_ote, bearish_fvg
    )

    # ── Score ─────────────────────────────────────────────────────────────
    score, breakdown = score_ict_setup(
        direction        = direction,
        killzone         = killzone,
        is_silver_bullet = is_silver_bullet,
        ipda_aligned     = ipda_ok,
        amd              = amd,
        ote              = ote,
        current_price    = current_price,
        breaker          = breaker,
        mitigation       = mitigation,
        displacement_fvg = displacement,
    )

    # Add short-specific scores
    score    += short_score
    breakdown = {**breakdown, **short_bd}

    # Bearish FVG entry zone takes priority for sniper shorts
    if direction == -1 and bearish_fvg:
        ote = bearish_ote  # use bearish OTE as the ote reference

    # ── Entry Zone: prefer Silver Bullet FVG > OTE > Breaker > Mitigation ─
    entry_zone = None
    entry_source = None

    if sb_fvg:
        entry_zone   = {"mid": sb_fvg["fvg_mid"],
                        "low": sb_fvg["fvg_bottom"],
                        "high": sb_fvg["fvg_top"]}
        entry_source = "silver_bullet_fvg"

    elif ote and price_in_ote(current_price, ote):
        entry_zone   = {"mid": ote["ote_705"],
                        "low": ote["ote_low"],
                        "high": ote["ote_high"]}
        entry_source = "ote"

    elif breaker:
        entry_zone   = {"mid": breaker["mid"],
                        "low": breaker["low"],
                        "high": breaker["high"]}
        entry_source = "breaker_block"

    elif mitigation:
        entry_zone   = {"mid": mitigation["mid"],
                        "low": mitigation["low"],
                        "high": mitigation["high"]}
        entry_source = "mitigation_block"

    # ── IPDA draw level as TP target ──────────────────────────────────────
    draw_key = "nearest_bull_draw" if direction == 1 else "nearest_bear_draw"
    ipda_tp  = ipda.get(draw_key, {})
    ipda_tp_level = ipda_tp.get("level", 0.0) if ipda_tp else 0.0

    # ── AMD manipulation extreme as SL anchor ─────────────────────────────
    manip_sl = amd.get("manip_extreme", 0.0)

    details = {
        "engine":          "ICT",
        "direction":       direction,
        "score":           score,
        "breakdown":       breakdown,
        "killzone":        killzone,
        "is_silver_bullet":is_silver_bullet,
        "ipda":            ipda,
        "ipda_aligned":    ipda_ok,
        "ipda_tp_level":   ipda_tp_level,
        "amd":             amd,
        "displacement":    displacement,
        "ote":             ote,
        "breaker":         breaker,
        "mitigation":      mitigation,
        "sb_fvg":          sb_fvg,
        "entry_zone":      entry_zone,
        "entry_source":    entry_source,
        "manip_sl":        manip_sl,
        "judas":           judas,
        "in_premium":      in_premium,
        "bearish_ote":     bearish_ote,
        "bearish_fvg":     bearish_fvg,
        "short_score":     short_score,
        "current_price":   current_price,
    }

    if score < ICT_MIN_SCORE_HALF:
        return 0, score, {
            "reason": f"ICT score too low: {score:.1f}/{ICT_MAX_SCORE:.0f}",
            **details
        }

    return direction, score, details


# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME PROFILE INTEGRATION WRAPPER — called from detect_ict_signal
# ─────────────────────────────────────────────────────────────────────────────

def apply_vp_to_ict_signal(details: dict, vp_stack: dict,
                            sym_point: float) -> tuple[float, dict]:
    """
    Score an ICT signal against Volume Profile levels.
    Passes OTE zone and IPDA level for deeper alignment checking.

    Returns (vp_score_delta, vp_breakdown).
    """
    from volume_profile import get_vp_confluence, get_vp_tp_target

    entry_zone   = details.get("entry_zone")
    direction    = details.get("direction", 0)
    ote          = details.get("ote")
    ipda         = details.get("ipda", {})
    sb_fvg       = details.get("sb_fvg")

    entry_price  = entry_zone["mid"] if entry_zone else details.get("current_price", 0.0)
    ipda_key     = "nearest_bull_draw" if direction == 1 else "nearest_bear_draw"
    ipda_draw    = ipda.get(ipda_key, {}) or {}
    ipda_level   = ipda_draw.get("level", 0.0)

    vp_score, vp_bd = get_vp_confluence(
        vp_stack    = vp_stack,
        entry_price = entry_price,
        direction   = direction,
        sym_point   = sym_point,
        ote         = ote,
        ipda_level  = ipda_level,
        sb_fvg      = sb_fvg,
    )

    # Nearest VP level — may override IPDA TP if closer
    vp_tp = get_vp_tp_target(vp_stack, entry_price, direction)

    return vp_score, {**vp_bd, "vp_tp_level": vp_tp}


# ─────────────────────────────────────────────────────────────────────────────
#  ICT SNIPER SHORT CONCEPTS
# ─────────────────────────────────────────────────────────────────────────────

# Judas Swing detection window (UTC)
# NY session: price sweeps highs in first 30-60 mins then reverses down
JUDAS_SWING_START_UTC = 12   # 12:00 UTC = NY open
JUDAS_SWING_END_UTC   = 13   # 13:00 UTC = first hour only

# Premium array threshold — price must be above 50% of daily range
# to qualify for bearish OTE (selling in premium)
PREMIUM_THRESHOLD = 0.50


def detect_judas_swing(df_m5: pd.DataFrame, direction: int) -> dict:
    """
    Judas Swing — ICT concept for NY open false move.

    Bullish Judas (sets up SHORT):
      - During NY open killzone (12:00-13:00 UTC)
      - Price pushes UP aggressively (sweeps Asian/London highs)
      - Looks like breakout but is manipulation
      - Smart money selling into the retail longs
      - Real move is DOWN after the sweep

    Bearish Judas (sets up LONG):
      - Price pushes DOWN first, sweeps lows
      - Real move is UP

    Returns:
      {
        "detected":    bool,
        "sweep_level": float,   — the high/low that was swept
        "sweep_price": float,   — the actual wick extreme
        "reversal_confirmed": bool,  — price closed back past sweep level
        "judas_type":  "bull_sweep_short" | "bear_sweep_long"
      }
    """
    result = {
        "detected": False,
        "sweep_level": 0.0,
        "sweep_price": 0.0,
        "reversal_confirmed": False,
        "judas_type": "",
    }

    hour = utc_hour()
    if not (JUDAS_SWING_START_UTC <= hour < JUDAS_SWING_END_UTC):
        return result

    if len(df_m5) < 20:
        return result

    # Reference level: high/low from previous 2 hours (Asian close)
    ref_window = df_m5.iloc[-24:-4]   # ~2 hours back, not last 4 bars
    ref_high   = float(ref_window["high"].max())
    ref_low    = float(ref_window["low"].min())

    # Look at last 4 M5 candles for the sweep and reversal
    recent = df_m5.tail(4).reset_index(drop=True)

    for i in range(len(recent) - 1):
        c    = recent.iloc[i]
        next = recent.iloc[i + 1]

        if direction == -1:
            # Bullish Judas → SHORT setup
            # Wick above ref_high but closed below it
            swept_high = c["high"] > ref_high and c["close"] < ref_high
            if swept_high:
                # Confirm reversal: next candle bearish and closes lower
                reversal = next["close"] < next["open"]
                result.update({
                    "detected":           True,
                    "sweep_level":        ref_high,
                    "sweep_price":        float(c["high"]),
                    "reversal_confirmed": reversal,
                    "judas_type":         "bull_sweep_short",
                })
                return result

        elif direction == 1:
            # Bearish Judas → LONG setup
            swept_low = c["low"] < ref_low and c["close"] > ref_low
            if swept_low:
                reversal = next["close"] > next["open"]
                result.update({
                    "detected":           True,
                    "sweep_level":        ref_low,
                    "sweep_price":        float(c["low"]),
                    "reversal_confirmed": reversal,
                    "judas_type":         "bear_sweep_long",
                })
                return result

    return result


def is_in_premium_array(current_price: float, df_d1: pd.DataFrame,
                          direction: int) -> bool:
    """
    ICT Premium/Discount Array.

    For SHORT entries: price must be in PREMIUM zone (above 50% of daily range)
    For LONG  entries: price must be in DISCOUNT zone (below 50% of daily range)

    Premium = upper 50% of the current day's range
    Discount = lower 50%
    50% equilibrium = midpoint (also aligns with OTE concept)
    """
    if len(df_d1) < 2:
        return True   # Can't determine — don't block

    # Use previous day range as reference
    prev = df_d1.iloc[-2]
    day_high = float(prev["high"])
    day_low  = float(prev["low"])
    day_rng  = day_high - day_low

    if day_rng <= 0:
        return True

    equilibrium = day_low + day_rng * PREMIUM_THRESHOLD

    if direction == -1:
        # Short: must be in premium (above equilibrium)
        return current_price > equilibrium
    else:
        # Long: must be in discount (below equilibrium)
        return current_price < equilibrium


def detect_bearish_ote(df_h1: pd.DataFrame, df_m5: pd.DataFrame) -> dict | None:
    """
    Bearish OTE — Optimal Trade Entry for SHORT positions.

    After a bearish displacement (strong down move) on H1,
    price retraces INTO the 0.62–0.79 Fibonacci zone (premium retracement).
    This is where institutions sell more positions.

    Selling in a retracement = premium OTE short entry.

    Returns: {ote_low, ote_705, ote_high, swing_high, swing_low} or None
    """
    if len(df_h1) < 10 or len(df_m5) < 5:
        return None

    current_price = float(df_m5.iloc[-1]["close"])
    window = df_h1.tail(15).reset_index(drop=True)

    # Find the most recent bearish impulse swing
    for i in range(len(window) - 2, 1, -1):
        # Swing high: higher than both neighbors
        if (window.loc[i, "high"] > window.loc[i-1, "high"] and
                window.loc[i, "high"] > window.loc[i+1, "high"]):

            swing_high = float(window.loc[i, "high"])

            # Find swing low after this swing high
            subsequent = window.iloc[i+1:]
            if len(subsequent) == 0:
                continue

            swing_low = float(subsequent["low"].min())

            if swing_high <= swing_low:
                continue

            # Calculate OTE zone for SHORT (selling in premium retracement)
            ote = calculate_ote_zone(swing_low, swing_high, direction=-1)

            # Is current price in this OTE zone?
            if price_in_ote(current_price, ote):
                return {
                    **ote,
                    "swing_high": swing_high,
                    "swing_low":  swing_low,
                    "type": "bearish_ote",
                }

    return None


def detect_bearish_fvg_entry(df_m5: pd.DataFrame) -> dict | None:
    """
    Bearish FVG entry for sniper shorts.

    After displacement down, price retraces into the FVG created
    by the displacement. This is the ICT sniper short entry:
    sell at the FVG top (where price re-enters the gap).

    The FVG acts as resistance on the return.

    Returns: {fvg_top, fvg_bottom, fvg_mid, entry_price} or None
    """
    if len(df_m5) < 10:
        return None

    current_price = float(df_m5.iloc[-1]["close"])
    window = df_m5.tail(10).reset_index(drop=True)

    for i in range(2, len(window)):
        c0 = window.iloc[i - 2]
        c1 = window.iloc[i - 1]   # displacement candle
        c2 = window.iloc[i]

        # Bearish FVG: c0["low"] > c2["high"]
        if c0["low"] > c2["high"]:
            fvg_top    = float(c0["low"])
            fvg_bottom = float(c2["high"])
            fvg_mid    = (fvg_top + fvg_bottom) / 2.0

            # Displacement candle must be bearish
            if c1["close"] >= c1["open"]:
                continue

            # Price is now returning into the FVG (retracement)
            if fvg_bottom <= current_price <= fvg_top:
                return {
                    "fvg_top":    fvg_top,
                    "fvg_bottom": fvg_bottom,
                    "fvg_mid":    fvg_mid,
                    "entry_price": fvg_top,   # sell at top of gap
                    "type": "bearish_fvg_retracement",
                }

    return None


def score_short_ict_additions(
    judas:        dict,
    in_premium:   bool,
    bearish_ote:  dict | None,
    bearish_fvg:  dict | None,
) -> tuple[float, dict]:
    """
    Additional scoring for ICT sniper short setups.
    These scores ADD to the base ICT score.
    """
    score = 0.0
    bd    = {}

    # Judas Swing confirmed (+2 — very high conviction)
    if judas.get("detected") and judas.get("reversal_confirmed"):
        score += 2.0
        bd["judas_swing"] = 2.0
    elif judas.get("detected"):
        score += 1.0
        bd["judas_swing_unconfirmed"] = 1.0

    # Price in premium array for short (+1)
    if in_premium:
        score += 1.0
        bd["premium_array"] = 1.0

    # Bearish OTE zone hit (+1.5)
    if bearish_ote:
        score += 1.5
        bd["bearish_ote"] = 1.5

    # Bearish FVG retracement entry (+1)
    if bearish_fvg:
        score += 1.0
        bd["bearish_fvg"] = 1.0

    return score, bd
