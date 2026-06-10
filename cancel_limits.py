import MetaTrader5 as mt5
import time

# Ensure MT5 is initialized and logged in (reuse existing session if possible)
if not mt5.initialize():
    print('Failed to initialize MT5')
    exit()

# Login credentials (same as main script)
LOGIN = 5054434
PASSWORD = "@!Form3South@"
SERVER = "Headway-Demo"
if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
    err = mt5.last_error()
    print(f"MT5 login failed (code {err.retcode}): {err.message}")
    exit()

orders = mt5.orders_get()
if not orders:
    print('No pending orders to cancel.')
else:
    for order in orders:
        if order.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": order.ticket,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"Cancelled limit order {order.ticket} ({order.type}) for {order.symbol}")
            else:
                print(f"Failed to cancel order {order.ticket}: retcode {result.retcode if result else 'None'}")

mt5.shutdown()
