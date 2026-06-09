import pandas as pd
import numpy as np

def identify_aol(df):
    """
    Identify Areas of Liquidity (AOL)
    Looks for engulfing patterns (Type 1) to mark potential liquidity zones.
    """
    aols = []
    if len(df) < 2:
        return aols
        
    for i in range(1, len(df)):
        prev = df.iloc[i-1]
        curr = df.iloc[i]
        
        prev_is_bearish = prev['close'] < prev['open']
        prev_is_bullish = prev['close'] > prev['open']
        curr_is_bearish = curr['close'] < curr['open']
        curr_is_bullish = curr['close'] > curr['open']
        
        # Bullish Engulfing (Type 1)
        if prev_is_bullish and curr_is_bullish:
            # Current candle sweeps below previous low
            sweeps_low = curr['low'] < prev['low']
            # Fully engulfs the previous close
            engulfs_close = curr['close'] > prev['close']
            
            if sweeps_low and engulfs_close:
                aols.append({"type": "bullish_engulfing", "index": curr.name, "time": curr['time'], "high": curr['high'], "low": curr['low']})
                
        # Bearish Engulfing (Type 1)
        elif prev_is_bearish and curr_is_bearish:
            # Current candle sweeps above previous high
            sweeps_high = curr['high'] > prev['high']
            # Fully engulfs the previous close
            engulfs_close = curr['close'] < prev['close']
            
            if sweeps_high and engulfs_close:
                aols.append({"type": "bearish_engulfing", "index": curr.name, "time": curr['time'], "high": curr['high'], "low": curr['low']})
                
    return aols

def detect_market_shift(df, shift_point, current_trend):
    """
    Detects a shift in market structure.
    A shift occurs when the market closes below the shift point (in a bullish trend)
    or above the shift point (in a bearish trend).
    """
    for i in range(len(df)):
        curr = df.iloc[i]
        # Bearish shift: Price was bullish, but closed below support (shift point)
        if current_trend == 'bullish' and curr['close'] < shift_point:
            return {"shift_detected": True, "direction": "bearish", "time": curr['time']}
        # Bullish shift: Price was bearish, but closed above resistance (shift point)
        elif current_trend == 'bearish' and curr['close'] > shift_point:
            return {"shift_detected": True, "direction": "bullish", "time": curr['time']}
            
    return {"shift_detected": False, "direction": None}

def detect_liquidity_engineering(df, level, current_trend):
    """
    Detects Liquidity Engineering (LE).
    Requires:
    1. Overthrow: Thrust candlestick sweeping past the level.
    2. FMD: Further Most Deviation (the extreme wick for stop loss).
    3. CHoCH: Change of Character indicating market reversal.
    """
    le_detected = False
    fmd = None
    choch_detected = False
    
    for i in range(1, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i-1]
        
        # Bullish LE: Sweeping support (level) to go long
        if current_trend == 'bullish':
            # Thrust candle sweeps below the level
            if curr['low'] < level:
                # FMD is the lowest point
                if fmd is None or curr['low'] < fmd:
                    fmd = curr['low']
                
                # Check for CHoCH: Price reverses and closes above previous high
                if curr['close'] > prev['high']:
                    choch_detected = True
                    le_detected = True
                    break
                    
        # Bearish LE: Sweeping resistance (level) to go short
        elif current_trend == 'bearish':
            # Thrust candle sweeps above the level
            if curr['high'] > level:
                # FMD is the highest point
                if fmd is None or curr['high'] > fmd:
                    fmd = curr['high']
                    
                # Check for CHoCH: Price reverses and closes below previous low
                if curr['close'] < prev['low']:
                    choch_detected = True
                    le_detected = True
                    break
                    
    if le_detected:
        return {"le_detected": True, "fmd": fmd, "choch": choch_detected}
        
    return {"le_detected": False, "fmd": None, "choch": False}

def analyze_timeframe_coordination(constant_df, situational_df, current_trend):
    """
    Ensure the higher timeframe (e.g. Weekly) bias aligns with the lower timeframe (e.g. H4) shift.
    """
    # 1. Identify AOLs on Constant TF (Weekly)
    constant_aols = identify_aol(constant_df)
    if not constant_aols:
        return {"aligned": False, "bias": "neutral", "reason": "No AOL on Constant TF"}
        
    latest_aol = constant_aols[-1]
    
    # Define the shift point based on the AOL's extreme
    shift_point = latest_aol['low'] if current_trend == 'bullish' else latest_aol['high']
    
    # 2. Check for Market Shift on Situational TF (H4)
    shift = detect_market_shift(situational_df, shift_point, current_trend)
    
    if shift['shift_detected']:
        return {"aligned": True, "bias": shift['direction'], "shift_time": shift['time'], "aol_level": shift_point}
        
    return {"aligned": False, "bias": "neutral", "reason": "No Shift Detected on Situational TF"}
