"""
main.py — NGAO Scalper Bot v3.0
================================
Pure Price Action | APA/SMC | Heiken Ashi Bias Layer
Multi-Symbol: Headway (XAUUSD, EURUSD, GBPUSD, US100) +
              Deriv Synthetics (Vol75, Vol25, Boom1000, Crash1000)

Signal Flow:
  HA Daily + HA H4 → bias
  H1 BOS/CHoCH + OB + FVG → structure and entry zone
  M15 sweep → confirmation
  M5 BOS + M1 CHoCH → sniper entry trigger
  Confluence score ≥ 7/10 → trade fires

Guardrails:
  - Per-trade dynamic lot (balance-tiered risk%)
  - Daily loss halt (tiered by balance)
  - Weekly drawdown halt (10% — manual restart)
  - Peak equity emergency stop (20% drop)
  - Per-symbol: 3 trades/day, 30min gap, 2-loss session lock
  - Max 3 concurrent trades, correlation filter
  - Partial TPs: 30% @ 1:1, 40% @ 1:2, trail remainder
  - Structure-based trailing SL (no ATR)
  - Pending order expiry (3 candles)
  - 2-hour max hold on real market symbols
  - Startup reconciliation after reconnect/restart
"""

import os
import time
import threading
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

from config import (
    SYMBOLS, SYNTHETIC_SYMBOLS, LONG_ONLY_SYMBOLS, SHORT_ONLY_SYMBOLS,
    TF_DAILY, TF_H4, TF_H1, TF_M15, TF_M5, TF_M1,
    BARS_DAILY, BARS_H4, BARS_H1, BARS_M15, BARS_M5, BARS_M1,
    MAGIC_NUMBER, MIN_SIGNAL_SCORE, MIN_SIGNAL_SCORE_HALF,
    TP1_RR, TP2_RR, TP1_PCT, TP2_PCT,
    SYMBOL_MAX_SPREAD, SYMBOL_SL_BUFFER,
    MIN_SL_POINTS, DEFAULT_SL_POINTS,
    ASIAN_START, ASIAN_END, DEAD_ZONE_START, DEAD_ZONE_END,
    LONDON_START, LONDON_END, NY_START, NY_END,
    SCAN_INTERVAL_SECS, BALANCE_MILESTONES,
    MAX_CONCURRENT_TRADES, MAX_HOLD_MINUTES,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_FLUSH_SECS,
    get_risk_percent,
)
from strategy import (
    detect_scalp_signal,
    get_structure_trail_sl,
    find_swing_low, find_swing_high,
)
from risk import (
    daily_tracker, weekly_tracker, peak_monitor, sym_tracker,
    calculate_lot_size, calculate_sl_tp, sl_from_swing,
    validate_sl_tp, get_filling_type,
)


# ─────────────────────────────────────────────────────────────────────────────
#  BOT STATE
# ─────────────────────────────────────────────────────────────────────────────

BOT_STATE   = "RUNNING"
_state_lock = threading.Lock()

def get_state() -> str:
    with _state_lock:
        return BOT_STATE

def set_state(state: str) -> None:
    global BOT_STATE
    with _state_lock:
        BOT_STATE = state
    log(f"[STATE] → {state}")


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

_LOG_PATH = os.path.join(os.path.dirname(__file__), "bot_logs.txt")

