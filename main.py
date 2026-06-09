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
        
        # 4. Execute Trade
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            continue
            
        order_type = mt5.ORDER_TYPE_BUY if trend_direction == 'bullish' else mt5.ORDER_TYPE_SELL
        price = symbol_info.ask if order_type == mt5.ORDER_TYPE_BUY else symbol_info.bid
        
        # Calculate SL distance in points using FMD (Further Most Deviation)
        point = symbol_info.point
        sl_points = abs(price - le['fmd']) / point
        
        # Standardize minimum SL to avoid extreme tightness
        sl_points = max(sl_points, 100)
        tp_points = sl_points * 2  # 1:2 Risk to Reward
        
        lot = calculate_lot_size(symbol, sl_points)
        if lot < 0.01: 
            lot = 0.01
            
        sl, tp = calculate_sl_tp(order_type, price, sl_points, tp_points, symbol_info)
        
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': float(lot),
            'type': order_type,
            'price': price,
            'sl': sl,
            'tp': tp,
            'deviation': 20,
            'magic': MAGIC_NUMBER,
            'comment': 'AOL LE Entry',
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
        
    print("AOL Trading Bot Started.")
    try:
        while True:
            run_bot_cycle()
            print("Sleeping for 5 minutes...")
            time.sleep(300)  # Wait 5 minutes before checking again
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        mt5.shutdown()
