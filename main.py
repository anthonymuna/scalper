"""
main.py — XAUUSD APA Scalping Bot (v2)

New in v2:
  - Credentials loaded from .env
  - Session filter  (London + NY only)
  - Spread guard    (skip if spread > MAX_SPREAD_POINTS)
  - Swing-based SL placement
  - Trailing stop   (software-side, runs every cycle)
  - Partial close   (50 % at 1:1 R:R, let rest trail)
  - Daily loss halt (DailyLossTracker)
  - Bot state machine  (RUNNING / PAUSED / STOPPED)
  - Telegram alert hook (telegram_bot.py)
  - Sniper signal threshold 4/7
"""

import os
import time
import threading
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from config import (
    SYMBOLS, TIMEFRAME_CYCLE, MAGIC_NUMBER,
    MIN_SL_POINTS, DEFAULT_SL_POINTS, TP_RATIO,
    PROFIT_TARGET_PERCENT, TIME_LIMIT_SECONDS,
    SCAN_INTERVAL_SECONDS, MAX_CONCURRENT_TRADES,
    MAX_SPREAD_POINTS, MIN_SIGNAL_STRENGTH,
    TRAILING_ACTIVATION_POINTS, TRAILING_STEP_POINTS,
    PARTIAL_CLOSE_RATIO,
    LONDON_SESSION_START, LONDON_SESSION_END,
    NY_SESSION_START, NY_SESSION_END,
    LONDON_KZ_START, LONDON_KZ_END,
    NY_KZ_START, NY_KZ_END,
    USE_FVG_ENTRY,
    BALANCE_MILESTONES,
)
from strategy import detect_scalp_signal, analyze_timeframe_coordination
from risk import calculate_lot_size, calculate_sl_tp, sl_from_swing, daily_tracker

# ─────────────────────────────────────────────────────────────────────────────
#  Bot state
# ─────────────────────────────────────────────────────────────────────────────
BOT_STATE   = "RUNNING"    # "RUNNING" | "PAUSED" | "STOPPED"
_state_lock = threading.Lock()

def get_state() -> str:
    with _state_lock:
        return BOT_STATE

def set_state(state: str) -> None:
    global BOT_STATE
    with _state_lock:
        BOT_STATE = state
    log(f"[STATE] Bot state changed to: {state}")


# ─────────────────────────────────────────────────────────────────────────────
#  Milestone tracking
# ─────────────────────────────────────────────────────────────────────────────
_hit_milestones: set = set()

def check_milestones(balance: float) -> list:
    """Return any new milestones just crossed."""
    new_hits = []
    for m in BALANCE_MILESTONES:
        if balance >= m and m not in _hit_milestones:
            _hit_milestones.add(m)
            new_hits.append(m)
    return new_hits


# ─────────────────────────────────────────────────────────────────────────────
#  Telegram alert hook (populated by telegram_bot.py at runtime)
# ─────────────────────────────────────────────────────────────────────────────
_tg_alert_fn = None   # Callable[[str], None] | None

def register_telegram_alert(fn) -> None:
    global _tg_alert_fn
    _tg_alert_fn = fn

def tg_alert(msg: str) -> None:
    if _tg_alert_fn:
        try:
            _tg_alert_fn(msg)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Logging
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
#  MT5 helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_df(symbol: str, tf: int, count: int = 100) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def get_filling_type(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    fm = info.filling_mode
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


# ─────────────────────────────────────────────────────────────────────────────
#  Session filter
# ─────────────────────────────────────────────────────────────────────────────

def is_trading_session() -> bool:
    """Return True if within London or NY session (broad outer guard)."""
    hour = datetime.now(timezone.utc).hour
    in_london = LONDON_SESSION_START <= hour < LONDON_SESSION_END
    in_ny     = NY_SESSION_START     <= hour < NY_SESSION_END
    return in_london or in_ny


def is_killzone() -> bool:
    """
    Return True if we're in a high-probability killzone:
      - London open:  07:00–09:00 UTC
      - NY open:      13:00–15:00 UTC

    Killzone trades get a bonus score point in detect_scalp_signal.
    Trades outside killzones but inside session are still valid
    if they score high enough without the bonus.
    """
    hour = datetime.now(timezone.utc).hour
    in_london_kz = LONDON_KZ_START <= hour < LONDON_KZ_END
    in_ny_kz     = NY_KZ_START     <= hour < NY_KZ_END
    return in_london_kz or in_ny_kz


