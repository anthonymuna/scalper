"""Diagnose MT5 trading bot issues."""
import os
import MetaTrader5 as mt5
import math
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()  # Load credentials from .env

def main():
    if not mt5.initialize():
        print("FAIL: Cannot initialize MT5")
        return

    LOGIN = int(os.getenv("MT5_LOGIN", 0))
    PASSWORD = os.getenv("MT5_PASSWORD", "")
    SERVER = os.getenv("MT5_SERVER", "")
    if not LOGIN or not PASSWORD or not SERVER:
        print("ERROR: MT5 credentials missing from .env file")
        return
    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        print(f"FAIL: Login failed: {err}")
        return

    print("=== ACCOUNT INFO ===")
    acc = mt5.account_info()
    print(f"  Balance:      ${acc.balance}")
    print(f"  Equity:       ${acc.equity}")
    print(f"  Free Margin:  ${acc.margin_free}")
    print(f"  Leverage:     1:{acc.leverage}")
    print(f"  Trade Allowed:{acc.trade_allowed}")
    print(f"  Trade Expert: {acc.trade_expert}")

    symbol = "XAUUSD"
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    print(f"\n=== SYMBOL INFO: {symbol} ===")
    print(f"  Visible:      {info.visible}")
    print(f"  Trade Mode:   {info.trade_mode} (0=disabled, 4=full)")
    print(f"  Min Lot:      {info.volume_min}")
    print(f"  Max Lot:      {info.volume_max}")
    print(f"  Lot Step:     {info.volume_step}")
    print(f"  Point:        {info.point}")
    print(f"  Digits:       {info.digits}")
    print(f"  Tick Value:   {info.trade_tick_value}")
    print(f"  Tick Size:    {info.trade_tick_size}")
    print(f"  Spread:       {info.spread}")
    print(f"  Filling Mode: {info.filling_mode}")
    print(f"  Trade Stops:  {info.trade_stops_level}")

    if tick:
        print(f"\n=== TICK DATA ===")
        print(f"  Ask:          {tick.ask}")
        print(f"  Bid:          {tick.bid}")
        print(f"  Spread:       {(tick.ask - tick.bid)/info.point:.1f} points")
        print(f"  Time:         {datetime.fromtimestamp(tick.time)}")
    else:
        print("\n  NO TICK DATA — Market likely closed!")

    # Check what order would be sent
    print(f"\n=== SIMULATED ORDER ===")
    entry_price = tick.ask if tick else 0
    point = info.point
    precision = info.digits

    # Calculate lot size like the bot does
    risk_amount = acc.balance * (20.0 / 100.0)
    sl_points = 20  # min from code
    tick_value = info.trade_tick_value
    tick_size = info.trade_tick_size
    if tick_value > 0 and sl_points > 0:
        calculated_lot = risk_amount / (sl_points * tick_value)
    else:
        calculated_lot = 0.01
    lot = max(info.volume_min, min(info.volume_max, calculated_lot))
    lot = round(lot / info.volume_step) * info.volume_step

    sl = entry_price - (sl_points * point)
    tp = entry_price + (sl_points * 1.5 * point)

    print(f"  Entry Price:  {entry_price}")
    print(f"  SL:           {sl}")
    print(f"  TP:           {tp}")
    print(f"  SL Distance:  {sl_points} points = ${sl_points * tick_value * lot:.2f}")
    print(f"  Lot Size:     {lot}")
    print(f"  Risk Amount:  ${risk_amount:.2f}")
    print(f"  Margin Req:   ~${entry_price * lot * 100 / acc.leverage:.2f}")

    # Check filling modes
    print(f"\n=== FILLING MODE CHECK ===")
    filling_ioc = 1  # ORDER_FILLING_IOC
    filling_fok = 0  # ORDER_FILLING_FOK
    filling_return = 2  # ORDER_FILLING_RETURN
    fill_mode = info.filling_mode
    print(f"  Symbol fill_mode bitmask: {fill_mode}")
    print(f"  Supports FOK (0):    {bool(fill_mode & 1)}")
    print(f"  Supports IOC (1):    {bool(fill_mode & 2)}")
    print(f"  Supports RETURN (2): {bool(fill_mode & 4)}")

    # Try to determine the right filling type
    if fill_mode & 2:
        right_fill = mt5.ORDER_FILLING_IOC
        print(f"  -> Best filling: IOC")
    elif fill_mode & 1:
        right_fill = mt5.ORDER_FILLING_FOK
        print(f"  -> Best filling: FOK")
    else:
        right_fill = mt5.ORDER_FILLING_RETURN
        print(f"  -> Best filling: RETURN")

    # Check market session
    print(f"\n=== MARKET SESSION ===")
    print(f"  Current Time:  {datetime.now()}")
    print(f"  Trade Mode:    {info.trade_mode}")
    sess_from = getattr(info, 'session_deals', None)
    print(f"  Market is:     {'OPEN' if info.trade_mode == 4 else 'CLOSED or RESTRICTED'}")

    # Check open positions & pending orders
    positions = mt5.positions_get()
    orders = mt5.orders_get()
    print(f"\n=== OPEN POSITIONS: {len(positions) if positions else 0} ===")
    if positions:
        for p in positions:
            print(f"  {p.symbol} {('BUY' if p.type==0 else 'SELL')} {p.volume} lots, profit=${p.profit}")
    print(f"\n=== PENDING ORDERS: {len(orders) if orders else 0} ===")
    if orders:
        for o in orders:
            print(f"  {o.symbol} type={o.type} price={o.price_open} vol={o.volume_current}")

    # Try a check order (ORDER_CHECK, doesn't actually place)
    print(f"\n=== ORDER CHECK (dry run) ===")
    if tick:
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': 0.01,
            'type': mt5.ORDER_TYPE_BUY,
            'price': tick.ask,
            'sl': round(tick.ask - 50 * point, precision),
            'tp': round(tick.ask + 75 * point, precision),
            'deviation': 20,
            'magic': 123456,
            'comment': 'Diagnose Test',
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': right_fill,
        }
        check = mt5.order_check(request)
        if check:
            print(f"  Retcode:   {check.retcode}")
            print(f"  Comment:   {check.comment}")
            print(f"  Balance:   {check.balance}")
            print(f"  Margin:    {check.margin}")
            print(f"  Profit:    {check.profit}")
        else:
            print(f"  order_check returned None. Last error: {mt5.last_error()}")

    mt5.shutdown()
    print("\nDone.")

if __name__ == "__main__":
    main()
