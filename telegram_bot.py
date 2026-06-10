"""
telegram_bot.py — Telegram remote control for the APA Scalping Bot.

Commands
--------
/start   — Resume trading (unpause)
/stop    — Pause new trades (open positions stay)
/status  — Account balance, equity, daily P&L, state
/trades  — List all open positions
/close   — Close ALL open bot positions
/setsl <points>  — Change DEFAULT_SL_POINTS on the fly
/settp <ratio>   — Change TP_RATIO on the fly (e.g. 2.0)
/log     — Send last 20 lines of bot_logs.txt

Automatic alerts sent by main.py via register_telegram_alert():
  - Trade opened
  - Trade closed
  - Trailing SL moved
  - Daily loss limit hit
  - Balance milestones
"""

import os
import asyncio
import threading
import logging
from datetime import datetime, timezone

import MetaTrader5 as mt5
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)
from telegram.constants import ParseMode

load_dotenv()

# Suppress noisy httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")


# ─────────────────────────────────────────────────────────────────────────────
#  Alert sender (called from main.py thread)
# ─────────────────────────────────────────────────────────────────────────────

_bot_instance: Bot | None = None
_event_loop:   asyncio.AbstractEventLoop | None = None

def _send_alert_sync(message: str) -> None:
    """Thread-safe: schedule a coroutine on the bot's event loop."""
    if not _bot_instance or not _event_loop or not CHAT_ID:
        return
    asyncio.run_coroutine_threadsafe(
        _bot_instance.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
        ),
        _event_loop,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Helper
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_pos(pos) -> str:
    side = "🟢 BUY" if pos.type == mt5.ORDER_TYPE_BUY else "🔴 SELL"
    pnl  = f"{'+'if pos.profit>=0 else ''}{pos.profit:.2f}"
    return (
        f"  #{pos.ticket} {side} {pos.volume}L @ {pos.price_open:.2f}\n"
        f"  SL={pos.sl:.2f}  TP={pos.tp:.2f}  P&L={pnl}$"
    )


def _requires_auth(chat_id_str: str) -> bool:
    """Reject commands from anyone other than the configured chat ID."""
    if not CHAT_ID:
        return False   # no CHAT_ID set → open (dangerous but permissive)
    return str(chat_id_str) != str(CHAT_ID)


