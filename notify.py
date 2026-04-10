"""
notify.py — Telegram notification helper
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env
"""

import os
import re
import requests
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
from dotenv import load_dotenv

load_dotenv()
_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def log(message: str):
    """Print to console/log only — does NOT send to Telegram."""
    timestamp = datetime.now(_IST).strftime("%H:%M:%S IST")
    print(f"[{timestamp}] {_strip_html(message)}")


def send(message: str, silent: bool = False) -> bool:
    """
    Send a message to Telegram AND print to console.
    silent=True → print to console only, skip Telegram (for debug/intermediate steps).
    """
    timestamp = datetime.now(_IST).strftime("%H:%M:%S IST")
    print(f"[{timestamp}] {_strip_html(message)[:120]}")

    if silent:
        return True   # console-only; do not send to Telegram

    if not _BOT_TOKEN or not _CHAT_ID:
        return True   # Telegram not configured — silent no-op

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    _CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  Telegram send failed: {e}")
        return False
