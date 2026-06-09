from flask import Flask, render_template_string
import MetaTrader5 as mt5
from config import DASHBOARD_PORT, SYMBOLS

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Trading Bot Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background-color: #f4f4f9; padding: 20px; }
        .card { background: #fff; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        h1, h2 { color: #333; }
        .success { color: green; }
        .danger { color: red; }
    </style>
</head>
<body>
    <h1>Area of Liquidity Trading Bot Dashboard</h1>
    
    <div class="card">
        <h2>Account Info</h2>
        <p><strong>Balance:</strong> ${{ balance }}</p>
        <p><strong>Equity:</strong> ${{ equity }}</p>
        <p><strong>Margin Free:</strong> ${{ margin_free }}</p>
    </div>
    
    <div class="card">
        <h2>Monitored Pairs</h2>
        <ul>
        {% for symbol in symbols %}
            <li>{{ symbol }}</li>
        {% endfor %}
        </ul>
    </div>
    
    <div class="card">
        <h2>Open Positions</h2>
        {% if positions %}
            <ul>
            {% for pos in positions %}
                <li>{{ pos.symbol }} - {{ 'BUY' if pos.type == 0 else 'SELL' }} {{ pos.volume }} lots @ {{ pos.price_open }} (Profit: ${{ pos.profit }})</li>
            {% endfor %}
            </ul>
        {% else %}
            <p>No open positions.</p>
        {% endif %}
    <div class="card">
        <h2>Live Bot Analysis Logs</h2>
        <pre style="background: #1e1e1e; color: #00ff00; padding: 15px; border-radius: 5px; height: 300px; overflow-y: scroll;">
{% if logs %}
{{ logs }}
{% else %}
Waiting for bot to write logs...
{% endif %}
        </pre>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    if not mt5.initialize():
        return "MT5 Initialization Failed"
        
    account = mt5.account_info()
    positions = mt5.positions_get()
    
    # Read logs
    logs = ""
    try:
        with open("trading_bot/bot_logs.txt", "r") as f:
            # Get last 50 lines
            lines = f.readlines()
            logs = "".join(lines[-50:])
    except FileNotFoundError:
        pass
    
    # Process positions
    pos_data = []
    if positions:
        for p in positions:
            pos_data.append({
                "symbol": p.symbol,
                "type": p.type,
                "volume": p.volume,
                "price_open": p.price_open,
                "profit": p.profit
            })
            
    return render_template_string(
        HTML_TEMPLATE, 
        balance=account.balance if account else 0,
        equity=account.equity if account else 0,
        margin_free=account.margin_free if account else 0,
        symbols=SYMBOLS,
        positions=pos_data,
        logs=logs
    )

if __name__ == '__main__':
    print(f"Starting dashboard on port {DASHBOARD_PORT}...")
    app.run(port=DASHBOARD_PORT, debug=True)
