import time
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime

from config import SYMBOLS, TIMEFRAME_CYCLE, MAGIC_NUMBER
from strategy import analyze_timeframe_coordination, detect_liquidity_engineering
from risk import calculate_lot_size, calculate_sl_tp

def log(msg):
    print(msg, flush=True)
    with open("trading_bot/bot_logs.txt", "a") as f:
        f.write(msg + "\n")

def get_df(symbol, tf, count=100):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

def manage_open_positions():
    positions = mt5.positions_get()
    if positions:
        for pos in positions:
            # 1. Close if profit is $1.00 or more
            if pos.profit >= 1.0:
                close_position(pos, "Target Profit Reached")
            # 2. Close if trade has been open for more than 5 minutes (300 seconds)
            elif (time.time() - pos.time) > 300:
                close_position(pos, "Time Limit Exceeded")
                
def close_position(pos, reason):
    tick = mt5.symbol_info_tick(pos.symbol)
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": order_type,
        "position": pos.ticket,
        "price": price,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": reason,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [CLOSED] Position {pos.ticket} on {pos.symbol}. Reason: {reason}. Profit: ${pos.profit}")
    else:
        log(f"  -> [ERROR] Failed to close position {pos.ticket}. Retcode: {result.retcode if result else 'None'}")

def run_bot_cycle():
    log(f"\n[{datetime.now()}] Starting analysis cycle...")
    
    for symbol in SYMBOLS:
        log(f"Analyzing {symbol}...")
        
        # 1. Fetch Data
        weekly = get_df(symbol, TIMEFRAME_CYCLE['constant'], 50)
        h4 = get_df(symbol, TIMEFRAME_CYCLE['situational_1'], 100)
        m30 = get_df(symbol, TIMEFRAME_CYCLE['situational_2'], 100)
        
        if weekly is None or h4 is None or m30 is None:
            log(f"Skipping {symbol} due to missing data.")
            continue
            
        # 2. Check Coordination (Weekly -> H4)
        bullish_coord = analyze_timeframe_coordination(weekly, h4, 'bullish')
        bearish_coord = analyze_timeframe_coordination(weekly, h4, 'bearish')
        
        active_coord = None
        trend_direction = None
        
        if bullish_coord['aligned']:
            active_coord = bullish_coord
            trend_direction = 'bullish'
        elif bearish_coord['aligned']:
            active_coord = bearish_coord
            trend_direction = 'bearish'
            
        if not active_coord:
            log(f"  -> No H4 shift alignment detected for {symbol}.")
            continue
            
        # 3. Check for Liquidity Engineering (M30)
        shift_point = active_coord['aol_level']
        le = detect_liquidity_engineering(m30, shift_point, trend_direction)
        
        if not le['le_detected']:
            log(f"  -> {trend_direction.capitalize()} bias aligned, but waiting for M30 Liquidity Engineering.")
            continue
            
        log(f"  -> SETUP DETECTED! {trend_direction.capitalize()} alignment with LE on {symbol}. FMD: {le['fmd']}")
        
        # 4. Execute Sniper Limit Trade
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            continue
            
        # Determine 50% Retracement limit price
        limit_price = (le['fmd'] + le['choch_price']) / 2.0
        limit_price = round(limit_price, symbol_info.digits)
        
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if trend_direction == 'bullish' else mt5.ORDER_TYPE_SELL_LIMIT
        
        # Current price (just for SL calculation distance if needed, but we base SL on limit_price)
        current_price = symbol_info.ask if trend_direction == 'bullish' else symbol_info.bid
        
        # Calculate SL distance in points using FMD
        point = symbol_info.point
        sl_points = abs(limit_price - le['fmd']) / point
        
        # Standardize minimum SL for scalping (very tight)
        sl_points = max(sl_points, 20)
        tp_points = sl_points * 1.5  # Aggressive 1:1.5 Risk to Reward for fast exits
        
        lot = calculate_lot_size(symbol, sl_points)
        if lot < 0.01: 
            lot = 0.01
            
        # Validate Limit Price vs Current Price to avoid Invalid Price error (10015)
        # For a BUY LIMIT, the limit price must be below the Ask price.
        # For a SELL LIMIT, the limit price must be above the Bid price.
        if order_type == mt5.ORDER_TYPE_BUY_LIMIT and limit_price >= symbol_info.ask:
            log(f"  -> Skipping {symbol}: Limit price {limit_price} is above current Ask {symbol_info.ask}")
            continue
        elif order_type == mt5.ORDER_TYPE_SELL_LIMIT and limit_price <= symbol_info.bid:
            log(f"  -> Skipping {symbol}: Limit price {limit_price} is below current Bid {symbol_info.bid}")
            continue
            
        sl, tp = calculate_sl_tp(mt5.ORDER_TYPE_BUY if trend_direction == 'bullish' else mt5.ORDER_TYPE_SELL, limit_price, sl_points, tp_points, symbol_info)
        
        request = {
            'action': mt5.TRADE_ACTION_PENDING,
            'symbol': symbol,
            'volume': float(lot),
            'type': order_type,
            'price': limit_price,
            'sl': sl,
            'tp': tp,
            'deviation': 20,
            'magic': MAGIC_NUMBER,
            'comment': 'Sniper Limit Entry',
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  -> [SUCCESS] Placed {trend_direction} trade on {symbol}. Ticket: {result.order}")
            # Could add logic here to track 'already traded' FMDs so it doesn't double-enter
        else:
            print(f"  -> [ERROR] Trade failed. Retcode: {result.retcode if result else 'None'}")

if __name__ == "__main__":
    if not mt5.initialize():
        print("Failed to initialize MT5")
        exit()
        
    log("AOL Trading Bot Started. Aggressive High-Frequency Mode.")
    try:
        while True:
            manage_open_positions()
            run_bot_cycle()
            log("Sleeping for 15 seconds for rapid scalping scan...")
            time.sleep(15)  # Wait 15 seconds for high-frequency scanning
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        mt5.shutdown()