# ─────────────────────────────────────────────────────────────────────────────
#  Command handlers
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    from main import set_state, get_state
    set_state("RUNNING")
    await update.message.reply_text(
        "✅ *Bot RESUMED* — now looking for trades.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    from main import set_state
    set_state("PAUSED")
    await update.message.reply_text(
        "⏸ *Bot PAUSED* — no new trades will open.\n"
        "Open positions remain active.\n"
        "Use /start to resume.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    from main import get_state
    from risk import daily_tracker

    acc = mt5.account_info()
    if not acc:
        await update.message.reply_text("❌ Cannot connect to MT5.")
        return

    daily_pnl  = daily_tracker.daily_pnl(acc.balance)
    state_icon = {"RUNNING": "🟢", "PAUSED": "⏸", "STOPPED": "🔴"}.get(
        get_state(), "❓"
    )

    positions  = mt5.positions_get() or []
    bot_pos    = [p for p in positions if p.magic == 123456]
    open_pnl   = sum(p.profit for p in bot_pos)

    text = (
        f"{state_icon} *Bot Status: {get_state()}*\n\n"
        f"💰 Balance:    `${acc.balance:.2f}`\n"
        f"📊 Equity:     `${acc.equity:.2f}`\n"
        f"🆓 Free Margin:`${acc.margin_free:.2f}`\n"
        f"📈 Daily P&L:  `{'+'if daily_pnl>=0 else ''}{daily_pnl:.2f}$`\n"
        f"📂 Open trades: `{len(bot_pos)}`\n"
        f"💵 Open P&L:   `{'+'if open_pnl>=0 else ''}{open_pnl:.2f}$`\n"
        f"🕐 UTC Time:   `{datetime.now(timezone.utc).strftime('%H:%M:%S')}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    positions = mt5.positions_get() or []
    bot_pos   = [p for p in positions if p.magic == 123456]

    if not bot_pos:
        await update.message.reply_text("📭 No open positions.")
        return

    lines = ["*Open Positions:*\n"]
    for p in bot_pos:
        lines.append(_fmt_pos(p))
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    from main import close_position

    positions = mt5.positions_get() or []
    bot_pos   = [p for p in positions if p.magic == 123456]

    if not bot_pos:
        await update.message.reply_text("📭 No open positions to close.")
        return

    await update.message.reply_text(
        f"⚠️ Closing {len(bot_pos)} position(s)..."
    )
    closed = 0
    for p in bot_pos:
        if close_position(p, "Manual /close all"):
            closed += 1

    await update.message.reply_text(
        f"✅ Closed {closed}/{len(bot_pos)} positions."
    )


async def cmd_setsl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    import config
    try:
        points = int(context.args[0])
        if points < 100:
            raise ValueError("Too small")
        config.DEFAULT_SL_POINTS = points
        await update.message.reply_text(
            f"✅ DEFAULT_SL_POINTS set to `{points}` pts",
            parse_mode=ParseMode.MARKDOWN,
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/setsl <points>` (min 100)\nExample: `/setsl 800`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_settp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    import config
    try:
        ratio = float(context.args[0])
        if ratio < 1.0:
            raise ValueError("Too low")
        config.TP_RATIO = ratio
        await update.message.reply_text(
            f"✅ TP_RATIO set to `{ratio}x`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/settp <ratio>` (min 1.0)\nExample: `/settp 2.5`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _requires_auth(update.effective_chat.id):
        return
    log_path = os.path.join(os.path.dirname(__file__), "bot_logs.txt")
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()
        last = "".join(lines[-20:]).strip()
        if not last:
            last = "(log is empty)"
        await update.message.reply_text(f"```\n{last[-3900:]}\n```",
                                        parse_mode=ParseMode.MARKDOWN)
    except FileNotFoundError:
        await update.message.reply_text("❌ Log file not found yet.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*APA Scalping Bot Commands*\n\n"
        "/start — Resume trading\n"
        "/stop  — Pause new trades\n"
        "/status — Account info & P&L\n"
        "/trades — List open positions\n"
        "/close  — Close all positions\n"
        "/setsl `<pts>` — Change SL distance\n"
        "/settp `<ratio>` — Change TP ratio\n"
        "/log   — Last 20 log lines\n"
        "/help  — This message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────────────────────
#  Startup greeting
# ─────────────────────────────────────────────────────────────────────────────

async def _send_startup_message(app: Application) -> None:
    if not CHAT_ID:
        return
    acc = mt5.account_info()
    bal = f"${acc.balance:.2f}" if acc else "unknown"
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "🤖 *APA Scalping Bot v2 Online*\n"
            f"Balance: `{bal}`\n"
            "Send /help for commands."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Run the Telegram bot (blocking — call from a dedicated thread)
# ─────────────────────────────────────────────────────────────────────────────

def run_telegram_bot() -> None:
    """
    Build and start the Telegram Application.
    This function BLOCKS until the bot is stopped.
    Call it from a background thread via run.py.
    """
    global _bot_instance, _event_loop

    if not BOT_TOKEN:
        print("[Telegram] BOT_TOKEN not set in .env — Telegram disabled.")
        return

    # Build the app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("close",  cmd_close_all))
    app.add_handler(CommandHandler("setsl",  cmd_setsl))
    app.add_handler(CommandHandler("settp",  cmd_settp))
    app.add_handler(CommandHandler("log",    cmd_log))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Store bot + loop for cross-thread alerts
    _bot_instance = app.bot

    async def _inner():
        global _event_loop
        _event_loop = asyncio.get_running_loop()
        await _send_startup_message(app)
        # Register the send function into main.py
        from main import register_telegram_alert
        register_telegram_alert(_send_alert_sync)
        # Run polling indefinitely
        await app.run_polling(drop_pending_updates=True)

    asyncio.run(_inner())