def log(msg: str) -> None:
    stamped = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(stamped, flush=True)
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(stamped + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM — QUEUED AND BATCHED
# ─────────────────────────────────────────────────────────────────────────────

_tg_queue:     list = []
_tg_last_sent: float = 0.0
_tg_lock       = threading.Lock()

def tg_queue(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    with _tg_lock:
        _tg_queue.append(msg)

def tg_flush() -> None:
    global _tg_last_sent
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    now = time.time()
    if now - _tg_last_sent < TELEGRAM_FLUSH_SECS:
        return
    with _tg_lock:
        if not _tg_queue:
            return
        messages = list(_tg_queue)
        _tg_queue.clear()
    _tg_last_sent = now

    import urllib.request, urllib.parse
    for msg in messages:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       msg,
                "parse_mode": "HTML",
            }).encode()
            urllib.request.urlopen(url, data, timeout=5)
        except Exception as e:
            log(f"[TG] Send failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  SESSION FILTER
# ─────────────────────────────────────────────────────────────────────────────

def get_session(symbol: str) -> str:
    """Returns current active session or 'dead'."""
    if symbol in SYNTHETIC_SYMBOLS:
        return "synthetic"   # Always active

    hour = datetime.now(timezone.utc).hour

    # Dead zone between Asian close and London open
    if DEAD_ZONE_START <= hour < DEAD_ZONE_END:
        return "dead"

    in_london = LONDON_START <= hour < LONDON_END
    in_ny     = NY_START     <= hour < NY_END

    if symbol in ("XAUUSD", "XAUUSDm"):
        if in_london or in_ny:
            return "london_ny"
    if symbol in ("EURUSD", "GBPUSD"):
        if in_london:
            return "london"
    if symbol in ("US100",):
        if in_ny:
            return "ny"

    # Asian session for real markets — skip
    if ASIAN_START <= hour or hour < ASIAN_END:
        return "asian_skip"

    return "dead"


def is_tradeable_session(symbol: str) -> bool:
    session = get_session(symbol)
    return session not in ("dead", "asian_skip")


# ─────────────────────────────────────────────────────────────────────────────
#  SPREAD FILTER
# ─────────────────────────────────────────────────────────────────────────────

def get_spread_points(symbol: str) -> float:
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None or info.point == 0:
        return 9999.0
    return (tick.ask - tick.bid) / info.point


def passes_spread_filter(symbol: str) -> bool:
    if symbol in SYNTHETIC_SYMBOLS:
        return True   # Synthetics have fixed minimal spread
    max_spread = SYMBOL_MAX_SPREAD.get(symbol, 100)
    if max_spread == 0:
        return True
    return get_spread_points(symbol) <= max_spread


# ─────────────────────────────────────────────────────────────────────────────
#  CORRELATION FILTER
# ─────────────────────────────────────────────────────────────────────────────

def passes_correlation_filter(symbol: str, direction: int) -> bool:
    """
    EURUSD and GBPUSD are ~85% correlated.
    Don't open same-direction trade on both simultaneously.
    """
    is_usd_pair = any(x in symbol for x in ["EUR", "GBP"])
    if not is_usd_pair:
        return True

    positions = mt5.positions_get()
    if not positions:
        return True

    for p in positions:
        if p.magic != MAGIC_NUMBER:
            continue
        open_sym = p.symbol
        open_dir = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        if any(x in open_sym for x in ["EUR", "GBP"]) and open_dir == direction:
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  DATA FETCHER
# ─────────────────────────────────────────────────────────────────────────────

def get_df(symbol: str, tf: int, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def fetch_all_timeframes(symbol: str) -> dict | None:
    """Fetch all required timeframes in one go."""
    mt5.symbol_select(symbol, True)
    data = {}
    specs = [
        ("d1",  TF_DAILY, BARS_DAILY),
        ("h4",  TF_H4,    BARS_H4),
        ("h1",  TF_H1,    BARS_H1),
        ("m15", TF_M15,   BARS_M15),
        ("m5",  TF_M5,    BARS_M5),
        ("m1",  TF_M1,    BARS_M1),
    ]
    for key, tf, bars in specs:
        df = get_df(symbol, tf, bars)
        if df is None or len(df) < 5:
            log(f"  {symbol}: No data for {key}")
            return None
        data[key] = df
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  PARTIAL CLOSE TRACKER
# ─────────────────────────────────────────────────────────────────────────────

_tp1_done: set = set()
_tp2_done: set = set()


def close_partial(ticket: int, percent: float, symbol: str) -> bool:
    """Close `percent`% of a position by ticket."""
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False

    total_vol = pos[0].volume
    close_vol = round(total_vol * percent / 100.0, 2)

    sym_info = mt5.symbol_info(symbol)
    if not sym_info:
        return False

    vol_min  = sym_info.volume_min
    vol_step = sym_info.volume_step
    close_vol = max(vol_min, close_vol)
    close_vol = round(round(close_vol / vol_step) * vol_step, 2)

    if close_vol >= total_vol:
        return False   # Would close entire position — let TP handle it

    order_type = (mt5.ORDER_TYPE_SELL if pos[0].type == mt5.ORDER_TYPE_BUY
                  else mt5.ORDER_TYPE_BUY)
    tick  = mt5.symbol_info_tick(symbol)
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       float(close_vol),
        "type":         order_type,
        "position":     ticket,
        "price":        price,
        "deviation":    30,
        "magic":        MAGIC_NUMBER,
        "comment":      f"NGAO partial {int(percent)}%",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    result = mt5.order_send(request)
    ok = result and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        log(f"  Partial close: {symbol} #{ticket} {close_vol}L ({int(percent)}%)")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
#  CLOSE POSITION
# ─────────────────────────────────────────────────────────────────────────────

def close_position(pos, reason: str) -> bool:
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return False
    order_type = (mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY
                  else mt5.ORDER_TYPE_BUY)
    price   = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
    filling = get_filling_type(pos.symbol)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       pos.symbol,
        "volume":       pos.volume,
        "type":         order_type,
        "position":     pos.ticket,
        "price":        price,
        "deviation":    30,
        "magic":        MAGIC_NUMBER,
        "comment":      reason[:31],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    ok = result and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        log(f"  Closed #{pos.ticket} {pos.symbol} | {reason}")
    return ok


def close_all_positions(reason: str = "Emergency") -> None:
    positions = mt5.positions_get()
    if not positions:
        return
    for pos in positions:
        if pos.magic == MAGIC_NUMBER:
            close_position(pos, reason)


# ─────────────────────────────────────────────────────────────────────────────
#  MANAGE OPEN POSITIONS — Partials, Trail, Time Exit
# ─────────────────────────────────────────────────────────────────────────────

def manage_open_positions() -> None:
    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        if pos.magic != MAGIC_NUMBER:
            continue

        symbol    = pos.symbol
        ticket    = pos.ticket
        direction = 1 if pos.type == mt5.ORDER_TYPE_BUY else -1
        open_price= pos.price_open
        current_sl= pos.sl
        sl_dist   = abs(open_price - current_sl)

        sym_info = mt5.symbol_info(symbol)
        if not sym_info:
            continue

        point  = sym_info.point
        digits = sym_info.digits

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue

        current_price = tick.bid if direction == 1 else tick.ask
        profit_dist   = (current_price - open_price) * direction
        rr            = profit_dist / sl_dist if sl_dist > 0 else 0

        # ── TIME EXIT: real-market symbols only ──────────────────────────
        if symbol not in SYNTHETIC_SYMBOLS:
            held_mins = (time.time() - pos.time) / 60.0
            if held_mins > MAX_HOLD_MINUTES and pos.profit < 0:
                if close_position(pos, f"Time limit {MAX_HOLD_MINUTES}min"):
                    tg_queue(f"⏰ TIME EXIT: {symbol}\nHeld {int(held_mins)}min | "
                             f"RR: {rr:.2f}")
                continue

        # ── TP1: close 30% + breakeven ───────────────────────────────────
        if rr >= TP1_RR and ticket not in _tp1_done:
            closed = close_partial(ticket, TP1_PCT, symbol)
            if closed:
                _tp1_done.add(ticket)
                tg_queue(f"✅ TP1: {symbol} | 30% closed | RR 1:1")

            # Move SL to breakeven
            new_sl = round(open_price, digits)
            stop_level = sym_info.trade_stops_level * point
            be_ok = (direction == 1 and new_sl > current_sl and
                     current_price - new_sl > stop_level)
            be_ok = be_ok or (direction == -1 and new_sl < current_sl and
                              new_sl - current_price > stop_level)
            if be_ok:
                req = {
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "symbol":   symbol,
                    "position": ticket,
                    "sl":       new_sl,
                    "tp":       pos.tp,
                }
                mt5.order_send(req)

        # ── TP2: close 40% ───────────────────────────────────────────────
        if rr >= TP2_RR and ticket not in _tp2_done:
            closed = close_partial(ticket, TP2_PCT, symbol)
            if closed:
                _tp2_done.add(ticket)
                tg_queue(f"✅ TP2: {symbol} | 40% closed | RR 1:2")

        # ── STRUCTURAL TRAILING STOP (after TP2) ─────────────────────────
        if rr >= TP2_RR:
            df_m5 = get_df(symbol, TF_M5, 20)
            if df_m5 is not None:
                trail_sl = get_structure_trail_sl(df_m5, direction)
                if trail_sl > 0:
                    stop_level = sym_info.trade_stops_level * point
                    freeze_level = sym_info.trade_freeze_level * point

                    # Skip if within freeze level
                    near_sl = abs(current_price - current_sl) <= freeze_level
                    if not near_sl:
                        trail_valid = (
                            (direction == 1 and
                             trail_sl > current_sl and
                             current_price - trail_sl > stop_level)
                            or
                            (direction == -1 and
                             trail_sl < current_sl and
                             trail_sl - current_price > stop_level)
                        )
                        if trail_valid:
                            req = {
                                "action":   mt5.TRADE_ACTION_SLTP,
                                "symbol":   symbol,
                                "position": ticket,
                                "sl":       round(trail_sl, digits),
                                "tp":       pos.tp,
                            }
                            mt5.order_send(req)

        # ── CHoCH EXIT — structure flips against us ───────────────────────
        if rr > 0.5:
            df_m5 = get_df(symbol, TF_M5, 20)
            if df_m5 is not None:
                from strategy import detect_bos_choch
                m5_struct = detect_bos_choch(df_m5, lookback=10)
                flip = (direction == 1 and m5_struct["choch_bear"]) or \
                       (direction == -1 and m5_struct["choch_bull"])
                if flip:
                    if close_position(pos, "CHoCH flip"):
                        tg_queue(f"🔄 CHoCH EXIT: {symbol} | RR: {rr:.2f}")


# ─────────────────────────────────────────────────────────────────────────────
#  PENDING ORDER EXPIRY
# ─────────────────────────────────────────────────────────────────────────────

_pending_candle_count: dict = defaultdict(int)

def manage_pending_orders() -> None:
    from config import ORDER_EXPIRY_BARS
    orders = mt5.orders_get()
    if not orders:
        return
    for order in orders:
        if getattr(order, "magic", 0) != MAGIC_NUMBER:
            continue
        _pending_candle_count[order.ticket] += 1
        if _pending_candle_count[order.ticket] >= ORDER_EXPIRY_BARS:
            req = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order":  order.ticket,
            }
            result = mt5.order_send(req)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"  Expired pending #{order.ticket} {order.symbol}")
                del _pending_candle_count[order.ticket]


