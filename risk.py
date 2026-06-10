"""
risk.py — Position sizing, SL/TP calculation, and daily loss protection.

Improvements over v1:
  - Compound-aware lot sizing: scales up naturally as balance grows
  - Daily loss tracker: halts the bot if drawdown threshold is breached
  - Margin safety: never use more than 40 % of free margin on one trade
  - Swing-based SL distance support
"""

import time
from datetime import datetime, timezone
import MetaTrader5 as mt5

from config import (
    DEFAULT_LOT_SIZE,
    MAX_RISK_PERCENT,
    MAX_DAILY_LOSS_PERCENT,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Daily Loss Tracker
# ─────────────────────────────────────────────────────────────────────────────

class DailyLossTracker:
    """
    Tracks the account balance at the start of each trading day (UTC).
    Provides a method to check whether the daily drawdown limit has been hit.
    """

    def __init__(self):
        self._day_start_balance: float = 0.0
        self._last_reset_date: str = ""

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def update(self, current_balance: float) -> None:
        """Call once per bot cycle to keep the tracker current."""
        today = self._today_utc()
        if today != self._last_reset_date:
            # New trading day — record starting balance
            self._day_start_balance = current_balance
            self._last_reset_date   = today

    def is_daily_limit_hit(self, current_balance: float) -> bool:
        """Return True if the day's drawdown has exceeded the threshold."""
        if self._day_start_balance <= 0:
            return False
        drawdown_pct = (
            (self._day_start_balance - current_balance)
            / self._day_start_balance
            * 100
        )
        return drawdown_pct >= MAX_DAILY_LOSS_PERCENT

    def daily_pnl(self, current_balance: float) -> float:
        """Return today's P&L in USD (negative = loss)."""
        return current_balance - self._day_start_balance

    @property
    def day_start_balance(self) -> float:
        return self._day_start_balance


# Singleton — imported by main.py
daily_tracker = DailyLossTracker()


# ─────────────────────────────────────────────────────────────────────────────
#  Account helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_account_info():
    info = mt5.account_info()
    if info is None:
        print("Failed to get account info:", mt5.last_error())
    return info


# ─────────────────────────────────────────────────────────────────────────────
#  Lot size calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_lot_size(symbol: str, sl_points: float) -> float:
    """
    Risk-based lot size.

    Formula:
        risk_amount  = balance × MAX_RISK_PERCENT%
        lot          = risk_amount / (sl_ticks × tick_value_per_lot)

    For a $6 account this will likely return volume_min (0.01).
    As balance grows the lot automatically compounds upward.

    Additional guards:
      - Never exceed volume_max
      - Never use more than 40 % of free margin
      - Round to the broker's volume_step
    """
    account = get_account_info()
    if not account:
        return DEFAULT_LOT_SIZE

    balance      = account.balance
    margin_free  = account.margin_free
    leverage     = account.leverage

    symbol_info  = mt5.symbol_info(symbol)
    if not symbol_info:
        return DEFAULT_LOT_SIZE

    tick_value   = symbol_info.trade_tick_value    # $ per tick per 1 lot
    tick_size    = symbol_info.trade_tick_size
    point        = symbol_info.point
    vol_min      = symbol_info.volume_min
    vol_max      = symbol_info.volume_max
    vol_step     = symbol_info.volume_step
    contract_sz  = getattr(symbol_info, "trade_contract_size", 100)

    if tick_size == 0 or tick_value == 0 or sl_points == 0:
        return DEFAULT_LOT_SIZE

    # ── Risk-based calculation ─────────────────────────────────────────────
    risk_amount  = balance * (MAX_RISK_PERCENT / 100.0)
    sl_ticks     = sl_points * (point / tick_size)
    calc_lot     = risk_amount / (sl_ticks * tick_value)

    # ── Margin guard: max 40 % of free margin on a single trade ───────────
    tick = mt5.symbol_info_tick(symbol)
    if tick and leverage > 0:
        price           = tick.ask
        margin_per_lot  = (price * contract_sz) / leverage
        max_lot_margin  = (margin_free * 0.40) / margin_per_lot
    else:
        max_lot_margin  = vol_max

    # ── Clamp and round ───────────────────────────────────────────────────
    lot = max(vol_min, min(vol_max, calc_lot, max_lot_margin))
    lot = round(round(lot / vol_step) * vol_step, 2)

    return lot


# ─────────────────────────────────────────────────────────────────────────────
#  SL / TP from swing price
# ─────────────────────────────────────────────────────────────────────────────

def calculate_sl_tp(order_type: int, entry_price: float,
                    sl_points: float, tp_points: float,
                    symbol_info) -> tuple:
    """
    Calculate absolute SL and TP prices, rounded to symbol precision.
    Falls back to point-based if swing_sl_price is not supplied.
    """
    point  = symbol_info.point
    digits = symbol_info.digits

    if order_type == mt5.ORDER_TYPE_BUY:
        sl = round(entry_price - (sl_points * point), digits)
        tp = round(entry_price + (tp_points * point), digits)
    else:  # SELL
        sl = round(entry_price + (sl_points * point), digits)
        tp = round(entry_price - (tp_points * point), digits)

    return sl, tp


def sl_from_swing(order_type: int, entry_price: float,
                  swing_price: float, symbol_info,
                  buffer_points: int = 50) -> tuple:
    """
    Place SL just beyond the swing high/low with a small buffer.
    Returns (sl_price, sl_points_distance).
    """
    point  = symbol_info.point
    digits = symbol_info.digits

    if order_type == mt5.ORDER_TYPE_BUY:
        sl = round(swing_price - (buffer_points * point), digits)
        sl_dist = (entry_price - sl) / point
    else:
        sl = round(swing_price + (buffer_points * point), digits)
        sl_dist = (sl - entry_price) / point

    return sl, max(sl_dist, 1)   # always positive
