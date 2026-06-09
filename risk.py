import MetaTrader5 as mt5
from config import DEFAULT_LOT_SIZE, MAX_RISK_PERCENT

def get_account_info():
    """Retrieve account information from MT5."""
    account_info = mt5.account_info()
    if account_info is None:
        print("Failed to get account info, error code:", mt5.last_error())
        return None
    return account_info

def calculate_lot_size(symbol, stop_loss_dist_points):
    """
    Calculate the appropriate lot size given the account balance and risk.
    For small accounts (<$500), this will likely default to the minimum lot size (0.01).
    """
    account_info = get_account_info()
    if not account_info:
        return DEFAULT_LOT_SIZE

    balance = account_info.balance
    risk_amount = balance * (MAX_RISK_PERCENT / 100.0)
    
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        return DEFAULT_LOT_SIZE

    # Value of one tick
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    
    if tick_size == 0 or tick_value == 0 or stop_loss_dist_points == 0:
        return DEFAULT_LOT_SIZE

    # Lot size calculation based on risk amount
    # (risk_amount) / (stop_loss_points * point_value)
    # simplified for micro lots
    calculated_lot = risk_amount / (stop_loss_dist_points * tick_value)
    
    # Ensure it's within min/max limits
    lot = max(symbol_info.volume_min, min(symbol_info.volume_max, calculated_lot))
    
    # Round to volume step
    volume_step = symbol_info.volume_step
    lot = round(lot / volume_step) * volume_step
    
    return lot

def calculate_sl_tp(order_type, entry_price, sl_points, tp_points, symbol_info):
    """
    Calculate absolute Stop Loss and Take Profit prices.
    """
    point = symbol_info.point
    if order_type == mt5.ORDER_TYPE_BUY:
        sl = entry_price - (sl_points * point)
        tp = entry_price + (tp_points * point)
    else:  # SELL
        sl = entry_price + (sl_points * point)
        tp = entry_price - (tp_points * point)
        
    return sl, tp