# ─────────────────────────────────────────────────────────────────────────────
#  Spread guard
# ─────────────────────────────────────────────────────────────────────────────

def get_spread_points(symbol: str) -> float:
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        return 0.0
    return (tick.ask - tick.bid) / info.point


# ─────────────────────────────────────────────────────────────────────────────
#  Position management
# ─────────────────────────────────────────────────────────────────────────────

def close_position(pos, reason: str) -> bool:
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        log(f"  -> [ERROR] No tick to close #{pos.ticket}")
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
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      reason[:31],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [CLOSED] #{pos.ticket} {pos.symbol}. {reason}. P&L=${pos.profit:.2f}")
        tg_alert(
            f"🔒 *Trade Closed*\n"
            f"`{pos.symbol}` #{pos.ticket}\n"
            f"Reason: {reason}\n"
            f"P&L: `{'+'if pos.profit>=0 else ''}{pos.profit:.2f}$`"
        )
        return True
    else:
        rc = result.retcode if result else "None"
        log(f"  -> [ERROR] Close failed #{pos.ticket}. Retcode: {rc}")
        return False


def partial_close_position(pos, close_ratio: float = 0.5) -> bool:
    """Close a fraction of the position (default 50%)."""
    symbol_info = mt5.symbol_info(pos.symbol)
    if not symbol_info:
        return False

    vol_to_close = round(pos.volume * close_ratio, 2)
    vol_to_close = max(symbol_info.volume_min, vol_to_close)
    # Ensure it's a valid step multiple
    step = symbol_info.volume_step
    vol_to_close = round(round(vol_to_close / step) * step, 2)

    if vol_to_close >= pos.volume:
        return close_position(pos, "Full close (partial calc)")

    tick    = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return False

    order_type = (mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY
                  else mt5.ORDER_TYPE_BUY)
    price   = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
    filling = get_filling_type(pos.symbol)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       pos.symbol,
        "volume":       float(vol_to_close),
        "type":         order_type,
        "position":     pos.ticket,
        "price":        price,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      "Partial 1:1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    ok = result and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        log(f"  -> [PARTIAL] #{pos.ticket} closed {vol_to_close} lots at 1:1 R:R")
        tg_alert(f"✂️ *Partial Close* #{pos.ticket} — {vol_to_close} lots at 1:1 R:R")
    return ok


def modify_sl(pos, new_sl: float) -> bool:
    """Move SL to new_sl (trailing stop / breakeven)."""
    symbol_info = mt5.symbol_info(pos.symbol)
    if not symbol_info:
        return False

    digits  = symbol_info.digits
    new_sl  = round(new_sl, digits)

    # Sanity: don't move SL backward (skip check if SL is 0 = not yet set)
    if pos.type == mt5.ORDER_TYPE_BUY and pos.sl != 0 and new_sl <= pos.sl:
        return False
    if pos.type == mt5.ORDER_TYPE_SELL and pos.sl != 0 and new_sl >= pos.sl:
        return False

    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos.symbol,
        "position": pos.ticket,
        "sl":       new_sl,
        "tp":       pos.tp,
    }
    result = mt5.order_send(request)
    ok = result and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        log(f"  -> [TRAIL] #{pos.ticket} SL moved to {new_sl:.2f}")
        tg_alert(f"📌 *Trailing SL moved* #{pos.ticket} → `{new_sl:.2f}`")
    return ok


def cancel_order(ticket: int, reason: str = "") -> None:
    request = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
    result  = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [CANCELLED] Order {ticket}. {reason}")
    else:
        log(f"  -> [ERROR] Failed to cancel {ticket}. {reason}")


# ─────────────────────────────────────────────────────────────────────────────
#  Trailing stop logic
# ─────────────────────────────────────────────────────────────────────────────

# Track which positions have already been partially closed at 1:1
_partial_closed: set = set()

