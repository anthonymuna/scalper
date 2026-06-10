import MetaTrader5 as mt5
import time

def log(msg):
    print(msg, flush=True)
    with open('trading_bot/bot_logs.txt', 'a') as f:
        f.write(msg + '\n')

# Initialize connection (assumes MT5 already initialized in this process, otherwise connect)
if not mt5.initialize():
    log('MT5 init failed')
    exit()

# Close all open positions
positions = mt5.positions_get()
if positions:
    for pos in positions:
        symbol = pos.symbol
        ticket = pos.ticket
        volume = pos.volume
        # Determine opposite order type
        order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = mt5.symbol_info_tick(symbol).bid if order_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(symbol).ask
        request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': symbol,
            'volume': volume,
            'type': order_type,
            'position': ticket,
            'price': price,
            'deviation': 20,
            'magic': 123456,
            'comment': 'Close all positions',
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log(f'Closed position {ticket} on {symbol}')
        else:
            log(f'Failed to close position {ticket}: retcode {result.retcode if result else "None"}')
else:
    log('No open positions')

# Cancel all pending orders
orders = mt5.orders_get()
if orders:
    for order in orders:
        request = {'action': mt5.TRADE_ACTION_REMOVE, 'order': order.ticket}
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log(f'Cancelled order {order.ticket}')
        else:
            log(f'Failed to cancel order {order.ticket}: retcode {result.retcode if result else "None"}')
else:
    log('No pending orders')

mt5.shutdown()
log('All trades cleared')
