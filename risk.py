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
    point = symbol_info.point
    
    if tick_size == 0 or tick_value == 0 or stop_loss_dist_points == 0:
        return DEFAULT_LOT_SIZE

    # Convert stop_loss points to ticks
    # For XAUUSD: point = 0.01, tick_size = 0.01, so 1 point = 1 tick
    sl_ticks = stop_loss_dist_points * (point / tick_size)
    
    # Lot size calculation: risk_amount / (sl_ticks * tick_value_per_lot)
    calculated_lot = risk_amount / (sl_ticks * tick_value)
    
    # Ensure it's within min/max limits
    lot = max(symbol_info.volume_min, min(symbol_info.volume_max, calculated_lot))
    
    # Round to volume step
    volume_step = symbol_info.volume_step
    lot = round(lot / volume_step) * volume_step
    
    # For tiny accounts, cap lot size so margin requirement doesn't exceed free margin
    # XAUUSD: margin ~= price * lot * contract_size / leverage
    # With 1:1000 leverage and price ~4260, margin per 0.01 lot ~= $4.26
    margin_free = account_info.margin_free
    if margin_free > 0:
        # Estimate margin needed (conservative: use 50% of free margin max)
        max_margin = margin_free * 0.5
        contract_size = getattr(symbol_info, 'trade_contract_size', 100)
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            price = tick.ask
            margin_per_lot = (price * contract_size) / account_info.leverage
            max_lot_by_margin = max_margin / margin_per_lot
            max_lot_by_margin = round(max_lot_by_margin / volume_step) * volume_step
            lot = min(lot, max(symbol_info.volume_min, max_lot_by_margin))
    
    return round(lot, 2)

def calculate_sl_tp(order_type, entry_price, sl_points, tp_points, symbol_info):
    """
    Calculate absolute Stop Loss and Take Profit prices.
    Ensures SL/TP are properly rounded to symbol digits.
    """
    point = symbol_info.point
    digits = symbol_info.digits
    
    if order_type == mt5.ORDER_TYPE_BUY:
        sl = round(entry_price - (sl_points * point), digits)
        tp = round(entry_price + (tp_points * point), digits)
    else:  # SELL
        sl = round(entry_price + (sl_points * point), digits)
        tp = round(entry_price - (tp_points * point), digits)
        
    return sl, tp
