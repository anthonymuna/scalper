"""
platform_utils.py — Cross-platform compatibility layer
=======================================================
Handles differences between:
  Windows native  : MT5 installed normally
  Linux + Wine    : MT5 installed via Wine (Kali, Ubuntu, etc.)
  Linux native    : MT5 Python package talks to Wine MT5 process

Key differences handled:
  1. MT5 terminal path detection (Windows vs Wine prefix)
  2. Python MetaTrader5 package: Windows-only binary
     → On Linux, must use a compatibility shim or run via Wine
  3. File path separators (os.path handles this, but Wine paths need mapping)
  4. .env credential loading (same on both, just verify encoding)
  5. Log file location (relative paths work on both)
  6. Timezone handling (Wine uses system tz — must force UTC)
  7. Process detection (check if MT5 terminal is running)

WINE SETUP REQUIRED ON LINUX:
  pip install metaTrader5  — this is a Windows-only wheel
  On Linux you must either:
    Option A (recommended): Run entire bot inside Wine Python
      wine python run.py
    Option B: Use MT5 Linux bridge (community package mt5linux)
      pip install mt5linux
      Then replace: import MetaTrader5 as mt5
      With:         from mt5linux import MetaTrader5 as mt5
    Option C: Run bot natively on Windows VPS (simplest for production)
"""

from __future__ import annotations
import os
import sys
import platform
import subprocess
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
#  PLATFORM DETECTION
# ─────────────────────────────────────────────────────────────────────────────

IS_WINDOWS  = sys.platform == "win32"
IS_LINUX    = sys.platform.startswith("linux")
IS_MAC      = sys.platform == "darwin"

# Detect Wine environment (Linux running Windows binaries)
# Wine sets WINEPREFIX or WINEDEBUG env vars, or we can check for wine binary
IS_WINE     = IS_LINUX and (
    os.environ.get("WINEPREFIX") is not None or
    os.environ.get("WINEDEBUG")  is not None or
    _wine_exists()
)

def _wine_exists() -> bool:
    try:
        result = subprocess.run(
            ["which", "wine"], capture_output=True, text=True, timeout=2
        )
        return result.returncode == 0
    except Exception:
        return False

# Re-evaluate with the function available
IS_WINE = IS_LINUX and (
    os.environ.get("WINEPREFIX") is not None or
    _wine_exists()
)


def get_platform_name() -> str:
    if IS_WINDOWS:
        return "Windows"
    if IS_WINE:
        return "Linux+Wine"
    if IS_LINUX:
        return "Linux"
    if IS_MAC:
        return "macOS"
    return platform.system()


# ─────────────────────────────────────────────────────────────────────────────
#  MT5 IMPORT — handles Windows native vs Linux+Wine
# ─────────────────────────────────────────────────────────────────────────────

def get_mt5_module():
    """
    Return the correct MT5 module for this platform.

    Windows:    import MetaTrader5  (official Metaquotes package)
    Linux+Wine: try mt5linux bridge first, fallback to MetaTrader5
                (works if bot is running inside Wine Python)
    Linux:      mt5linux bridge required
    """
    if IS_WINDOWS:
        import MetaTrader5 as mt5
        return mt5

    # Linux — try mt5linux bridge (community package for Linux)
    try:
        from mt5linux import MetaTrader5 as mt5
        return mt5
    except ImportError:
        pass

    # Fallback — may work if running inside Wine Python
    try:
        import MetaTrader5 as mt5
        return mt5
    except ImportError:
        raise ImportError(
            "Cannot import MetaTrader5 on Linux.\\n"
            "Options:\\n"
            "  1. pip install mt5linux  (Linux bridge — recommended)\\n"
            "  2. Run: wine python run.py  (run entire bot in Wine)\\n"
            "  3. Use a Windows VPS for production\\n"
            "See README.md for full setup guide."
        )


# ─────────────────────────────────────────────────────────────────────────────
#  MT5 TERMINAL PATH DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def find_mt5_terminal() -> Path | None:
    """
    Find the MT5 terminal.exe path for this platform.
    Used when mt5.initialize(path=...) is needed.

    Windows: standard Program Files locations
    Linux+Wine: Wine prefix locations
    """
    candidates: list[Path] = []

    if IS_WINDOWS:
        # Standard Windows install locations
        for base in [
            os.environ.get("PROGRAMFILES",  "C:/Program Files"),
            os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]:
            if base:
                candidates += [
                    Path(base) / "MetaTrader 5" / "terminal64.exe",
                    Path(base) / "MetaTrader 5" / "terminal.exe",
                    Path(base) / "Headway" / "terminal64.exe",
                    Path(base) / "Headway MT5" / "terminal64.exe",
                ]

    elif IS_LINUX:
        # Wine prefix locations
        wine_prefix = Path(os.environ.get(
            "WINEPREFIX",
            Path.home() / ".wine"
        ))
        drive_c = wine_prefix / "drive_c"

        candidates += [
            drive_c / "Program Files" / "MetaTrader 5" / "terminal64.exe",
            drive_c / "Program Files" / "MetaTrader 5" / "terminal.exe",
            drive_c / "Program Files (x86)" / "MetaTrader 5" / "terminal64.exe",
            drive_c / "Program Files" / "Headway" / "terminal64.exe",
            drive_c / "Program Files" / "Headway MT5" / "terminal64.exe",
            drive_c / "users" / os.environ.get("USER", "user") /
                "AppData" / "Roaming" / "MetaQuotes" /
                "Terminal" / "terminal64.exe",
        ]

    for path in candidates:
        if path.exists():
            return path

    return None


