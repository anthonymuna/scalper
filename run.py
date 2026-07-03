"""
run.py — NGAO Scalper Bot v4.2 — Single entry point
=====================================================
Works on:
  Windows native  : python run.py
  Linux + Wine    : wine python run.py   OR  python run.py (with mt5linux)
  Kali Linux      : same as Linux

Starts:
  Thread 1 — Trading engine  (main.py → start_bot)
  Thread 2 — Telegram controller (telegram_bot.py → run_telegram_bot)

Usage:
  python run.py              # normal start
  python run.py --check      # validate environment only, don't trade
"""

import sys
import os
import threading
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env before anything else ────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)

# ── Platform detection and validation ─────────────────────────────────────────
from platform_utils import (
    ensure_utc_timezone,
    print_platform_info,
    validate_environment,
    get_platform_name,
    IS_WINDOWS, IS_LINUX, IS_WINE,
)

ensure_utc_timezone()   # Must be before any datetime imports


def main():
    check_only = "--check" in sys.argv

    print("=" * 62)
    print("  NGAO Scalper Bot v4.2")
    print("  Dual Engine: APA/SMC + ICT | Pure Price Action")
    print("=" * 62)
    print_platform_info()
    print()

    ok, issues = validate_environment()

    if not ok:
        print("\n[STARTUP BLOCKED] Fix the issues above before trading.")
        sys.exit(1)

    if check_only:
        print("\n[--check mode] Environment OK. Not starting bot.")
        sys.exit(0)

    # ── Platform-specific MT5 import via platform_utils ───────────────────
    from platform_utils import get_mt5_module
    mt5 = get_mt5_module()

    from main         import start_bot
    from telegram_bot import run_telegram_bot

    print("\nStarting engines...\n")

    # ── Telegram runs in a daemon thread ──────────────────────────────────
    tg_thread = threading.Thread(
        target=run_telegram_bot,
        name="TelegramBot",
        daemon=True,
    )
    tg_thread.start()

    # ── Trading bot runs on main thread (blocking) ─────────────────────────
    try:
        start_bot()
    except KeyboardInterrupt:
        print("\nShutdown requested — stopping.")
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
        print("NGAO Scalper stopped.")


if __name__ == "__main__":
    main()
