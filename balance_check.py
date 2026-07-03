import MetaTrader5 as mt5

# Initialize connection
if not mt5.initialize():
    print('Failed to initialize MT5')
    exit()

# Login credentials (same as main script)
from dotenv import load_dotenv
import os
load_dotenv()
LOGIN    = int(os.getenv("MT5_LOGIN", 0))
PASSWORD = os.getenv("MT5_PASSWORD", "")
SERVER   = os.getenv("MT5_SERVER",   "")
if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
    err = mt5.last_error()
    print(f"MT5 login failed (code {err.retcode}): {err.message}")
    mt5.shutdown()
    exit()

account = mt5.account_info()
if account:
    print(f"Balance: {account.balance}")
else:
    print('Failed to retrieve account info')

mt5.shutdown()
