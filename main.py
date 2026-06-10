import time
import os
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
import math
from dotenv import load_dotenv

load_dotenv()  # Load credentials from .env

from config import (
    SYMBOLS, TIMEFRAME_CYCLE, MAGIC_NUMBER,
    MIN_SL_POINTS, DEFAULT_SL_POINTS, TP_RATIO,
    PROFIT_TARGET_USD, TIME_LIMIT_SECONDS,
    SCAN_INTERVAL_SECONDS, MAX_CONCURRENT_TRADES
)
from strategy import analyze_timeframe_coordination, detect_liquidity_engineering
from risk import calculate_lot_size, calculate_sl_tp

def log(msg):
    print(msg, flush=True)
    try:
        with open("trading_bot/bot_logs.txt", "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def get_df(symbol, tf, count=100):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

def get_filling_type(symbol):
    """Determine the correct filling type for a symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    
    fill_mode = info.filling_mode
    # Check supported modes (bitmask)
    if fill_mode & 2:  # IOC supported
        return mt5.ORDER_FILLING_IOC
    elif fill_mode & 1:  # FOK supported
        return mt5.ORDER_FILLING_FOK
    else:
        return mt5.ORDER_FILLING_RETURN

def manage_open_positions():
    """Manage existing positions — close at profit target or time limit."""
    positions = mt5.positions_get()
    if not positions:
        return 0
    
    count = 0
    for pos in positions:
        if pos.magic != MAGIC_NUMBER:
            continue
        count += 1
        
        # 1. Close if trade is in profit >= target
        if pos.profit >= PROFIT_TARGET_USD:
            close_position(pos, f"Profit Target ${pos.profit:.2f}")
            count -= 1
        # 2. Close if time exceeded
        elif (time.time() - pos.time) > TIME_LIMIT_SECONDS:
            close_position(pos, f"Time Limit ({TIME_LIMIT_SECONDS}s). P&L: ${pos.profit:.2f}")
            count -= 1
    
    return count

def manage_pending_orders():
    """Cancel stale or out-of-range pending orders."""
    orders = mt5.orders_get()
    if not orders:
        return
    for order in orders:
        if getattr(order, 'magic', 0) != MAGIC_NUMBER:
            continue
        # Cancel any pending orders older than 2 minutes
        time_elapsed = time.time() - order.time_setup
        if time_elapsed > 120:
            cancel_order(order.ticket, "Stale (>2min)")

def cancel_order(ticket, reason=""):
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": ticket,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [CANCELLED] Order {ticket}. {reason}")
    else:
        log(f"  -> [ERROR] Failed to cancel order {ticket}. {reason}")
                
def close_position(pos, reason):
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        log(f"  -> [ERROR] No tick to close {pos.ticket}")
        return
    
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
    filling = get_filling_type(pos.symbol)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": order_type,
        "position": pos.ticket,
        "price": price,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": reason[:31],  # MT5 comment max 31 chars
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [CLOSED] {pos.symbol} #{pos.ticket}. {reason}")
    else:
        rc = result.retcode if result else 'None'
        comment = getattr(result, 'comment', '') if result else ''
        log(f"  -> [ERROR] Close failed #{pos.ticket}. Retcode: {rc} {comment}")

def get_ema(df, period, column='close'):
    """Calculate EMA for given period."""
    return df[column].ewm(span=period, adjust=False).mean()

def get_rsi(df, period=14):
    """Calculate RSI."""
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def detect_scalp_signal(symbol):
    """
    Fast scalp signal detection using multiple confirmations:
    1. M15 trend direction (EMA 20 vs EMA 50)
    2. M5 momentum confirmation (EMA 9 cross + RSI)
    3. M1 entry timing (candle pattern + price action)
    
    Returns: ('bullish'|'bearish'|None, signal_strength, details_dict)
    """
    # Get data for all timeframes
    m15 = get_df(symbol, mt5.TIMEFRAME_M15, 60)
    m5 = get_df(symbol, mt5.TIMEFRAME_M5, 60)
    m1 = get_df(symbol, mt5.TIMEFRAME_M1, 30)
    
    if m15 is None or m5 is None or m1 is None:
        return None, 0, {"reason": "Missing data"}
    
    # === M15: Trend bias ===
    m15['ema20'] = get_ema(m15, 20)
    m15['ema50'] = get_ema(m15, 50)
    latest_m15 = m15.iloc[-1]
    
    m15_bullish = latest_m15['ema20'] > latest_m15['ema50'] and latest_m15['close'] > latest_m15['ema20']
    m15_bearish = latest_m15['ema20'] < latest_m15['ema50'] and latest_m15['close'] < latest_m15['ema20']
    
    if not m15_bullish and not m15_bearish:
        return None, 0, {"reason": "No M15 trend alignment"}
    
    trend = 'bullish' if m15_bullish else 'bearish'
    
    # === M5: Momentum confirmation ===
    m5['ema9'] = get_ema(m5, 9)
    m5['ema21'] = get_ema(m5, 21)
    m5['rsi'] = get_rsi(m5, 14)
    latest_m5 = m5.iloc[-1]
    prev_m5 = m5.iloc[-2]
    
    signal_strength = 0
    
    if trend == 'bullish':
        # EMA9 above EMA21
        if latest_m5['ema9'] > latest_m5['ema21']:
            signal_strength += 1
        # EMA9 just crossed above (or trending up)
        if prev_m5['ema9'] <= prev_m5['ema21'] and latest_m5['ema9'] > latest_m5['ema21']:
            signal_strength += 2  # Fresh cross = strong signal
        # RSI in buy zone (40-70, not overbought)
        if 40 < latest_m5['rsi'] < 70:
            signal_strength += 1
        # Price above EMA9 (momentum)
        if latest_m5['close'] > latest_m5['ema9']:
            signal_strength += 1
    else:  # bearish
        if latest_m5['ema9'] < latest_m5['ema21']:
            signal_strength += 1
        if prev_m5['ema9'] >= prev_m5['ema21'] and latest_m5['ema9'] < latest_m5['ema21']:
            signal_strength += 2
        if 30 < latest_m5['rsi'] < 60:
            signal_strength += 1
        if latest_m5['close'] < latest_m5['ema9']:
            signal_strength += 1
    
    if signal_strength < 2:
        return None, signal_strength, {"reason": f"Weak M5 signal ({signal_strength}/5)"}
    
    # === M1: Entry timing ===
    m1['ema5'] = get_ema(m1, 5)
    m1['ema13'] = get_ema(m1, 13)
    latest_m1 = m1.iloc[-1]
    prev_m1 = m1.iloc[-2]
    
    if trend == 'bullish':
        # M1 should show micro-pullback recovery
        m1_confirm = (latest_m1['close'] > latest_m1['ema5'] and
                      latest_m1['close'] > latest_m1['open'])  # bullish candle
        # Or: price bouncing off M1 EMA support
        if not m1_confirm:
            m1_confirm = (prev_m1['low'] <= prev_m1['ema13'] and
                          latest_m1['close'] > prev_m1['high'])
    else:
        m1_confirm = (latest_m1['close'] < latest_m1['ema5'] and
                      latest_m1['close'] < latest_m1['open'])
        if not m1_confirm:
            m1_confirm = (prev_m1['high'] >= prev_m1['ema13'] and
                          latest_m1['close'] < prev_m1['low'])
    
    if m1_confirm:
        signal_strength += 1
    
    # Need at least strength 2 to trade
    if signal_strength >= 2:
        details = {
            "trend": trend,
            "m15_ema20": round(latest_m15['ema20'], 2),
            "m15_ema50": round(latest_m15['ema50'], 2),
            "m5_rsi": round(latest_m5['rsi'], 1),
            "m5_ema_cross": latest_m5['ema9'] > latest_m5['ema21'] if trend == 'bullish' else latest_m5['ema9'] < latest_m5['ema21'],
            "m1_confirm": m1_confirm,
            "strength": signal_strength,
        }
        return trend, signal_strength, details
    
    return None, signal_strength, {"reason": f"Signal too weak ({signal_strength}/6)"}

def place_trade(symbol, direction, signal_strength, details):
    """Place a market order with proper SL/TP accounting for spread."""
    mt5.symbol_select(symbol, True)
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        log(f"  -> [ERROR] Cannot get symbol info for {symbol}")
        return False
    
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        log(f"  -> [ERROR] No tick data for {symbol}")
        return False
    
    # Check if market is actually tradeable
    if symbol_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        log(f"  -> [SKIP] {symbol} trade mode = {symbol_info.trade_mode} (not FULL)")
        return False
    
    point = symbol_info.point
    digits = symbol_info.digits
    spread = tick.ask - tick.bid
    spread_points = spread / point
    
    # Entry price
    if direction == 'bullish':
        entry_price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY
    else:
        entry_price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL
    
    entry_price = round(entry_price, digits)
    
    # SL must be wider than spread + buffer
    # Use the larger of: DEFAULT_SL_POINTS or (spread_points * 2)
    sl_points = max(DEFAULT_SL_POINTS, spread_points * 2, MIN_SL_POINTS)
    tp_points = sl_points * TP_RATIO
    
    # Calculate lot size (risk-managed)
    lot = calculate_lot_size(symbol, sl_points)
    if lot < symbol_info.volume_min:
        lot = symbol_info.volume_min
    
    # Calculate SL/TP prices
    sl, tp = calculate_sl_tp(order_type, entry_price, sl_points, tp_points, symbol_info)
    
    # Get correct filling type
    filling = get_filling_type(symbol)
    
    log(f"  -> Preparing {direction.upper()} order:")
    log(f"     Entry: {entry_price}, SL: {sl}, TP: {tp}")
    log(f"     Lot: {lot}, Spread: {spread_points:.0f}pts, SL: {sl_points:.0f}pts")
    
    # Validate using order_check first
    request = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': symbol,
        'volume': float(lot),
        'type': order_type,
        'price': entry_price,
        'sl': sl,
        'tp': tp,
        'deviation': 20,
        'magic': MAGIC_NUMBER,
        'comment': f'Scalp {direction[:4]} s{signal_strength}',
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': filling,
    }
    
    # Pre-check the order
    check = mt5.order_check(request)
    if check is None:
        log(f"  -> [ERROR] order_check returned None. Error: {mt5.last_error()}")
        return False
    
    if check.retcode != 0:
        log(f"  -> [CHECK FAIL] Retcode: {check.retcode}, Comment: {check.comment}")
        
        # Common fixes
        if check.retcode == 10016:  # Invalid stops
            # SL/TP too close — widen further
            sl_points = max(sl_points * 1.5, spread_points * 3)
            tp_points = sl_points * TP_RATIO
            sl, tp = calculate_sl_tp(order_type, entry_price, sl_points, tp_points, symbol_info)
            request['sl'] = sl
            request['tp'] = tp
            log(f"     Retrying with wider SL: {sl_points:.0f}pts -> SL={sl}, TP={tp}")
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                log(f"  -> [STILL FAILING] Retcode: {check.retcode if check else 'None'}")
                # Last resort: send without SL/TP, we'll manage manually
                request.pop('sl', None)
                request.pop('tp', None)
                log(f"     Attempting without SL/TP (will manage manually)")
        
        elif check.retcode == 10019:  # Not enough money
            # Reduce lot size to minimum
            request['volume'] = float(symbol_info.volume_min)
            lot = symbol_info.volume_min
            log(f"     Reduced lot to minimum: {lot}")
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                log(f"  -> [STILL FAILING] Not enough margin even for min lot")
                return False
        
        elif check.retcode == 10018:  # Market closed
            log(f"  -> [MARKET CLOSED] Cannot trade right now")
            return False
    
    # Send the order
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [SUCCESS] {direction.upper()} {symbol} {lot} lots @ {entry_price}. Ticket: {result.order}")
        return True
    else:
        rc = result.retcode if result else 'None'
        comment = getattr(result, 'comment', '') if result else ''
        log(f"  -> [ERROR] Trade failed. Retcode: {rc} {comment}")
        return False

def run_bot_cycle():
    log(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === Scalp Scan ===")
    
    # Check account
    acc = mt5.account_info()
    if acc:
        log(f"  Balance: ${acc.balance:.2f} | Equity: ${acc.equity:.2f} | Free: ${acc.margin_free:.2f}")
    
    # Count our open positions
    open_count = 0
    positions = mt5.positions_get()
    if positions:
        for p in positions:
            if p.magic == MAGIC_NUMBER:
                open_count += 1
    
    if open_count >= MAX_CONCURRENT_TRADES:
        log(f"  Max trades ({MAX_CONCURRENT_TRADES}) reached. Managing existing positions...")
        return
    
    for symbol in SYMBOLS:
        if open_count >= MAX_CONCURRENT_TRADES:
            break
            
        # Detect scalp opportunity
        direction, strength, details = detect_scalp_signal(symbol)
        
        if direction is None:
            reason = details.get('reason', 'No signal')
            log(f"  {symbol}: {reason}")
            continue
        
        log(f"  {symbol}: {direction.upper()} signal! Strength: {strength}/6")
        log(f"     RSI: {details.get('m5_rsi', 'N/A')}, EMA cross: {details.get('m5_ema_cross', 'N/A')}, M1 confirm: {details.get('m1_confirm', 'N/A')}")
        
        # Also run the AOL/LE check as bonus confirmation (optional — doesn't block trade)
        try:
            m15_df = get_df(symbol, TIMEFRAME_CYCLE['constant'], 50)
            m5_df = get_df(symbol, TIMEFRAME_CYCLE['situational_1'], 100)
            m1_df = get_df(symbol, TIMEFRAME_CYCLE['situational_2'], 100)
            
            if m15_df is not None and m5_df is not None:
                coord = analyze_timeframe_coordination(m15_df, m5_df, direction)
                if coord['aligned']:
                    log(f"     AOL alignment confirmed at {coord.get('aol_level', 'N/A')}")
                    strength += 1  # Bonus strength
        except Exception as e:
            pass  # AOL check is optional, don't let it block trading
        
        # Place the trade
        success = place_trade(symbol, direction, strength, details)
        if success:
            open_count += 1

if __name__ == "__main__":
    # Initialise MT5 library
    if not mt5.initialize():
        print("Failed to initialize MT5 library")
        exit()
    
    # Login with credentials from .env
    LOGIN = int(os.getenv("MT5_LOGIN", 0))
    PASSWORD = os.getenv("MT5_PASSWORD", "")
    SERVER = os.getenv("MT5_SERVER", "")
    if not LOGIN or not PASSWORD or not SERVER:
        print("ERROR: MT5 credentials missing from .env file")
        exit()
    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        print(f"MT5 login failed: {err}")
        exit()
    
    acc = mt5.account_info()
    log("=" * 60)
    log(f"XAUUSD Scalping Bot Started")
    log(f"Account: {LOGIN} @ {SERVER}")
    log(f"Balance: ${acc.balance:.2f} | Leverage: 1:{acc.leverage}")
    log(f"Risk: {10}% per trade | Max trades: {MAX_CONCURRENT_TRADES}")
    log(f"SL: {DEFAULT_SL_POINTS}pts | TP ratio: {TP_RATIO}x")
    log(f"Profit target: ${PROFIT_TARGET_USD} | Time limit: {TIME_LIMIT_SECONDS}s")
    log(f"Scan interval: {SCAN_INTERVAL_SECONDS}s")
    log("=" * 60)
    
    try:
        while True:
            try:
                manage_open_positions()
                manage_pending_orders()
                run_bot_cycle()
            except Exception as e:
                log(f"  [CYCLE ERROR] {type(e).__name__}: {e}")
            
            time.sleep(SCAN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("Bot stopped by user.")
        mt5.shutdown()
