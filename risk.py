"""
risk.py — Dynamic risk management, position sizing, daily/weekly protection.

Improvements over previous version:
  - Balance-tiered risk percentages (auto-selected)
  - Balance-tiered daily loss limits
  - Weekly drawdown tracker
  - Peak equity monitor (emergency stop)
  - Margin safety guard (40% free margin max)
  - Per-symbol consecutive loss tracker
  - Minimum stop level validation before order
"""

import time
from datetime import datetime, timezone
import MetaTrader5 as mt5

from config import (
    RISK_TIERS, DAILY_LOSS_TIERS,
    get_risk_percent, get_daily_loss_limit,
    WEEKLY_DRAWDOWN_LIMIT, PEAK_EQUITY_DROP_LIMIT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_SYMBOL,
    MIN_MINUTES_BETWEEN, MAGIC_NUMBER,
    SYMBOL_SL_BUFFER, DEFAULT_SL_POINTS, MIN_SL_POINTS,
)


# ─────────────────────────────────────────────────────────────────────────────
#  DAILY LOSS TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class DailyLossTracker:
    def __init__(self):
        self._day_start_balance: float = 0.0
        self._last_reset_date:   str   = ""

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def update(self, current_balance: float) -> None:
        today = self._today_utc()
        if today != self._last_reset_date:
            self._day_start_balance = current_balance
            self._last_reset_date   = today

    def is_limit_hit(self, current_balance: float) -> bool:
        if self._day_start_balance <= 0:
            return False
        loss_pct = ((self._day_start_balance - current_balance)
                    / self._day_start_balance * 100.0)
        limit = get_daily_loss_limit(self._day_start_balance)
        return loss_pct >= limit

    def daily_pnl(self, current_balance: float) -> float:
        return current_balance - self._day_start_balance

    @property
    def day_start_balance(self) -> float:
        return self._day_start_balance


# ─────────────────────────────────────────────────────────────────────────────
#  WEEKLY DRAWDOWN TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class WeeklyDrawdownTracker:
    def __init__(self):
        self._week_start_equity: float = 0.0
        self._last_reset_week:   str   = ""
        self.halt_active:        bool  = False

    def _current_week(self) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year}-W{now.isocalendar()[1]}"

    def update(self, current_equity: float) -> None:
        week = self._current_week()
        if week != self._last_reset_week:
            self._week_start_equity = current_equity
            self._last_reset_week   = week
            self.halt_active        = False

    def is_limit_hit(self, current_equity: float) -> bool:
        if self._week_start_equity <= 0:
            return False
        loss_pct = ((self._week_start_equity - current_equity)
                    / self._week_start_equity * 100.0)
        if loss_pct >= WEEKLY_DRAWDOWN_LIMIT:
            self.halt_active = True
            return True
        return False

    def manual_reset(self) -> None:
        """Call this to resume after manual review."""
        self.halt_active = False


# ─────────────────────────────────────────────────────────────────────────────
#  PEAK EQUITY MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class PeakEquityMonitor:
    def __init__(self):
        self._peak:        float = 0.0
        self.emergency:    bool  = False

    def update(self, current_equity: float) -> None:
        if current_equity > self._peak:
            self._peak = current_equity

    def is_emergency(self, current_equity: float) -> bool:
        if self._peak <= 0:
            return False
        drop_pct = ((self._peak - current_equity) / self._peak * 100.0)
        if drop_pct >= PEAK_EQUITY_DROP_LIMIT:
            self.emergency = True
            return True
        return False

    @property
    def peak(self) -> float:
        return self._peak


# ─────────────────────────────────────────────────────────────────────────────
#  PER-SYMBOL STATE TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class SymbolTracker:
    """Tracks per-symbol trade count, gaps, and consecutive losses."""

    def __init__(self):
        self._trades_today:     dict = {}
        self._last_trade_time:  dict = {}
        self._consec_losses:    dict = {}
        self._session_locked:   dict = {}
        self._last_reset:       str  = ""

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_if_new_day(self) -> None:
        today = self._today()
        if today != self._last_reset:
            self._trades_today   = {}
            self._session_locked = {}
            self._consec_losses  = {}
            self._last_reset     = today

    def can_trade(self, symbol: str) -> tuple:
        """Returns (ok, reason)"""
        self._reset_if_new_day()

        if self._session_locked.get(symbol, False):
            return False, f"{symbol} locked — 2 consecutive losses this session"

        trades = self._trades_today.get(symbol, 0)
        if trades >= MAX_TRADES_PER_SYMBOL:
            return False, f"{symbol} daily trade limit ({MAX_TRADES_PER_SYMBOL}) reached"

        last = self._last_trade_time.get(symbol, 0)
        if last > 0:
            elapsed_mins = (time.time() - last) / 60.0
            if elapsed_mins < MIN_MINUTES_BETWEEN:
                remaining = int(MIN_MINUTES_BETWEEN - elapsed_mins)
                return False, f"{symbol} gap filter — {remaining}min remaining"

        return True, "ok"

    def record_entry(self, symbol: str) -> None:
        self._reset_if_new_day()
        self._trades_today[symbol]    = self._trades_today.get(symbol, 0) + 1
        self._last_trade_time[symbol] = time.time()

    def record_result(self, symbol: str, profit: float) -> None:
        self._reset_if_new_day()
        if profit < 0:
            losses = self._consec_losses.get(symbol, 0) + 1
            self._consec_losses[symbol] = losses
            if losses >= 2:
                self._session_locked[symbol] = True
        else:
            self._consec_losses[symbol]  = 0
            self._session_locked[symbol] = False

    def trades_today(self, symbol: str) -> int:
        self._reset_if_new_day()
        return self._trades_today.get(symbol, 0)


