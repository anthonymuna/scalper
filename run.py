"""
run.py — Single entry point for the APA Scalping Bot.

Starts two threads concurrently:
  1. Trading bot  (main.py → start_bot)
  2. Telegram bot (telegram_bot.py → run_telegram_bot)

Usage:
    python run.py

Stop:  Ctrl+C  (or send /stop then /start via Telegram)
"""

import threading
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

from main          import start_bot
from telegram_bot  import run_telegram_bot


def main():
    print("=" * 60)
    print("  APA Gold Scalping Bot v2")
    print("  Starting trading engine + Telegram controller...")
    print("=" * 60)

    # ── Telegram in a daemon thread ────────────────────────────────────────
    tg_thread = threading.Thread(
        target=run_telegram_bot,
        name="TelegramBot",
        daemon=True,   # dies with the main thread
    )
    tg_thread.start()

    # ── Trading bot runs on the main thread (blocking) ────────────────────
    try:
        start_bot()
    except KeyboardInterrupt:
        print("\nShutdown requested — stopping bot.")
    finally:
        mt5.shutdown()
        print("Done.")


if __name__ == "__main__":
    main()
