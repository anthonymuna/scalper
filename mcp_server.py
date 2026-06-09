from fastmcp import FastMCP
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
from risk import calculate_lot_size, calculate_sl_tp
from strategy import identify_aol, detect_market_shift
from config import SYMBOLS, TIMEFRAME_CYCLE, MAGIC_NUMBER

mcp = FastMCP("MT5_Trading_Bot")

@mcp.tool()
def init_mt5():
    """Initialize connection to MetaTrader 5"""
    if not mt5.initialize():
        return f"initialize() failed, error code = {mt5.last_error()}"
    return "MT5 Initialized Successfully"

@mcp.tool()
def get_candles(symbol: str, timeframe: int, count: int) -> str:
    """Fetch recent candles for a symbol and timeframe"""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None:
        return f"Failed to get rates for {symbol}, error code: {mt5.last_error()}"
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df.to_string()

@mcp.tool()
def get_market_structure(symbol: str) -> str:
    """Get the current market structure (AOLs, shifts) across timeframes"""
    # Just a placeholder implementation to fetch candles and run logic
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 100)
    if rates is None:
        return "Failed to get market structure"
    
    df = pd.DataFrame(rates)
    aols = identify_aol(df)
    shift = detect_market_shift(df)
    
    return f"AOLs detected: {len(aols)}, Market Shift: {shift['shift_detected']}"

@mcp.tool()
def place_trade(symbol: str, order_type_str: str, sl_points: float, tp_points: float) -> str:
    """Place a trade on MT5 with calculated risk"""
    order_type = mt5.ORDER_TYPE_BUY if order_type_str.upper() == 'BUY' else mt5.ORDER_TYPE_SELL
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return f"Symbol {symbol} not found"
        
    lot = calculate_lot_size(symbol, sl_points)
    
    price = mt5.symbol_info_tick(symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).bid
    sl, tp = calculate_sl_tp(order_type, price, sl_points, tp_points, symbol_info)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "Bot trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return f"Order failed, retcode={result.retcode}"
        
    return f"Order placed successfully! Ticket: {result.order}"

if __name__ == "__main__":
    mt5.initialize()
    mcp.run()
