import MetaTrader5 as mt5

# Initialize connection
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
    mt5.shutdown()
    exit()

account = mt5.account_info()
if account:
    print(f"Balance: {account.balance}")
else:
    print('Failed to retrieve account info')

mt5.shutdown()