# ─────────────────────────────────────────────────────────────────────────────
#  PLACE TRADE
# ─────────────────────────────────────────────────────────────────────────────

def place_trade(symbol: str, direction: int, score: float,
                details: dict) -> bool:
    sym_info = mt5.symbol_info(symbol)
    if not sym_info:
        log(f"  {symbol}: Cannot get symbol info")
        return False

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        log(f"  {symbol}: No tick data")
        return False

    if sym_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        log(f"  {symbol}: Not in FULL trade mode")
        return False

    point   = sym_info.point
    digits  = sym_info.digits
    filling = get_filling_type(symbol)

    order_type = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL

    # ── Entry price: OB midpoint (limit) or market ────────────────────────
    ob  = details.get("ob")
    fvg = details.get("fvg")

    use_limit   = False
    entry_price = round(tick.ask if direction == 1 else tick.bid, digits)

    if ob and not ob.get("mitigated"):
        entry_price = round(ob["mid"], digits)
        use_limit   = True
    elif fvg and not fvg.get("filled"):
        entry_price = round(fvg["mid"], digits)
        use_limit   = True

    # Validate limit entry is below/above current price
    if use_limit:
        if direction == 1 and entry_price >= tick.ask:
            entry_price = round(tick.ask, digits)
            use_limit = False
        if direction == -1 and entry_price <= tick.bid:
            entry_price = round(tick.bid, digits)
            use_limit = False

    # ── SL from swing ─────────────────────────────────────────────────────
    swing_sl = details.get("swing_sl", 0.0)
    if swing_sl and swing_sl > 0:
        sl_price, sl_points = sl_from_swing(order_type, entry_price,
                                             swing_sl, sym_info, symbol)
    else:
        sl_points = max(DEFAULT_SL_POINTS, MIN_SL_POINTS)
        sl_price  = None

    sl_points = max(sl_points, MIN_SL_POINTS)

    # ── Lot size ──────────────────────────────────────────────────────────
    lot = calculate_lot_size(symbol, sl_points, score)
    lot = max(lot, sym_info.volume_min)

    # ── TP at 1:3 RR (remainder after partials runs here) ─────────────────
    tp_ratio   = 3.0
    sl_calc, tp_price = calculate_sl_tp(order_type, entry_price,
                                         sl_points, tp_ratio, sym_info)
    if sl_price is None:
        sl_price = sl_calc

    # ── Validate stop levels ──────────────────────────────────────────────
    sl_price, tp_price = validate_sl_tp(symbol, entry_price,
                                         sl_price, tp_price,
                                         order_type, sym_info)

    # ── Max slippage per symbol type ──────────────────────────────────────
    if symbol in SYNTHETIC_SYMBOLS:
        deviation = 30
    elif "XAU" in symbol or "Gold" in symbol:
        deviation = 150
    else:
        deviation = 50

    comment = (f"NGAO {'B' if direction==1 else 'S'} "
               f"s{score:.0f} {'L' if use_limit else 'M'}")

    if use_limit:
        ltype = mt5.ORDER_TYPE_BUY_LIMIT if direction == 1 \
                else mt5.ORDER_TYPE_SELL_LIMIT
        request = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       float(lot),
            "type":         ltype,
            "price":        entry_price,
            "sl":           sl_price,
            "tp":           tp_price,
            "deviation":    deviation,
            "magic":        MAGIC_NUMBER,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
    else:
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       float(lot),
            "type":         order_type,
            "price":        entry_price,
            "sl":           sl_price,
            "tp":           tp_price,
            "deviation":    deviation,
            "magic":        MAGIC_NUMBER,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

    # ── Pre-check ─────────────────────────────────────────────────────────
    check = mt5.order_check(request)
    if check is None:
        log(f"  {symbol}: order_check returned None. {mt5.last_error()}")
        return False

    if check.retcode != 0:
        log(f"  {symbol}: Check fail {check.retcode} — {check.comment}")
        if check.retcode == 10016:   # Invalid stops
            sl_points = max(sl_points * 1.5, MIN_SL_POINTS)
            sl_price, tp_price = calculate_sl_tp(order_type, entry_price,
                                                  sl_points, tp_ratio, sym_info)
            request["sl"] = sl_price
            request["tp"] = tp_price
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                log(f"  {symbol}: Still failing after SL widening")
                return False
        elif check.retcode == 10019:  # Not enough margin
            request["volume"] = float(sym_info.volume_min)
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                log(f"  {symbol}: Not enough margin for min lot")
                return False
        elif check.retcode == 10018:
            log(f"  {symbol}: Market closed")
            return False
        else:
            return False

    # ── Send ──────────────────────────────────────────────────────────────
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  ✅ {symbol} {'BUY' if direction==1 else 'SELL'} "
            f"{'LIMIT' if use_limit else 'MKT'} "
            f"@ {entry_price} SL={sl_price} TP={tp_price} "
            f"Lot={lot} Score={score:.1f}")
        tg_queue(
            f"{'🟢' if direction==1 else '🔴'} <b>NGAO TRADE</b>\n"
            f"<b>{symbol}</b> {'BUY' if direction==1 else 'SELL'} "
            f"{'(LIMIT)' if use_limit else '(MARKET)'}\n"
            f"Entry: <code>{entry_price}</code>\n"
            f"SL: <code>{sl_price}</code>\n"
            f"TP: <code>{tp_price}</code>\n"
            f"Lot: <code>{lot}</code>  Score: <code>{score:.1f}/10</code>"
        )
        sym_tracker.record_entry(symbol)
        return True

    rc  = result.retcode if result else "None"
    log(f"  ❌ {symbol}: Send failed retcode={rc}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP RECONCILIATION
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_on_startup() -> None:
    log("Reconciling state on startup...")
    positions = mt5.positions_get()
    if positions:
        for p in positions:
            if p.magic == MAGIC_NUMBER:
                sym_tracker.record_entry(p.symbol)
                log(f"  Reconciled: {p.symbol} #{p.ticket} P/L={p.profit:.2f}")

    orders = mt5.orders_get()
    if orders:
        for o in orders:
            if getattr(o, "magic", 0) == MAGIC_NUMBER:
                log(f"  Pending: {o.symbol} #{o.ticket}")


# ─────────────────────────────────────────────────────────────────────────────
#  MILESTONE TRACKER
# ─────────────────────────────────────────────────────────────────────────────

_hit_milestones: set = set()

def check_milestones(balance: float) -> None:
    for m in BALANCE_MILESTONES:
        if balance >= m and m not in _hit_milestones:
            _hit_milestones.add(m)
            msg = f"🎯 MILESTONE: ${m:.0f} reached! Balance: ${balance:.2f}"
            log(msg)
            tg_queue(msg)


# ─────────────────────────────────────────────────────────────────────────────
#  DAILY PERFORMANCE REPORT
# ─────────────────────────────────────────────────────────────────────────────

_last_report_day: str = ""

def maybe_send_daily_report(balance: float) -> None:
    global _last_report_day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today == _last_report_day:
        return
    _last_report_day = today

    pnl     = daily_tracker.daily_pnl(balance)
    pnl_pct = (pnl / daily_tracker.day_start_balance * 100.0
               if daily_tracker.day_start_balance > 0 else 0)
    risk    = get_risk_percent(balance)

    tg_queue(
        f"📅 <b>DAILY REPORT</b>\n"
        f"Start: ${daily_tracker.day_start_balance:.2f}\n"
        f"Now: ${balance:.2f}\n"
        f"P/L: ${pnl:.2f} ({pnl_pct:+.1f}%)\n"
        f"Risk tier: {risk}%/trade\n"
        f"Peak: ${peak_monitor.peak:.2f}"
    )
    tg_flush()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN SCAN CYCLE
# ─────────────────────────────────────────────────────────────────────────────

def run_scan_cycle() -> None:
    log("\n=== NGAO Scan ===")

    acc = mt5.account_info()
    if not acc:
        log("  [ERROR] No account info")
        return

    balance = acc.balance
    equity  = acc.equity

    log(f"  Balance=${balance:.2f}  Equity=${equity:.2f}  "
        f"Free=${acc.margin_free:.2f}  Risk={get_risk_percent(balance)}%")

    # ── Update trackers ───────────────────────────────────────────────────
    daily_tracker.update(balance)
    weekly_tracker.update(equity)
    peak_monitor.update(equity)

    # ── LEVEL 6: Peak equity emergency ───────────────────────────────────
    if peak_monitor.is_emergency(equity):
        log("  🚨 EMERGENCY: Peak equity drop exceeded. Closing all.")
        close_all_positions("Emergency peak drop")
        tg_queue(f"🚨 <b>EMERGENCY STOP</b>\n"
                 f"Peak: ${peak_monitor.peak:.2f}\n"
                 f"Now: ${equity:.2f}")
        set_state("STOPPED")
        return

    # ── LEVEL 4: Weekly halt ──────────────────────────────────────────────
    if weekly_tracker.is_limit_hit(equity):
        log("  ⛔ Weekly drawdown limit hit. Halting until manual reset.")
        tg_queue(f"⛔ <b>WEEKLY HALT</b>\n"
                 f"Drawdown limit reached.\nManual restart required.")
        set_state("PAUSED")
        return

    # ── LEVEL 3: Daily halt ───────────────────────────────────────────────
    if daily_tracker.is_limit_hit(balance):
        log("  🛑 Daily loss limit hit. Pausing until tomorrow.")
        tg_queue(f"🛑 <b>DAILY HALT</b>\nLoss limit reached. Resuming tomorrow.")
        set_state("PAUSED")
        return

    check_milestones(balance)
    maybe_send_daily_report(balance)

    # ── Manage existing positions ─────────────────────────────────────────
    manage_open_positions()
    manage_pending_orders()

    # ── Count open positions ──────────────────────────────────────────────
    open_count = sum(
        1 for p in (mt5.positions_get() or [])
        if p.magic == MAGIC_NUMBER
    )
    if open_count >= MAX_CONCURRENT_TRADES:
        log(f"  Max trades ({MAX_CONCURRENT_TRADES}) active — skipping scan")
        return

    # ── Scan + score all symbols ──────────────────────────────────────────
    setups = []

    for symbol in SYMBOLS:
        if not mt5.symbol_select(symbol, True):
            continue

        if not is_tradeable_session(symbol):
            continue

        if not passes_spread_filter(symbol):
            log(f"  {symbol}: Spread too wide")
            continue

        can, reason = sym_tracker.can_trade(symbol)
        if not can:
            log(f"  {symbol}: {reason}")
            continue

        # Check no existing position on this symbol
        existing = [p for p in (mt5.positions_get(symbol=symbol) or [])
                    if p.magic == MAGIC_NUMBER]
        if existing:
            continue

        # Fetch data
        data = fetch_all_timeframes(symbol)
        if not data:
            continue

        sym_info = mt5.symbol_info(symbol)
        if not sym_info:
            continue

        spread_pts = get_spread_points(symbol)

        direction, score, details = detect_scalp_signal(
            symbol, data, sym_info.point, spread_pts
        )

        if direction == 0 or score < MIN_SIGNAL_SCORE_HALF:
            reason = details.get("reason", f"Score {score:.1f}")
            log(f"  {symbol}: {reason}")
            continue

        log(f"  {symbol}: {'BUY' if direction==1 else 'SELL'} "
            f"Score={score:.1f}/10 | "
            f"HA_D={details.get('ha_daily')} "
            f"HA_H4={details.get('ha_h4')} | "
            f"OB={'✓' if details.get('ob') else '✗'} "
            f"FVG={'✓' if details.get('fvg') else '✗'} "
            f"Sweep={'✓' if details.get('sweep_m15') else '✗'} "
            f"CHoCH={'✓' if details.get('m1_choch') else '✗'}")

        setups.append((symbol, direction, score, details))

    # ── Sort by score descending ──────────────────────────────────────────
    setups.sort(key=lambda x: x[2], reverse=True)

    # ── Execute top setups ────────────────────────────────────────────────
    for symbol, direction, score, details in setups:
        if open_count >= MAX_CONCURRENT_TRADES:
            break

        if not passes_correlation_filter(symbol, direction):
            log(f"  {symbol}: Correlation filter — skipping")
            continue

        if score >= MIN_SIGNAL_SCORE:
            success = place_trade(symbol, direction, score, details)
            if success:
                open_count += 1

    tg_flush()


# ─────────────────────────────────────────────────────────────────────────────
#  CANDLE CLOCK — run scan only on new M5 candle
# ─────────────────────────────────────────────────────────────────────────────

_last_candle_time: int = 0

def is_new_candle() -> bool:
    global _last_candle_time
    rates = mt5.copy_rates_from_pos("EURUSD", TF_M5, 0, 1)
    if rates is None or len(rates) == 0:
        rates = mt5.copy_rates_from_pos(
            "Volatility 25 Index", TF_M5, 0, 1)
    if rates is None or len(rates) == 0:
        return False
    t = int(rates[0]["time"])
    if t != _last_candle_time:
        _last_candle_time = t
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def start_bot() -> None:
    if not mt5.initialize():
        print("Failed to initialise MT5")
        return

    LOGIN    = int(os.getenv("MT5_LOGIN",    0))
    PASSWORD = os.getenv("MT5_PASSWORD", "")
    SERVER   = os.getenv("MT5_SERVER",   "")

    if not LOGIN or not PASSWORD or not SERVER:
        print("ERROR: MT5 credentials missing from .env")
        mt5.shutdown()
        return

    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        print(f"MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return

    acc = mt5.account_info()
    log("=" * 60)
    log("NGAO Scalper v3.0 — Pure PA | APA/SMC | HA Bias")
    log(f"Account: {LOGIN} @ {SERVER}")
    log(f"Balance: ${acc.balance:.2f}  Leverage: 1:{acc.leverage}")
    log(f"Symbols: {', '.join(SYMBOLS)}")
    log(f"Risk tier: {get_risk_percent(acc.balance)}%/trade")
    log("=" * 60)

    reconcile_on_startup()

    tg_queue(
        f"🤖 <b>NGAO Scalper v3.0 STARTED</b>\n"
        f"Balance: ${acc.balance:.2f}\n"
        f"Symbols: {len(SYMBOLS)}\n"
        f"Risk: {get_risk_percent(acc.balance)}%/trade"
    )
    tg_flush()

    try:
        while get_state() != "STOPPED":
            if get_state() == "PAUSED":
                # Resume automatically at new day
                now_utc = datetime.now(timezone.utc)
                if daily_tracker._last_reset_date != now_utc.strftime("%Y-%m-%d"):
                    log("  New day — resuming from daily halt")
                    set_state("RUNNING")
                else:
                    log("  [PAUSED] Waiting...")
                    time.sleep(SCAN_INTERVAL_SECS)
                    continue

            try:
                if is_new_candle():
                    run_scan_cycle()
                else:
                    # Still manage positions on every tick cycle
                    manage_open_positions()
            except Exception as e:
                log(f"  [CYCLE ERROR] {type(e).__name__}: {e}")

            time.sleep(SCAN_INTERVAL_SECS)

    except KeyboardInterrupt:
        log("Bot stopped by user.")
    finally:
        mt5.shutdown()
        log("MT5 shutdown complete.")


if __name__ == "__main__":
    start_bot()
