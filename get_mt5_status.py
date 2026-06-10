import MetaTrader5 as mt5
import json, sys

def log(msg):
    print(msg, flush=True)

if not mt5.initialize():
    log('MT5 init failed')
    sys.exit(1)

# Get account info
account = mt5.account_info()
log(f'Account: {account.login if account else "N/A"}, Balance: {account.balance if account else "N/A"}')

# Open positions
positions = mt5.positions_get()
log(f'Open positions ({len(positions) if positions else 0}):')
if positions:
    for p in positions:
        log(f'  Ticket={p.ticket}, Symbol={p.symbol}, Type={"BUY" if p.type==mt5.ORDER_TYPE_BUY else "SELL"}, Volume={p.volume}, Price={p.price}, Profit={p.profit}')

# Pending orders
orders = mt5.orders_get()
log(f'Pending orders ({len(orders) if orders else 0}):')
if orders:
    for o in orders:
        typ = {mt5.ORDER_TYPE_BUY_LIMIT: 'BUY_LIMIT', mt5.ORDER_TYPE_SELL_LIMIT: 'SELL_LIMIT', mt5.ORDER_TYPE_BUY_STOP: 'BUY_STOP', mt5.ORDER_TYPE_SELL_STOP: 'SELL_STOP'}.get(o.type, f'Other({o.type})')
        log(f'  Ticket={o.ticket}, Symbol={o.symbol}, Type={typ}, Price={o.price}, Volume={o.volume}')

mt5.shutdown()