# ─────────────────────────────────────────────────────────────────────────────
#  LOT SIZE CALCULATION — dynamic, balance-tiered
# ─────────────────────────────────────────────────────────────────────────────

def calculate_lot_size(symbol: str, sl_points: float,
                       score: float = 10.0) -> float:
    """
    Risk-based lot size with score-based scaling.

    score >= 9  → full lot
    score 7–8   → full lot
    score 5–6   → half lot (MIN_SIGNAL_SCORE_HALF range)

    Guards:
      - Never exceed volume_max
      - Never use more than 40% of free margin
      - Round to broker's volume_step
    """
    from config import MIN_SIGNAL_SCORE_HALF, MIN_SIGNAL_SCORE

    acc = mt5.account_info()
    if not acc:
        return 0.0

    balance     = acc.balance
    margin_free = acc.margin_free
    leverage    = acc.leverage

    sym_info = mt5.symbol_info(symbol)
    if not sym_info:
        return 0.0

    tick_value   = sym_info.trade_tick_value
    tick_size    = sym_info.trade_tick_size
    point        = sym_info.point
    vol_min      = sym_info.volume_min
    vol_max      = sym_info.volume_max
    vol_step     = sym_info.volume_step
    contract_sz  = getattr(sym_info, "trade_contract_size", 100)

    if tick_size == 0 or tick_value == 0 or sl_points == 0:
        return vol_min

    risk_pct    = get_risk_percent(balance)

    # Half lot for borderline scores
    if score < MIN_SIGNAL_SCORE:
        risk_pct = risk_pct * 0.5

    risk_amount = balance * (risk_pct / 100.0)
    sl_ticks    = sl_points * (point / tick_size)
    calc_lot    = risk_amount / (sl_ticks * tick_value)

    # Margin guard
    tick = mt5.symbol_info_tick(symbol)
    if tick and leverage > 0:
        price          = tick.ask
        margin_per_lot = (price * contract_sz) / leverage
        max_margin_lot = (margin_free * 0.40) / margin_per_lot if margin_per_lot > 0 else vol_max
    else:
        max_margin_lot = vol_max

    lot = max(vol_min, min(vol_max, calc_lot, max_margin_lot))
    lot = round(round(lot / vol_step) * vol_step, 2)
    return lot


# ─────────────────────────────────────────────────────────────────────────────
#  SL / TP PRICE CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calculate_sl_tp(order_type: int, entry_price: float,
                    sl_points: float, tp_ratio: float,
                    symbol_info) -> tuple:
    """Returns (sl_price, tp_price)."""
    point  = symbol_info.point
    digits = symbol_info.digits
    tp_points = sl_points * tp_ratio

    if order_type == mt5.ORDER_TYPE_BUY:
        sl = round(entry_price - sl_points * point, digits)
        tp = round(entry_price + tp_points * point, digits)
    else:
        sl = round(entry_price + sl_points * point, digits)
        tp = round(entry_price - tp_points * point, digits)

    return sl, tp


def sl_from_swing(order_type: int, entry_price: float,
                  swing_price: float, symbol_info,
                  symbol: str) -> tuple:
    """
    Place SL just beyond the swing high/low with per-symbol buffer.
    Returns (sl_price, sl_points_distance).
    """
    point       = symbol_info.point
    digits      = symbol_info.digits
    buffer_pts  = SYMBOL_SL_BUFFER.get(symbol, DEFAULT_SL_POINTS)

    if order_type == mt5.ORDER_TYPE_BUY:
        sl      = round(swing_price - buffer_pts * point, digits)
        sl_dist = max((entry_price - sl) / point, 1.0)
    else:
        sl      = round(swing_price + buffer_pts * point, digits)
        sl_dist = max((sl - entry_price) / point, 1.0)

    return sl, sl_dist


def validate_sl_tp(symbol: str, entry: float, sl: float, tp: float,
                   order_type: int, symbol_info) -> tuple:
    """
    Ensure SL and TP meet broker's minimum stop level.
    Returns (sl, tp) — adjusted if needed.
    """
    stop_level = int(symbol_info.trade_stops_level)
    point      = symbol_info.point
    digits     = symbol_info.digits
    min_dist   = stop_level * point * 1.2  # 20% buffer above minimum

    if order_type == mt5.ORDER_TYPE_BUY:
        if entry - sl < min_dist:
            sl = round(entry - min_dist, digits)
        if tp - entry < min_dist:
            tp = round(entry + min_dist, digits)
    else:
        if sl - entry < min_dist:
            sl = round(entry + min_dist, digits)
        if entry - tp < min_dist:
            tp = round(entry - min_dist, digits)

    return sl, tp


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
#  SINGLETONS — imported by main.py
# ─────────────────────────────────────────────────────────────────────────────

daily_tracker  = DailyLossTracker()
weekly_tracker = WeeklyDrawdownTracker()
peak_monitor   = PeakEquityMonitor()
sym_tracker    = SymbolTracker()