def _cleanup_partial_closed() -> None:
    """Remove tickets that are no longer open from the partial-close tracker."""
    if not _partial_closed:
        return
    open_tickets = {p.ticket for p in (mt5.positions_get() or [])}
    stale = _partial_closed - open_tickets
    for t in stale:
        _partial_closed.discard(t)

def apply_trailing_stop(pos) -> None:
    """
    Called each cycle for every open position.

    1. Once in profit ≥ TRAILING_ACTIVATION_POINTS → move SL to breakeven.
    2. As price moves further, trail SL by TRAILING_STEP_POINTS.
    3. At 1:1 R:R → partial close 50 % if not already done.
    """
    symbol_info = mt5.symbol_info(pos.symbol)
    if not symbol_info:
        return

    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return

    point   = symbol_info.point
    digits  = symbol_info.digits
    entry   = pos.price_open

    if pos.type == mt5.ORDER_TYPE_BUY:
        current_price = tick.bid
        profit_points = (current_price - entry) / point

        # ── Partial close at 1:1 ────────────────────────────────────────
        if pos.ticket not in _partial_closed:
            # SL distance in points (use MIN_SL_POINTS as floor when no SL set)
            sl_dist_pts = max((entry - pos.sl) / point, MIN_SL_POINTS) if pos.sl else MIN_SL_POINTS
            if profit_points >= sl_dist_pts:   # 1:1 reached
                if partial_close_position(pos, PARTIAL_CLOSE_RATIO):
                    _partial_closed.add(pos.ticket)

        # ── Trailing SL ─────────────────────────────────────────────────
        if profit_points >= TRAILING_ACTIVATION_POINTS:
            # Ideal SL = current price − TRAILING_STEP_POINTS
            ideal_sl = round(current_price - (TRAILING_STEP_POINTS * point), digits)
            # Move SL to the higher of: breakeven or ideal_sl
            be_sl    = round(entry + (10 * point), digits)   # 10 pts above entry
            new_sl   = max(be_sl, ideal_sl)
            modify_sl(pos, new_sl)

    else:  # SELL
        current_price = tick.ask
        profit_points = (entry - current_price) / point

        if pos.ticket not in _partial_closed:
            sl_dist_pts = max((pos.sl - entry) / point, MIN_SL_POINTS) if pos.sl else MIN_SL_POINTS
            if profit_points >= sl_dist_pts:
                if partial_close_position(pos, PARTIAL_CLOSE_RATIO):
                    _partial_closed.add(pos.ticket)

        if profit_points >= TRAILING_ACTIVATION_POINTS:
            ideal_sl = round(current_price + (TRAILING_STEP_POINTS * point), digits)
            be_sl    = round(entry - (10 * point), digits)
            new_sl   = min(be_sl, ideal_sl)
            modify_sl(pos, new_sl)


# ─────────────────────────────────────────────────────────────────────────────
#  Manage open positions & pending orders
# ─────────────────────────────────────────────────────────────────────────────

def manage_open_positions(account_balance: float) -> int:
    """
    For each open position:
      - Apply trailing stop / partial close
      - Close if profit target % reached
      - Close if time limit exceeded (and trade losing)
    Returns count of bot-managed open positions.
    """
    positions = mt5.positions_get()
    if not positions:
        return 0

    profit_target_usd = account_balance * (PROFIT_TARGET_PERCENT / 100.0)
    count = 0

    for pos in positions:
        if pos.magic != MAGIC_NUMBER:
            continue
        count += 1

        # Apply trailing / partial close first
        apply_trailing_stop(pos)

        # Refresh pos after potential partial close (position may have changed)
        refreshed = mt5.positions_get(ticket=pos.ticket)
        if not refreshed:
            # Position was fully closed by partial_close (or externally)
            _partial_closed.discard(pos.ticket)
            count -= 1
            continue
        pos = refreshed[0]

        if pos.profit >= profit_target_usd:
            if close_position(pos, f"Profit target ${profit_target_usd:.2f}"):
                _partial_closed.discard(pos.ticket)
                count -= 1

        elif (time.time() - pos.time) > TIME_LIMIT_SECONDS and pos.profit < 0:
            if close_position(pos, f"Time limit {TIME_LIMIT_SECONDS}s"):
                _partial_closed.discard(pos.ticket)
                count -= 1

    # Prune stale tickets from the partial-close tracker
    _cleanup_partial_closed()
    return count


