# NGAO Scalper Bot v4.2
**Dual Engine: APA/SMC + ICT | Pure Price Action | Heiken Ashi Bias | Volume Profile**

---

## Quick Start

### Windows (Native — Recommended for Production)
```cmd
pip install -r requirements.txt
copy .env.example .env
# Fill in .env with your MT5 credentials
python run.py
```

### Linux + Wine (Kali, Ubuntu, Debian)
Two options — pick one:

**Option A: Run entire bot inside Wine Python (simplest)**
```bash
# Install Wine
sudo apt install wine winetricks

# Install Python inside Wine
wine winget install Python.Python.3.11
# OR download Python installer and run:
wine python-3.11.exe

# Install packages inside Wine Python
wine pip install -r requirements.txt

# Run bot inside Wine
wine python run.py
```

**Option B: Native Python + mt5linux bridge**
```bash
# Install Wine + MT5
sudo apt install wine
wine /path/to/mt5setup.exe

# Install Python packages natively
pip install -r requirements.txt
pip install mt5linux       # Linux bridge to Wine MT5

# Start MT5 in Wine first (must be running)
wine "C:/Program Files/MetaTrader 5/terminal64.exe" &

# Run bot natively
python run.py
```

---

## Environment Setup

Copy `.env.example` to `.env` and fill in your credentials:

```env
MT5_LOGIN=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=Headway-Demo
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

**Never commit your `.env` file.** It is already in `.gitignore`.

---

## Validate Environment (Before Trading)
```bash
python run.py --check
```

This checks:
- Python version (3.10+ required)
- All required packages installed
- .env file exists with credentials
- MT5 terminal is running
- Platform detection (Windows / Linux+Wine / Linux)

---

## MT5 on Kali Linux — Full Setup

```bash
# 1. Install Wine
sudo apt update && sudo apt install wine wine64 winetricks

# 2. Configure Wine for 64-bit
export WINEARCH=win64
export WINEPREFIX=~/.wine_mt5
winecfg   # Set Windows version to Windows 10

# 3. Download MT5 from your broker and install
wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
wine mt5setup.exe

# 4. Install Python 3.11 inside Wine
wget https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
wine python-3.11.9-amd64.exe

# 5. Install bot packages inside Wine
wine pip install MetaTrader5 pandas numpy python-dotenv python-telegram-bot flask

# 6. Copy bot files and .env to Wine-accessible location
cp -r /path/to/scalper ~/.wine_mt5/drive_c/ngao_scalper/

# 7. Run
wine python C:/ngao_scalper/run.py
```

---

## File Structure

```
scalper/
├── run.py              ← Entry point (start here)
├── main.py             ← Dual-engine scan loop + trade execution
├── strategy.py         ← APA/SMC engine (HA bias, OB, FVG, BOS, CHoCH)
├── ict_strategy.py     ← ICT engine (IPDA, AMD, OTE, Breaker, Silver Bullet)
├── volume_profile.py   ← Fixed Range Volume Profile (POC, VAH, VAL)
├── risk.py             ← Position sizing, daily/weekly/peak guardrails
├── config.py           ← All settings (symbols, risk tiers, sessions)
├── platform_utils.py   ← Cross-platform compatibility (Windows/Linux/Wine)
├── telegram_bot.py     ← Telegram remote control
├── diagnose.py         ← Debug tool — run if bot not connecting
├── requirements.txt    ← Python dependencies
├── .env                ← Your credentials (never commit this)
├── .env.example        ← Credentials template
└── bot_logs.txt        ← Live log output
```

---

## Bot Architecture

```
APA Engine              ICT Engine
───────────             ───────────
HA Daily bias           HA Daily bias (shared)
HA H4 bias              HA H4 bias (shared)
H1 BOS/CHoCH            IPDA 20/40/60-day levels
H1 Order Block          AMD Phase (Accum/Manip/Distrib)
H1 FVG                  Killzone gate (mandatory)
M15 Sweep               OTE 0.62–0.79 / 0.705
M5 BOS                  Breaker Blocks
M1 CHoCH trigger        Mitigation Blocks
                        Silver Bullet FVG
                        Judas Swing (shorts)
                        Premium/Discount Array
        ↓                       ↓
  Confluence 0–10        Confluence 0–11
  Min 7 to fire          Min 7 to fire

Volume Profile (both engines)
  POC / VAH / VAL / HVN / LVN
  +adjusts score, overrides TP target

Sniper Mode (ICT trades only)
  2-candle profit-only exit
  Never close at a loss within sniper window
```

---

## Telegram Commands

| Command | Action |
|---|---|
| `/start` | Resume trading |
| `/stop` | Pause new trades |
| `/status` | Balance, equity, P&L, state |
| `/trades` | List open positions |
| `/close` | Close all bot positions |
| `/log` | Last 20 lines of log |

---

## Symbols

| Symbol | Broker | Session (EAT) |
|---|---|---|
| XAUUSD | Headway | London + NY |
| EURUSD | Headway | London |
| GBPUSD | Headway | London |
| US100  | Headway | NY Open |
| Volatility 75 Index | Deriv | 24/7 |
| Volatility 25 Index | Deriv | 24/7 |
| Boom 1000 Index | Deriv | 24/7 (longs only) |
| Crash 1000 Index | Deriv | 24/7 (shorts only) |

---

## Risk Tiers (Auto-Selected by Balance)

| Balance | Risk/Trade | Daily Limit |
|---|---|---|
| $1–$10 | 0.5% | 3% |
| $10–$50 | 1.0% | 4% |
| $50–$200 | 1.5% | 5% |
| $200–$1000 | 2.0% | 6% |
| $1000+ | 1.5% | 5% |

---

*NGAO Scalper v4.2 | SGT Muna | Built on APA/SMC + ICT*