def get_mt5_init_kwargs() -> dict:
    """
    Build kwargs for mt5.initialize() that work on both platforms.
    Pass terminal path explicitly on Linux to help Wine locate it.
    """
    kwargs: dict = {}
    terminal = find_mt5_terminal()
    if terminal:
        kwargs["path"] = str(terminal)
    return kwargs


# ─────────────────────────────────────────────────────────────────────────────
#  PATH UTILITIES — cross-platform safe
# ─────────────────────────────────────────────────────────────────────────────

# All paths use pathlib.Path internally — converts to correct separator
# os.path.join already handles this but Path is more explicit

BOT_DIR  = Path(__file__).parent.resolve()
LOG_FILE = BOT_DIR / "bot_logs.txt"
ENV_FILE = BOT_DIR / ".env"


def get_log_path() -> str:
    """Cross-platform log file path."""
    return str(LOG_FILE)


def get_env_path() -> str:
    """Cross-platform .env file path."""
    return str(ENV_FILE)


# ─────────────────────────────────────────────────────────────────────────────
#  TIMEZONE — force UTC regardless of system tz
# ─────────────────────────────────────────────────────────────────────────────

def ensure_utc_timezone() -> None:
    """
    Force UTC timezone for the process.
    Critical on Linux+Wine where system tz may differ from Windows tz.
    Wine does NOT always inherit system UTC setting.

    Call this at bot startup before any datetime operations.
    """
    os.environ["TZ"] = "UTC"
    if IS_LINUX:
        try:
            import time
            time.tzset()
        except AttributeError:
            pass  # Windows doesn't have time.tzset — not needed there


# ─────────────────────────────────────────────────────────────────────────────
#  PROCESS CHECK — is MT5 terminal running?
# ─────────────────────────────────────────────────────────────────────────────

def is_mt5_terminal_running() -> bool:
    """
    Check if the MT5 terminal process is active.
    Useful for startup validation.
    """
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
                capture_output=True, text=True, timeout=5
            )
            return "terminal64.exe" in result.stdout
        except Exception:
            return False

    elif IS_LINUX:
        # Check Wine process list
        try:
            result = subprocess.run(
                ["pgrep", "-f", "terminal64.exe"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            # Try wineserver check
            try:
                result = subprocess.run(
                    ["wineserver", "-l"],
                    capture_output=True, text=True, timeout=5
                )
                return "terminal64" in result.stdout
            except Exception:
                return False

    return False


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_environment() -> tuple[bool, list[str]]:
    """
    Run pre-flight checks for this platform.
    Returns (all_ok: bool, issues: list[str])
    """
    issues = []

    # 1. Python version
    if sys.version_info < (3, 10):
        issues.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            f"detected. Python 3.10+ required."
        )

    # 2. .env file exists
    if not ENV_FILE.exists():
        issues.append(
            f".env file not found at {ENV_FILE}. "
            "Copy .env.example to .env and fill in credentials."
        )

    # 3. Required packages
    required = ["MetaTrader5", "pandas", "numpy", "dotenv"]
    if IS_LINUX:
        required.append("mt5linux")
    for pkg in required:
        try:
            __import__(pkg.replace("-", "_").lower())
        except ImportError:
            # mt5linux is optional (only needed on Linux without Wine Python)
            if pkg == "mt5linux" and IS_WINE:
                continue
            issues.append(f"Missing package: {pkg}  →  pip install {pkg}")

    # 4. MT5 terminal running (warn only — don't block)
    if not is_mt5_terminal_running():
        issues.append(
            "WARNING: MT5 terminal64.exe not detected as running. "
            + ("Start MetaTrader 5 before running the bot."
               if IS_WINDOWS else
               "Start MT5 via Wine: wine 'C:/Program Files/MetaTrader 5/terminal64.exe'")
        )

    # 5. On Linux, check Wine is available
    if IS_LINUX and not IS_WINE:
        issues.append(
            "WARNING: Wine not detected. "
            "MetaTrader5 Python package requires Wine on Linux. "
            "Install: sudo apt install wine  OR  pip install mt5linux"
        )

    all_ok = not any(not i.startswith("WARNING") for i in issues)
    return all_ok, issues


def print_platform_info() -> None:
    """Print platform diagnostics at bot startup."""
    print(f"Platform   : {get_platform_name()}")
    print(f"Python     : {sys.version.split()[0]}")
    print(f"OS         : {platform.system()} {platform.release()}")

    terminal = find_mt5_terminal()
    print(f"MT5 Path   : {terminal or 'not found (will use default)'}")
    print(f"Bot Dir    : {BOT_DIR}")
    print(f"Log File   : {LOG_FILE}")
    print(f"Wine       : {'yes' if IS_WINE else 'no'}")

    ok, issues = validate_environment()
    if issues:
        print("\nEnvironment checks:")
        for issue in issues:
            prefix = "  ⚠" if issue.startswith("WARNING") else "  ✗"
            print(f"{prefix}  {issue}")
    else:
        print("Environment: ✓ All checks passed")