def manage_pending_orders() -> None:
    orders = mt5.orders_get()
    if not orders:
        return
    for order in orders:
        if getattr(order, "magic", 0) != MAGIC_NUMBER:
            continue
        if (time.time() - order.time_setup) > 120:
            cancel_order(order.ticket, "Stale >2min")


# ─────────────────────────────────────────────────────────────────────────────
#  Place trade
# ─────────────────────────────────────────────────────────────────────────────

def place_trade(symbol: str, direction: str, signal_strength: int,
                details: dict) -> bool:
    mt5.symbol_select(symbol, True)
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        log(f"  -> [ERROR] Cannot get symbol info for {symbol}")
        return False

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        log(f"  -> [ERROR] No tick data for {symbol}")
        return False

    if symbol_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        log(f"  -> [SKIP] {symbol} not in FULL trade mode")
        return False

    point   = symbol_info.point
    digits  = symbol_info.digits
    spread  = (tick.ask - tick.bid) / point

    # ── Entry price — FVG tap or market ──────────────────────────────────
    fvg = details.get("fvg")

    if direction == "bullish":
        order_type  = mt5.ORDER_TYPE_BUY
        if USE_FVG_ENTRY and fvg and fvg["type"] == "bullish_fvg":
            # Wait for price to retrace into the FVG — place limit at mid
            entry_price = round(fvg["mid"], digits)
            use_limit   = True
            log(f"     FVG entry: limit BUY @ {entry_price} "
                f"(FVG {fvg['bottom']:.2f}–{fvg['top']:.2f})")
        else:
            entry_price = round(tick.ask, digits)
            use_limit   = False
    else:
        order_type  = mt5.ORDER_TYPE_SELL
        if USE_FVG_ENTRY and fvg and fvg["type"] == "bearish_fvg":
            entry_price = round(fvg["mid"], digits)
            use_limit   = True
            log(f"     FVG entry: limit SELL @ {entry_price} "
                f"(FVG {fvg['bottom']:.2f}–{fvg['top']:.2f})")
        else:
            entry_price = round(tick.bid, digits)
            use_limit   = False

    # ── SL from swing (sniper placement) ──────────────────────────────────
    swing_sl_price = details.get("swing_sl_price")
    if swing_sl_price:
        sl_price, sl_points = sl_from_swing(
            order_type, entry_price, swing_sl_price, symbol_info,
            buffer_points=50
        )
    else:
        sl_points = max(DEFAULT_SL_POINTS, spread * 2, MIN_SL_POINTS)
        sl_price  = None   # calculated below

    # Enforce minimum SL
    sl_points = max(sl_points, MIN_SL_POINTS)
    tp_points = sl_points * TP_RATIO

    # ── Lot size ──────────────────────────────────────────────────────────
    lot = calculate_lot_size(symbol, sl_points)
    lot = max(lot, symbol_info.volume_min)

    # ── SL / TP prices ─────────────────────────────────────────────────────
    # Single call — returns (sl, tp) based on order direction
    sl_price_calc, tp_price = calculate_sl_tp(
        order_type, entry_price, sl_points, tp_points, symbol_info
    )
    if sl_price is None:
        sl_price = sl_price_calc

    filling = get_filling_type(symbol)

    log(f"  -> Preparing {direction.upper()} order "
        f"({'LIMIT' if use_limit else 'MARKET'}):")
    log(f"     Entry={entry_price}  SL={sl_price}  TP={tp_price}")
    log(f"     Lot={lot}  Spread={spread:.0f}pts  SL={sl_points:.0f}pts  "
        f"TP={tp_points:.0f}pts  Strength={signal_strength}/8")

    if use_limit:
        # Limit order — fills when price retraces to FVG
        limit_order_type = (mt5.ORDER_TYPE_BUY_LIMIT  if direction == "bullish"
                            else mt5.ORDER_TYPE_SELL_LIMIT)
        request = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       float(lot),
            "type":         limit_order_type,
            "price":        entry_price,
            "sl":           sl_price,
            "tp":           tp_price,
            "deviation":    20,
            "magic":        MAGIC_NUMBER,
            "comment":      f"APA {direction[:4]} FVG s{signal_strength}",
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
            "deviation":    20,
            "magic":        MAGIC_NUMBER,
            "comment":      f"APA {direction[:4]} s{signal_strength}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

    # ── Pre-check ─────────────────────────────────────────────────────────
    check = mt5.order_check(request)
    if check is None:
        log(f"  -> [ERROR] order_check None. Error: {mt5.last_error()}")
        return False

    if check.retcode != 0:
        log(f"  -> [CHECK FAIL] Retcode={check.retcode} {check.comment}")

        if check.retcode == 10016:   # Invalid stops — widen
            sl_points = max(sl_points * 1.5, spread * 3, MIN_SL_POINTS)
            tp_points = sl_points * TP_RATIO
            sl_price, tp_price = calculate_sl_tp(
                order_type, entry_price, sl_points, tp_points, symbol_info
            )
            request["sl"] = sl_price
            request["tp"] = tp_price
            log(f"     Retrying wider SL={sl_price} TP={tp_price}")
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                log("  -> [FAIL] Still failing after SL widening")
                return False

        elif check.retcode == 10019:  # Not enough margin
            request["volume"] = float(symbol_info.volume_min)
            lot = symbol_info.volume_min
            log(f"     Reduced to min lot: {lot}")
            check = mt5.order_check(request)
            if check is None or check.retcode != 0:
                log("  -> [FAIL] Not enough margin even for min lot")
                return False

        elif check.retcode == 10018:
            log("  -> [SKIP] Market closed")
            return False

    # ── Send ──────────────────────────────────────────────────────────────
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"  -> [SUCCESS] #{result.order} {direction.upper()} "
            f"{symbol} {lot}L @ {entry_price}")
        tg_alert(
            f"🚀 *Trade Opened*\n"
            f"`{symbol}` {'🟢 BUY' if direction=='bullish' else '🔴 SELL'}\n"
            f"Entry: `{entry_price}`  SL: `{sl_price}`  TP: `{tp_price}`\n"
            f"Lot: `{lot}`  Strength: `{signal_strength}/8`"
        )
        return True
    else:
        rc      = result.retcode if result else "None"
        comment = getattr(result, "comment", "") if result else ""
        log(f"  -> [ERROR] Trade failed. Retcode={rc} {comment}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Main bot cycle
# ─────────────────────────────────────────────────────────────────────────────

def run_bot_cycle() -> None:
    log(f"\n=== Scalp Scan ===")

    acc = mt5.account_info()
    if not acc:
        log("  [ERROR] Cannot get account info")
        return

    balance = acc.balance
    equity  = acc.equity
    log(f"  Balance=${balance:.2f}  Equity=${equity:.2f}  Free=${acc.margin_free:.2f}")

    # ── Update daily tracker ───────────────────────────────────────────────
    daily_tracker.update(balance)

    # ── Daily loss halt ────────────────────────────────────────────────────
    if daily_tracker.is_daily_limit_hit(balance):
        msg = (f"⛔ Daily loss limit hit! "
               f"Start=${daily_tracker.day_start_balance:.2f} "
               f"Now=${balance:.2f}. Halting.")
        log(f"  [HALT] {msg}")
        tg_alert(f"⛔ *Daily Loss Limit Hit*\nBot paused for today.")
        set_state("PAUSED")
        return

    # ── Milestone check ───────────────────────────────────────────────────
    for m in check_milestones(balance):
        msg = f"🎯 Balance milestone reached: ${m:.0f}!"
        log(f"  [MILESTONE] {msg}")
        tg_alert(msg)

    # ── Session filter ────────────────────────────────────────────────────
    if not is_trading_session():
        log("  [SKIP] Outside London/NY session")
        return

    # ── Count open positions ──────────────────────────────────────────────
    open_count = sum(
        1 for p in (mt5.positions_get() or [])
        if p.magic == MAGIC_NUMBER
    )

    if open_count >= MAX_CONCURRENT_TRADES:
        log(f"  Max trades ({MAX_CONCURRENT_TRADES}) reached")
        return

    # ── Scan symbols ──────────────────────────────────────────────────────
    for symbol in SYMBOLS:
        if open_count >= MAX_CONCURRENT_TRADES:
            break

        # Spread guard
        spread_pts = get_spread_points(symbol)
        if spread_pts > MAX_SPREAD_POINTS:
            log(f"  {symbol}: Spread too wide ({spread_pts:.0f} pts) — skipping")
            continue

        # Fetch OHLCV — 5M-only architecture
        # 250 bars needed for EMA200 to be accurate
        m5  = get_df(symbol, TIMEFRAME_CYCLE["situational_1"], 250)
        m1  = get_df(symbol, TIMEFRAME_CYCLE["situational_2"], 60)

        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info or m5 is None or m1 is None:
            log(f"  {symbol}: Missing data — skipping")
            continue

        symbol_data = {"m5": m5, "m1": m1}

        kz = is_killzone()
        kz_label = " 🎯 KILLZONE" if kz else ""

        # Detect signal — pass killzone flag for bonus scoring
        direction, strength, details = detect_scalp_signal(
            symbol_data, symbol_info.point, spread_pts,
            in_killzone=kz
        )

        if direction is None:
            reason = details.get("reason", "No signal")
            log(f"  {symbol}: {reason}{kz_label}")
            continue

        fvg      = details.get("fvg")
        fvg_tag  = f" | FVG [{fvg['bottom']:.2f}–{fvg['top']:.2f}]" if fvg else " | No FVG"

        log(f"  {symbol}: {direction.upper()} signal! "
            f"Strength {strength}/8 | RSI={details.get('m5_rsi','?')} | "
            f"CHoCH={details.get('choch',False)}"
            f"{fvg_tag}{kz_label} | "
            f"ATR={details.get('atr_points','?')}pts")

        if strength < MIN_SIGNAL_STRENGTH:
            log(f"  {symbol}: Strength {strength} below threshold {MIN_SIGNAL_STRENGTH}")
            continue

        success = place_trade(symbol, direction, strength, details)
        if success:
            open_count += 1


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def start_bot() -> None:
    """Initialise MT5, login, and run the main loop."""
    if not mt5.initialize():
        print("Failed to initialise MT5")
        return

    LOGIN    = int(os.getenv("MT5_LOGIN",    0))
    PASSWORD = os.getenv("MT5_PASSWORD", "")
    SERVER   = os.getenv("MT5_SERVER",   "")

    if not LOGIN or not PASSWORD or not SERVER:
        print("ERROR: MT5 credentials missing from .env file")
        mt5.shutdown()
        return

    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        print(f"MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return

    acc = mt5.account_info()
    log("=" * 60)
    log("XAUUSD APA Scalping Bot v2 Started")
    log(f"Account: {LOGIN} @ {SERVER}")
    log(f"Balance: ${acc.balance:.2f}  Leverage: 1:{acc.leverage}")
    log(f"Risk: {15}%/trade  MaxTrades: {MAX_CONCURRENT_TRADES}")
    log(f"SL: swing-based (min {MIN_SL_POINTS}pts)  TP Ratio: {TP_RATIO}x")
    log(f"Trail activates: +{TRAILING_ACTIVATION_POINTS}pts  Step: {TRAILING_STEP_POINTS}pts")
    log(f"Signal threshold: {MIN_SIGNAL_STRENGTH}/7")
    log(f"Sessions: London {LONDON_SESSION_START}–{LONDON_SESSION_END} UTC  "
        f"NY {NY_SESSION_START}–{NY_SESSION_END} UTC")
    log("=" * 60)

    try:
        while get_state() != "STOPPED":
            if get_state() == "PAUSED":
                log("  [PAUSED] Waiting...")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            try:
                acc = mt5.account_info()
                if acc:
                    manage_open_positions(acc.balance)
                    manage_pending_orders()
                    run_bot_cycle()
            except Exception as e:
                log(f"  [CYCLE ERROR] {type(e).__name__}: {e}")

            time.sleep(SCAN_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log("Bot stopped by user (KeyboardInterrupt).")
    finally:
        mt5.shutdown()
        log("MT5 shutdown complete.")


if __name__ == "__main__":
    start_bot()
