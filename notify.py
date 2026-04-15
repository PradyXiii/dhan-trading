# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
notify.py — Telegram notification helper
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env

If Telegram is down, critical alerts (🚨 ❌ ⚠️) are written to
data/critical_alerts.log as a local fallback.
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

# Local fallback log for critical alerts when Telegram is unreachable
_HERE         = os.path.dirname(os.path.abspath(__file__))
_ALERT_LOG    = os.path.join(_HERE, "data", "critical_alerts.log")
_CRITICAL_MARKERS = ("🚨", "❌", "⚠️", "CRITICAL", "FAILED", "ERROR")


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _write_alert_log(message: str):
    """Write message to local critical_alerts.log — fallback when Telegram is down."""
    try:
        os.makedirs(os.path.dirname(_ALERT_LOG), exist_ok=True)
        timestamp = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
        with open(_ALERT_LOG, "a") as f:
            f.write(f"\n[{timestamp}]\n{_strip_html(message)}\n{'─'*60}\n")
    except Exception:
        pass   # if even file write fails, nothing we can do


def log(message: str):
    """Print to console/log only — does NOT send to Telegram."""
    timestamp = datetime.now(_IST).strftime("%H:%M:%S IST")
    print(f"[{timestamp}] {_strip_html(message)}")


def send(message: str, silent: bool = False) -> bool:
    """
    Send a message to Telegram AND print to console.
    silent=True → print to console only, skip Telegram (for debug/intermediate steps).

    Critical alerts (containing 🚨 ❌ ⚠️) are ALWAYS written to
    data/critical_alerts.log as a fallback if Telegram is unreachable.
    """
    timestamp = datetime.now(_IST).strftime("%H:%M:%S IST")
    print(f"[{timestamp}] {_strip_html(message)[:120]}")

    # Always persist critical messages locally regardless of Telegram status
    if any(m in message for m in _CRITICAL_MARKERS):
        _write_alert_log(message)

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
        print(f"  Telegram send failed: {e}  → saved to data/critical_alerts.log")
        return False
