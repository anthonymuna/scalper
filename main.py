"""
main.py — NGAO Scalper Bot v4.0
================================
Dual Engine: APA/SMC + ICT — run independently, both can fire trades.

APA Engine:  HA Daily/H4 bias → H1 OB/FVG → M15 sweep → M5 BOS → M1 CHoCH
ICT Engine:  HA Daily/H4 bias → IPDA levels → AMD phase → Killzone →
             OTE 0.62–0.79 → Breaker/Mitigation → Silver Bullet FVG

Both engines scan every symbol every M5 candle.
Higher-scoring engine wins when both fire on same symbol same candle.
All risk management, guardrails, and trade execution are shared.
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
    SNIPER_MODE_ENABLED, MAX_SNIPER_CANDLES,
    MIN_SNIPER_PROFIT_PTS, SNIPER_SYMBOLS,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_FLUSH_SECS,
    get_risk_percent,
)
from strategy import (
    apply_vp_to_apa_signal,
    detect_scalp_signal,
    get_structure_trail_sl,
    get_ha_bias,
)
from ict_strategy import (
    apply_vp_to_ict_signal,
    detect_judas_swing,
    detect_ict_signal,
    get_active_killzone,
    ICT_MIN_SCORE,
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

_tg_queue:     list  = []
_tg_last_sent: float = 0.0
_tg_lock              = threading.Lock()

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
    if symbol in SYNTHETIC_SYMBOLS:
        return "synthetic"
    hour = datetime.now(timezone.utc).hour
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
    if ASIAN_START <= hour or hour < ASIAN_END:
        return "asian_skip"
    return "dead"

def is_tradeable_session(symbol: str) -> bool:
    return get_session(symbol) not in ("dead", "asian_skip")


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
        return True
    max_spread = SYMBOL_MAX_SPREAD.get(symbol, 100)
    if max_spread == 0:
        return True
    return get_spread_points(symbol) <= max_spread


# ─────────────────────────────────────────────────────────────────────────────
#  CORRELATION FILTER
# ─────────────────────────────────────────────────────────────────────────────

def passes_correlation_filter(symbol: str, direction: int) -> bool:
    is_usd = any(x in symbol for x in ["EUR", "GBP"])
    if not is_usd:
        return True
    positions = mt5.positions_get()
    if not positions:
        return True
    for p in positions:
        if p.magic != MAGIC_NUMBER:
            continue
        open_dir = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        if any(x in p.symbol for x in ["EUR", "GBP"]) and open_dir == direction:
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
_sniper_tracker: dict = {}  # {ticket: {candles,engine,entry_price,symbol,direction}}

def close_partial(ticket: int, percent: float, symbol: str) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    total_vol = pos[0].volume
    close_vol = round(total_vol * percent / 100.0, 2)
    sym_info  = mt5.symbol_info(symbol)
    if not sym_info:
        return False
    vol_min  = sym_info.volume_min
    vol_step = sym_info.volume_step
    close_vol = max(vol_min, close_vol)
    close_vol = round(round(close_vol / vol_step) * vol_step, 2)
    if close_vol >= total_vol:
        return False
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
        "type_filling": get_filling_type(pos.symbol),
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
#  MANAGE OPEN POSITIONS
# ─────────────────────────────────────────────────────────────────────────────


def manage_sniper_positions() -> None:
    """
    ICT Sniper Mode — profit-only candle-based exit.

    Every M5 candle for registered ICT trades:
      - If profit >= MIN_SNIPER_PROFIT_PTS  -> close trade (profit locked)
      - If not in profit                    -> hold, check next candle
      - If candles >= MAX_SNIPER_CANDLES    -> hand to normal TP/SL management
    Never closes at a loss within the sniper window.
    """
    if not SNIPER_MODE_ENABLED or not _sniper_tracker:
        return

    to_remove = []

    for ticket, info in list(_sniper_tracker.items()):
        pos_list = mt5.positions_get(ticket=ticket)
        if not pos_list:
            to_remove.append(ticket)
            continue

        pos       = pos_list[0]
        symbol    = pos.symbol
        direction = info["direction"]
        info["candles"] += 1
        candles   = info["candles"]

        sym_info = mt5.symbol_info(symbol)
        if not sym_info:
            continue
        point = sym_info.point
        tick  = mt5.symbol_info_tick(symbol)
        if not tick:
            continue

        cur   = tick.bid if direction == 1 else tick.ask
        entry = info["entry_price"]
        pts   = ((cur - entry) * direction) / point if point > 0 else 0

        side = "SHORT" if direction == -1 else "LONG"
        log("[SNIPER] #" + str(ticket) + " " + symbol + " " + side
            + " c" + str(candles) + "/" + str(MAX_SNIPER_CANDLES)
            + " profit=" + str(round(pts, 1)) + "pts")

        if pts >= MIN_SNIPER_PROFIT_PTS:
            if close_position(pos, "Sniper profit exit c" + str(candles)):
                pnl = pos.profit
                tg_queue(
                    "[SNIPER EXIT] " + side + " " + symbol + "\n"
                    "Profit: " + str(round(pts, 1)) + "pts | $"
                    + str(round(pnl, 2)) + "\n"
                    "Candles: " + str(candles)
                )
                to_remove.append(ticket)
            continue

        if candles >= MAX_SNIPER_CANDLES:
            log("[SNIPER] " + symbol + " max candles — handing to normal mgmt")
            tg_queue(
                "[SNIPER->NORMAL] " + symbol + " " + side
                + " c" + str(candles) + " profit=" + str(round(pts, 1)) + "pts"
            )
            to_remove.append(ticket)

    for t in to_remove:
        _sniper_tracker.pop(t, None)

def manage_open_positions() -> None:
    manage_sniper_positions()

    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        if pos.magic != MAGIC_NUMBER:
            continue

        symbol     = pos.symbol
        ticket     = pos.ticket
        direction  = 1 if pos.type == mt5.ORDER_TYPE_BUY else -1
        open_price = pos.price_open
        current_sl = pos.sl
        sl_dist    = abs(open_price - current_sl)

        sym_info = mt5.symbol_info(symbol)
        if not sym_info:
            continue

        point  = sym_info.point
        digits = sym_info.digits
        tick   = mt5.symbol_info_tick(symbol)
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
            if close_partial(ticket, TP1_PCT, symbol):
                _tp1_done.add(ticket)
                tg_queue(f"✅ TP1: {symbol} | 30% closed | RR 1:1")
            stop_level = sym_info.trade_stops_level * point
            new_sl     = round(open_price, digits)
            be_ok = (
                (direction ==  1 and new_sl > current_sl and
                 current_price - new_sl > stop_level) or
                (direction == -1 and new_sl < current_sl and
                 new_sl - current_price > stop_level)
            )
            if be_ok:
                mt5.order_send({
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "symbol":   symbol,
                    "position": ticket,
                    "sl":       new_sl,
                    "tp":       pos.tp,
                })

        # ── TP2: close 40% ───────────────────────────────────────────────
        if rr >= TP2_RR and ticket not in _tp2_done:
            if close_partial(ticket, TP2_PCT, symbol):
                _tp2_done.add(ticket)
                tg_queue(f"✅ TP2: {symbol} | 40% closed | RR 1:2")

        # ── STRUCTURE TRAILING STOP (after TP2) ──────────────────────────
        if rr >= TP2_RR:
            df_m5 = get_df(symbol, TF_M5, 20)
            if df_m5 is not None:
                trail_sl     = get_structure_trail_sl(df_m5, direction)
                stop_level   = sym_info.trade_stops_level * point
                freeze_level = sym_info.trade_freeze_level * point
                near_freeze  = abs(current_price - current_sl) <= freeze_level
                if trail_sl > 0 and not near_freeze:
                    trail_valid = (
                        (direction ==  1 and trail_sl > current_sl and
                         current_price - trail_sl > stop_level) or
                        (direction == -1 and trail_sl < current_sl and
                         trail_sl - current_price > stop_level)
                    )
                    if trail_valid:
                        mt5.order_send({
                            "action":   mt5.TRADE_ACTION_SLTP,
                            "symbol":   symbol,
                            "position": ticket,
                            "sl":       round(trail_sl, digits),
                            "tp":       pos.tp,
                        })

        # ── CHoCH EXIT ────────────────────────────────────────────────────
        if rr > 0.5:
            df_m5 = get_df(symbol, TF_M5, 20)
            if df_m5 is not None:
                from strategy import detect_bos_choch
                m5s  = detect_bos_choch(df_m5, lookback=10)
                flip = ((direction ==  1 and m5s["choch_bear"]) or
                        (direction == -1 and m5s["choch_bull"]))
                if flip:
                    if close_position(pos, "CHoCH flip"):
                        tg_queue(f"🔄 CHoCH EXIT: {symbol} | RR: {rr:.2f}")

        # ── POC STALL EXIT — price stuck at POC with no momentum ─────────
        # Only check after TP1 hit (we're at breakeven, no risk to close)
        if ticket in _tp1_done and rr > 0.3:
            df_m5 = get_df(symbol, TF_M5, 10)
            if df_m5 is not None:
                from volume_profile import build_vp_stack, check_poc_stall
                sym_data_mini = {"h1": get_df(symbol, TF_H1, 100),
                                 "m15": get_df(symbol, TF_M15, 100)}
                vp_mini = build_vp_stack(sym_data_mini, sym_info.point)
                if check_poc_stall(vp_mini, df_m5, sym_info.point):
                    if close_position(pos, "POC stall exit"):
                        tg_queue(f"🧲 POC STALL EXIT: {symbol} | "
                                 f"RR: {rr:.2f} | POC={vp_mini.get('h1').poc:.5f}"
                                 if vp_mini.get('h1') else
                                 f"🧲 POC STALL EXIT: {symbol}")


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
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_REMOVE,
                "order":  order.ticket,
            })
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"  Expired pending #{order.ticket} {order.symbol}")
                del _pending_candle_count[order.ticket]


# ─────────────────────────────────────────────────────────────────────────────
#  PLACE TRADE — shared by both engines
# ─────────────────────────────────────────────────────────────────────────────

def place_trade(symbol: str, direction: int, score: float,
                details: dict, engine: str = "APA") -> bool:
    sym_info = mt5.symbol_info(symbol)
    if not sym_info:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
    if sym_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        return False

    point    = sym_info.point
    digits   = sym_info.digits
    filling  = get_filling_type(symbol)
    order_type = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL

    # ── Entry price resolution ────────────────────────────────────────────
    # ICT engine provides entry_zone; APA engine provides ob/fvg
    use_limit   = False
    entry_price = round(tick.ask if direction == 1 else tick.bid, digits)

    if engine == "ICT":
        ez = details.get("entry_zone")
        if ez:
            entry_price = round(ez["mid"], digits)
            use_limit   = True
    else:
        ob  = details.get("ob")
        fvg = details.get("fvg")
        if ob and not ob.get("mitigated"):
            entry_price = round(ob["mid"], digits)
            use_limit   = True
        elif fvg and not fvg.get("filled"):
            entry_price = round(fvg["mid"], digits)
            use_limit   = True

    # Validate limit price vs current
    if use_limit:
        if direction == 1 and entry_price >= tick.ask:
            entry_price = round(tick.ask, digits)
            use_limit   = False
        if direction == -1 and entry_price <= tick.bid:
            entry_price = round(tick.bid, digits)
            use_limit   = False

    # ── SL calculation ────────────────────────────────────────────────────
    # ICT: prefer manipulation extreme as SL anchor
    # APA: prefer swing low/high
    swing_sl   = 0.0
    sl_price   = None

    if engine == "ICT":
        manip_sl = details.get("manip_sl", 0.0)
        if manip_sl and manip_sl > 0:
            swing_sl = manip_sl
    else:
        swing_sl = details.get("swing_sl", 0.0)

    if swing_sl and swing_sl > 0:
        sl_price, sl_points = sl_from_swing(order_type, entry_price,
                                             swing_sl, sym_info, symbol)
    else:
        sl_points = max(DEFAULT_SL_POINTS, MIN_SL_POINTS)

    sl_points = max(sl_points, MIN_SL_POINTS)

    # Check minimum stop level
    stop_level_pts = sym_info.trade_stops_level
    min_sl_pts     = stop_level_pts * 1.2
    if sl_points < min_sl_pts:
        sl_points = min_sl_pts

    # ── Lot size ──────────────────────────────────────────────────────────
    lot = calculate_lot_size(symbol, sl_points, score)
    lot = max(lot, sym_info.volume_min)

    # ── TP at 1:3 for remainder after partials ────────────────────────────
    # If ICT has IPDA level, use that as TP3 target
    ipda_tp_level = details.get("ipda_tp_level", 0.0)
    sl_calc, tp_price = calculate_sl_tp(order_type, entry_price,
                                         sl_points, 3.0, sym_info)
    if sl_price is None:
        sl_price = sl_calc

    # Override TP with best available institutional level:
    # IPDA draw level or VP key level — whichever is closer in trade direction
    vp_tp_level = details.get("vp_tp_level", 0.0)
    candidates  = []
    for lvl in [ipda_tp_level, vp_tp_level]:
        if lvl <= 0:
            continue
        if direction == 1 and lvl > entry_price:
            candidates.append(lvl)
        elif direction == -1 and lvl < entry_price:
            candidates.append(lvl)
    if candidates:
        # Use nearest level in direction (first meaningful TP)
        best_tp = min(candidates) if direction == 1 else max(candidates)
        tp_price = round(best_tp, digits)
        log(f"  TP override: {best_tp:.5f} "
            f"(IPDA={ipda_tp_level:.5f} VP={vp_tp_level:.5f})")

    # Validate SL/TP against broker stop levels
    sl_price, tp_price = validate_sl_tp(symbol, entry_price,
                                         sl_price, tp_price,
                                         order_type, sym_info)

    # Slippage by symbol type
    if symbol in SYNTHETIC_SYMBOLS:
        deviation = 30
    elif "XAU" in symbol or "Gold" in symbol:
        deviation = 150
    else:
        deviation = 50

    comment = (f"NGAO-{engine} {'B' if direction==1 else 'S'} "
               f"s{score:.0f}")

    ltype = (mt5.ORDER_TYPE_BUY_LIMIT if direction == 1
             else mt5.ORDER_TYPE_SELL_LIMIT)

    request = {
        "action":       mt5.TRADE_ACTION_PENDING if use_limit else mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       float(lot),
        "type":         ltype if use_limit else order_type,
        "price":        entry_price,
        "sl":           sl_price,
        "tp":           tp_price,
        "deviation":    deviation,
        "magic":        MAGIC_NUMBER,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    # Pre-check
    check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        rc = check.retcode if check else "None"
        log(f"  {symbol} [{engine}]: Check fail {rc}")
        if check and check.retcode == 10016:
            sl_points *= 1.5
            sl_calc, tp_price = calculate_sl_tp(order_type, entry_price,
                                                 sl_points, 3.0, sym_info)
            request["sl"] = sl_calc
            request["tp"] = tp_price
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                return False
        else:
            return False

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        emoji = "🟢" if direction == 1 else "🔴"
        side  = "BUY"  if direction == 1 else "SELL"
        mode  = "LIMIT" if use_limit else "MKT"
        log(f"  ✅ [{engine}] {symbol} {side} {mode} "
            f"@ {entry_price} SL={sl_price} TP={tp_price} "
            f"Lot={lot} Score={score:.1f}")
        tg_queue(
            f"{emoji} <b>NGAO-{engine}</b>\n"
            f"<b>{symbol}</b> {side} ({mode})\n"
            f"Entry: <code>{entry_price}</code>\n"
            f"SL:    <code>{sl_price}</code>\n"
            f"TP:    <code>{tp_price}</code>\n"
            f"Lot:   <code>{lot}</code>  "
            f"Score: <code>{score:.1f}/10</code>"
        )
        sym_tracker.record_entry(symbol)
        if (SNIPER_MODE_ENABLED and engine == 'ICT'
                and symbol in SNIPER_SYMBOLS):
            new_ticket = result.order if hasattr(result, 'order') else 0
            if new_ticket > 0:
                _sniper_tracker[new_ticket] = {
                    'candles': 0, 'engine': engine,
                    'entry_price': entry_price, 'symbol': symbol,
                    'direction': direction,
                }
                log('[SNIPER] ' + symbol
                    + ' registered ticket=' + str(new_ticket))
        return True

    rc = result.retcode if result else "None"
    log(f"  ❌ [{engine}] {symbol}: retcode={rc}")
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
                log(f"  Reconciled: {p.symbol} #{p.ticket} "
                    f"P/L={p.profit:.2f}")
    orders = mt5.orders_get()
    if orders:
        for o in orders:
            if getattr(o, "magic", 0) == MAGIC_NUMBER:
                log(f"  Pending: {o.symbol} #{o.ticket}")


# ─────────────────────────────────────────────────────────────────────────────
#  MILESTONE + DAILY REPORT
# ─────────────────────────────────────────────────────────────────────────────

_hit_milestones: set = set()

def check_milestones(balance: float) -> None:
    for m in BALANCE_MILESTONES:
        if balance >= m and m not in _hit_milestones:
            _hit_milestones.add(m)
            msg = f"🎯 MILESTONE: ${m:.0f} reached! Balance: ${balance:.2f}"
            log(msg); tg_queue(msg)

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
    tg_queue(
        f"📅 <b>DAILY REPORT</b>\n"
        f"Start: ${daily_tracker.day_start_balance:.2f}\n"
        f"Now:   ${balance:.2f}\n"
        f"P/L:   ${pnl:.2f} ({pnl_pct:+.1f}%)\n"
        f"Risk:  {get_risk_percent(balance)}%/trade\n"
        f"Peak:  ${peak_monitor.peak:.2f}"
    )
    tg_flush()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN SCAN CYCLE — DUAL ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_scan_cycle() -> None:
    log("\n=== NGAO Dual-Engine Scan ===")

    acc = mt5.account_info()
    if not acc:
        log("  [ERROR] No account info"); return

    balance = acc.balance
    equity  = acc.equity

    log(f"  Balance=${balance:.2f}  Equity=${equity:.2f}  "
        f"Risk={get_risk_percent(balance)}%  "
        f"KZ={get_active_killzone()[0] or 'none'}")

    # ── Update trackers ───────────────────────────────────────────────────
    daily_tracker.update(balance)
    weekly_tracker.update(equity)
    peak_monitor.update(equity)

    # ── Guardrails ────────────────────────────────────────────────────────
    if peak_monitor.is_emergency(equity):
        log("  🚨 EMERGENCY: Peak drop exceeded.")
        close_all_positions("Emergency peak drop")
        tg_queue(f"🚨 <b>EMERGENCY STOP</b>\nPeak: ${peak_monitor.peak:.2f} "
                 f"→ Now: ${equity:.2f}")
        set_state("STOPPED"); return

    if weekly_tracker.is_limit_hit(equity):
        log("  ⛔ Weekly drawdown limit hit.")
        tg_queue("⛔ <b>WEEKLY HALT</b>\nManual restart required.")
        set_state("PAUSED"); return

    if daily_tracker.is_limit_hit(balance):
        log("  🛑 Daily loss limit hit.")
        tg_queue("🛑 <b>DAILY HALT</b>\nResumes tomorrow.")
        set_state("PAUSED"); return

    check_milestones(balance)
    maybe_send_daily_report(balance)

    # ── Position management ───────────────────────────────────────────────
    manage_open_positions()
    manage_pending_orders()

    open_count = sum(
        1 for p in (mt5.positions_get() or [])
        if p.magic == MAGIC_NUMBER
    )
    if open_count >= MAX_CONCURRENT_TRADES:
        log(f"  Max trades ({MAX_CONCURRENT_TRADES}) active — skipping scan")
        return

    # ── DUAL ENGINE SCAN ─────────────────────────────────────────────────
    # Each symbol gets scored by both engines.
    # Structure: {symbol: {"APA": (dir, score, details),
    #                      "ICT": (dir, score, details)}}

    all_setups: list[tuple] = []
    # each entry: (symbol, direction, score, details, engine_name)

    for symbol in SYMBOLS:
        if not mt5.symbol_select(symbol, True):
            continue
        if not is_tradeable_session(symbol):
            continue
        if not passes_spread_filter(symbol):
            continue
        can, reason = sym_tracker.can_trade(symbol)
        if not can:
            log(f"  {symbol}: {reason}"); continue
        existing = [p for p in (mt5.positions_get(symbol=symbol) or [])
                    if p.magic == MAGIC_NUMBER]
        if existing:
            continue

        data = fetch_all_timeframes(symbol)
        if not data:
            continue

        sym_info = mt5.symbol_info(symbol)
        if not sym_info:
            continue

        spread_pts = get_spread_points(symbol)

        # ── VOLUME PROFILE — built once, shared by both engines ──────────
        from volume_profile import build_vp_stack
        vp_stack = build_vp_stack(data, sym_info.point)
        vp_h1 = vp_stack.get("h1")
        if vp_h1 and vp_h1.valid:
            log(f"  [VP] {symbol}: POC={vp_h1.poc:.5f} "
                f"VAH={vp_h1.vah:.5f} VAL={vp_h1.val:.5f} "
                f"HVN={len(vp_h1.hvn_prices)} LVN={len(vp_h1.lvn_prices)}")

        # Pre-compute HA bias once — shared by both engines
        ha_daily = get_ha_bias(data["d1"])
        ha_h4    = get_ha_bias(data["h4"])

        # ── APA ENGINE ───────────────────────────────────────────────────
        apa_dir, apa_score, apa_details = detect_scalp_signal(
            symbol, data, sym_info.point, spread_pts
        )
        if apa_dir != 0 and apa_score >= MIN_SIGNAL_SCORE_HALF:
            # Apply Volume Profile confluence to APA score
            vp_delta, vp_bd = apply_vp_to_apa_signal(
                apa_details, vp_stack, sym_info.point)
            apa_score   += vp_delta
            apa_details["vp"]          = vp_bd
            apa_details["vp_tp_level"] = vp_bd.get("vp_tp_level", 0.0)
            vp_tp = apa_details["vp_tp_level"]
            log(f"  [APA] {symbol}: {'BUY' if apa_dir==1 else 'SELL'} "
                f"Score={apa_score:.1f} (VP{vp_delta:+.1f}) | "
                f"OB={'✓' if apa_details.get('ob') else '✗'} "
                f"FVG={'✓' if apa_details.get('fvg') else '✗'} "
                f"Sweep={'✓' if apa_details.get('sweep_m15') else '✗'} "
                f"CHoCH={'✓' if apa_details.get('m1_choch') else '✗'} "
                f"VP_TP={vp_tp:.5f}")
            all_setups.append((symbol, apa_dir, apa_score, apa_details, "APA"))
        else:
            reason = apa_details.get("reason", f"Score {apa_score:.1f}")
            log(f"  [APA] {symbol}: {reason}")

        # ── ICT ENGINE ───────────────────────────────────────────────────
        ict_dir, ict_score, ict_details = detect_ict_signal(
            symbol, data, sym_info.point, ha_daily, ha_h4
        )
        if ict_dir != 0 and ict_score >= ICT_MIN_SCORE / 2:
            # Apply Volume Profile confluence to ICT score
            vp_delta, vp_bd = apply_vp_to_ict_signal(
                ict_details, vp_stack, sym_info.point)
            ict_score   += vp_delta
            ict_details["vp"]          = vp_bd
            ict_details["vp_tp_level"] = vp_bd.get("vp_tp_level", 0.0)
            kz    = ict_details.get("killzone", "")
            sb    = "⚡SB" if ict_details.get("is_silver_bullet") else ""
            vp_tp = ict_details["vp_tp_level"]
            log(f"  [ICT] {symbol}: {'BUY' if ict_dir==1 else 'SELL'} "
                f"Score={ict_score:.1f} (VP{vp_delta:+.1f}) | "
                f"KZ={kz}{sb} "
                f"AMD={ict_details.get('amd', {}).get('phase','?')} "
                f"OTE={'✓' if ict_details.get('ote') else '✗'} "
                f"BB={'✓' if ict_details.get('breaker') else '✗'} "
                f"SB_FVG={'✓' if ict_details.get('sb_fvg') else '✗'} "
                f"Judas={'✓' if ict_details.get('judas',{}).get('detected') else '✗'} "
                f"Premium={'✓' if ict_details.get('in_premium') else '✗'} "
                f"VP_TP={vp_tp:.5f}")
            all_setups.append((symbol, ict_dir, ict_score, ict_details, "ICT"))
        else:
            reason = ict_details.get("reason", f"ICT Score {ict_score:.1f}")
            log(f"  [ICT] {symbol}: {reason}")

    # ── RESOLVE CONFLICTS: same symbol, both engines fire ─────────────────
    # Group by symbol, pick highest score
    best_by_symbol: dict[str, tuple] = {}
    for setup in all_setups:
        sym = setup[0]
        if sym not in best_by_symbol or setup[2] > best_by_symbol[sym][2]:
            best_by_symbol[sym] = setup

    # Sort by score descending
    ranked = sorted(best_by_symbol.values(), key=lambda x: x[2], reverse=True)

    # ── EXECUTE ───────────────────────────────────────────────────────────
    for symbol, direction, score, details, engine in ranked:
        if open_count >= MAX_CONCURRENT_TRADES:
            break
        if not passes_correlation_filter(symbol, direction):
            log(f"  {symbol}: Correlation filter — skipping"); continue

        min_score = MIN_SIGNAL_SCORE if engine == "APA" else ICT_MIN_SCORE
        if score < min_score / 2:   # allow half-lot for borderline
            continue

        success = place_trade(symbol, direction, score, details, engine)
        if success:
            open_count += 1

    tg_flush()


# ─────────────────────────────────────────────────────────────────────────────
#  CANDLE CLOCK
# ─────────────────────────────────────────────────────────────────────────────

_last_candle_time: int = 0

def is_new_candle() -> bool:
    global _last_candle_time
    # Try real symbol first, fall back to synthetic
    for sym in ("EURUSD", "Volatility 25 Index"):
        rates = mt5.copy_rates_from_pos(sym, TF_M5, 0, 1)
        if rates and len(rates) > 0:
            t = int(rates[0]["time"])
            if t != _last_candle_time:
                _last_candle_time = t
                return True
            return False
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def start_bot() -> None:
    if not mt5.initialize():
        print("Failed to initialise MT5"); return

    LOGIN    = int(os.getenv("MT5_LOGIN",    0))
    PASSWORD = os.getenv("MT5_PASSWORD", "")
    SERVER   = os.getenv("MT5_SERVER",   "")

    if not LOGIN or not PASSWORD or not SERVER:
        print("ERROR: MT5 credentials missing from .env")
        mt5.shutdown(); return

    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        print(f"MT5 login failed: {mt5.last_error()}")
        mt5.shutdown(); return

    acc = mt5.account_info()
    log("=" * 60)
    log("NGAO Scalper v4.0 — Dual Engine: APA/SMC + ICT")
    log(f"Account : {LOGIN} @ {SERVER}")
    log(f"Balance : ${acc.balance:.2f}  Leverage: 1:{acc.leverage}")
    log(f"Symbols : {', '.join(SYMBOLS)}")
    log(f"Risk    : {get_risk_percent(acc.balance)}%/trade")
    log("Engines : APA/SMC (HA+OB+FVG+Sweep+BOS+CHoCH) | "
        "ICT (IPDA+AMD+OTE+BB+MB+SilverBullet)")
    log("=" * 60)

    reconcile_on_startup()

    tg_queue(
        f"🤖 <b>NGAO Scalper v4.0 STARTED</b>\n"
        f"<b>Dual Engine: APA/SMC + ICT</b>\n"
        f"Balance: ${acc.balance:.2f}\n"
        f"Symbols: {len(SYMBOLS)}\n"
        f"Risk:    {get_risk_percent(acc.balance)}%/trade\n"
        f"ICT: IPDA · AMD · OTE · Breaker · Mitigation · Silver Bullet"
    )
    tg_flush()

    try:
        while get_state() != "STOPPED":
            if get_state() == "PAUSED":
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
