"""Quick check: when does XAUUSD open for trading on this broker?"""
import MetaTrader5 as mt5
from datetime import datetime

mt5.initialize()
mt5.login(5054434, password="@!Form3South@", server="Headway-Demo")

info = mt5.symbol_info("XAUUSD")
tick = mt5.symbol_info_tick("XAUUSD")

print(f"Current local time: {datetime.now()}")
print(f"Trade mode: {info.trade_mode} (4=full, 0=disabled)")
print(f"Session deals: {getattr(info, 'session_deals', 'N/A')}")
print(f"Session buy orders: {getattr(info, 'session_buy_orders', 'N/A')}")
print(f"Session sell orders: {getattr(info, 'session_sell_orders', 'N/A')}")

if tick:
    print(f"Tick time: {datetime.fromtimestamp(tick.time)}")
    print(f"Ask: {tick.ask}, Bid: {tick.bid}")

# Try an order_check to see real status
request = {
    'action': mt5.TRADE_ACTION_DEAL,
    'symbol': "XAUUSD",
    'volume': 0.01,
    'type': mt5.ORDER_TYPE_BUY,
    'price': tick.ask if tick else 0,
    'deviation': 50,
    'magic': 123456,
    'comment': 'test',
    'type_time': mt5.ORDER_TIME_GTC,
    'type_filling': mt5.ORDER_FILLING_IOC,
}
check = mt5.order_check(request)
if check:
    print(f"\nOrder check retcode: {check.retcode}")
    print(f"Order check comment: {check.comment}")
else:
    print(f"\nOrder check: None, error={mt5.last_error()}")

mt5.shutdown()
