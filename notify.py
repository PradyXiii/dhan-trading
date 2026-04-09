"""
notify.py — Telegram notification helper
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env
"""

import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")


def send(message: str, silent: bool = False) -> bool:
    """
    Send a Telegram message. Returns True on success.
    If credentials not set, just prints to stdout (dev mode).
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    full_msg  = f"[{timestamp}] {message}"

    # Always print to console / log file
    print(full_msg)

    if not _BOT_TOKEN or not _CHAT_ID:
        return True  # silent no-op if Telegram not configured

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":              _CHAT_ID,
                "text":                 full_msg,
                "parse_mode":           "HTML",
                "disable_notification": silent,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  Telegram send failed: {e}")
        return False
