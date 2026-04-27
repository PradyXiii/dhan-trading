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

    Critical alerts (containing 🚨 ❌ ⚠️) are written to data/critical_alerts.log
    ONLY when Telegram is unreachable — it is a failure-mode audit trail, not a
    mirror of every message. health_ping.py treats any recent write to this file
    as evidence of a Telegram outage.
    """
    timestamp = datetime.now(_IST).strftime("%H:%M:%S IST")
    # Stdout echo cap — Telegram itself receives the full message (4096 char limit).
    # Cap stdout to keep cron logs readable while still showing full lever pipeline output.
    print(f"[{timestamp}] {_strip_html(message)[:2000]}")

    if silent:
        return True   # console-only; do not send to Telegram

    if not _BOT_TOKEN or not _CHAT_ID:
        # Telegram creds missing → log the failure (so health_ping can see it)
        # and return False so callers can detect that the alert never left the box.
        _write_alert_log(f"[CREDS MISSING — alert not sent]\n{message}")
        return False

    is_critical = any(m in message for m in _CRITICAL_MARKERS)

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
        if resp.status_code == 200:
            return True
        # Non-200: previously only critical alerts were mirrored to the
        # local fallback log. But operationally important non-critical
        # alerts (daily reports, lever status) also vanish on 429/5xx →
        # we lose them silently. Write a short stamp for non-critical
        # failures too, so health_ping can detect persistent outages.
        _write_alert_log(message if is_critical
                         else f"[non-critical send failed HTTP {resp.status_code}]\n"
                              f"{message[:500]}")
        return False
    except Exception as e:
        print(f"  Telegram send failed: {e}")
        _write_alert_log(message if is_critical
                         else f"[non-critical send exception: {e}]\n{message[:500]}")
        return False
