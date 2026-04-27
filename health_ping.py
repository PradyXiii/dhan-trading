#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
health_ping.py — Pre-market system heartbeat at 9:05 AM IST
=============================================================
Runs 25 minutes before the 9:30 AM trade. Checks that every dependency
the auto_trader needs is healthy, then sends a single Telegram "all-clear"
(or critical alert if something is broken).

Checks performed:
  1. Dhan token — live API call to GET /v2/fundlimit
  2. Signal freshness — signals_ml.csv (or signals.csv) has a recent row
  3. Available capital — warns if balance < ₹5,000 (can't open even 1 lot)
  4. Lock file — warns if /tmp/auto_trader.lock is stale (>1 hour old)
  5. Critical alert log — reports if data/critical_alerts.log was written
     since yesterday (previous session had Telegram failures)

Cron (9:05 AM IST = 3:35 AM UTC, Mon–Fri):
  35 3 * * 1-5 cd ~/dhan-trading && python3 health_ping.py >> logs/health_ping.log 2>&1
"""
import os
import sys
import time
import requests
import pandas as pd
from datetime import date, datetime, timezone, timedelta
from dotenv import load_dotenv

import notify

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")
HEADERS   = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR   = "data"
_HERE      = os.path.dirname(os.path.abspath(__file__))
_LOCK_FILE = "/tmp/auto_trader.lock"
_IST       = timezone(timedelta(hours=5, minutes=30))

MIN_CAPITAL_WARN = 5_000   # ₹5,000 — can't open even 1 lot below this


# NSE Trading Holidays 2026 — update each December from NSE's annual circular.
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 19),   # Chhatrapati Shivaji Maharaj Jayanti
    date(2026, 3, 20),   # Holi
    date(2026, 4,  3),   # Good Friday
    date(2026, 4,  6),   # Ram Navami
    date(2026, 4, 14),   # Dr. B.R. Ambedkar Jayanti
    date(2026, 5,  1),   # Maharashtra Day
    date(2026, 6, 27),   # Bakri Id (tentative)
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 27),   # Ganesh Chaturthi
    date(2026, 10,  2),  # Gandhi Jayanti
    date(2026, 10, 21),  # Dussehra (tentative)
    date(2026, 11,  1),  # Diwali Laxmi Pujan (tentative)
    date(2026, 11,  2),  # Diwali Balipratipada (tentative)
    date(2026, 11, 24),  # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),  # Christmas
}


def _is_trading_day() -> bool:
    today = datetime.now(_IST).date()
    if today.weekday() >= 5:
        return False
    return today not in NSE_HOLIDAYS_2026


def _time_to_trade() -> str:
    """Return human-readable time until next 9:30 AM IST trade window."""
    now = datetime.now(_IST)
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= target:
        # Past today's window — move to next trading day
        target += timedelta(days=1)
        while target.weekday() >= 5 or target.date() in NSE_HOLIDAYS_2026:
            target += timedelta(days=1)
    diff = target - now
    hrs, rem = divmod(int(diff.total_seconds()), 3600)
    mins = rem // 60
    if hrs == 0:
        return f"~{mins} min"
    if diff.days >= 1:
        return target.strftime("%a %d %b 9:30 AM IST")
    return f"~{hrs}h {mins}m"


def _check_token() -> tuple:
    """
    Returns (ok: bool, capital: float, message: str).
    Calls GET /v2/fundlimit — a live API call that exercises the token.
    """
    if not TOKEN or not CLIENT_ID:
        return False, 0.0, "DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID missing from .env"
    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                            headers=HEADERS, timeout=10)
    except Exception as e:
        return False, 0.0, f"Dhan API unreachable: {e}"

    if resp.status_code == 401:
        return False, 0.0, "Token EXPIRED (401) — regenerate at dhan.co → API Settings"
    if resp.status_code != 200:
        return False, 0.0, f"Dhan API returned HTTP {resp.status_code}: {resp.text[:80]}"

    d = resp.json()
    # Dhan v2 historically returned "availabelBalance" (typo); newer versions
    # may switch to the correct "availableBalance". Check both — and use
    # explicit None checks so a legitimate ₹0 balance is preserved (vs the
    # `or` chain which flipped 0.0 to the next fallback). Final fallback
    # logs a warning so silent ₹0 reports are caught.
    raw = (
        d.get("availabelBalance")
        if d.get("availabelBalance") is not None
        else d.get("availableBalance")
        if d.get("availableBalance") is not None
        else d.get("net")
    )
    if raw is None:
        return False, 0.0, (
            f"Dhan fundlimit response missing balance keys "
            f"(availabelBalance / availableBalance / net): {list(d)[:6]}"
        )
    return True, float(raw), "OK"


def _check_signal() -> tuple:
    """
    Returns (ok, signal, score, ml_conf, days_old, src_filename).
    Reads the latest row from signals_ml.csv (falls back to signals.csv).
    """
    for csv_path in [f"{DATA_DIR}/signals_ml.csv", f"{DATA_DIR}/signals.csv"]:
        if not os.path.exists(csv_path):
            continue
        try:
            df = pd.read_csv(csv_path)
            if df.empty:
                continue
            row = df.iloc[-1]
            sig_date_str = str(row.get("date", ""))
            signal  = str(row.get("signal", "?")).upper()
            score   = int(row.get("score", 0) if pd.notna(row.get("score", 0)) else 0)
            ml_conf = float(row.get("ml_conf", 0) if pd.notna(row.get("ml_conf", 0)) else 0)

            try:
                sig_date = date.fromisoformat(sig_date_str)
                days_old = (datetime.now(_IST).date() - sig_date).days
            except Exception:
                days_old = 99

            return True, signal, score, ml_conf, days_old, os.path.basename(csv_path)
        except Exception:
            continue

    return False, "?", 0, 0.0, 99, "Both signal CSVs missing — run data_fetcher.py + signal_engine.py"


def _check_lock() -> str | None:
    """Return a warning string if lock file is stale, else None."""
    if not os.path.exists(_LOCK_FILE):
        return None
    age_secs = time.time() - os.path.getmtime(_LOCK_FILE)
    if age_secs > 3600:
        return f"/tmp/auto_trader.lock is {age_secs/60:.0f} min old (possible stale lock from previous crash)"
    return None


def _check_critical_log() -> str | None:
    """
    Return a summary if data/critical_alerts.log was written today or yesterday
    (indicates Telegram was down during a previous session).
    Empty files are ignored — truncation leaves a fresh mtime on a 0-byte file
    and would otherwise false-alarm.
    """
    log_path = os.path.join(_HERE, DATA_DIR, "critical_alerts.log")
    if not os.path.exists(log_path):
        return None
    size_bytes = os.path.getsize(log_path)
    if size_bytes == 0:
        return None
    mtime = os.path.getmtime(log_path)
    age_hours = (time.time() - mtime) / 3600
    if age_hours <= 48:
        size_kb = size_bytes / 1024
        return (f"critical_alerts.log was updated {age_hours:.0f}h ago "
                f"({size_kb:.1f} KB) — Telegram may have been down during a recent session.")
    return None


def main():
    today_label = datetime.now(_IST).strftime("%d %b %Y (%A)")
    eta         = _time_to_trade()
    notify.log(f"Health ping — {today_label}")

    if not _is_trading_day():
        notify.log("Not a trading day — skipping health ping.")
        return

    # Run all checks
    token_ok, capital, token_msg  = _check_token()
    signal_ok, signal, score, ml_conf, days_old, sig_src = _check_signal()
    lock_warn    = _check_lock()
    crit_log_msg = _check_critical_log()

    # ── Build status lines ────────────────────────────────────────────────────
    issues = []

    # Token check
    if not token_ok:
        issues.append(f"🚨 TOKEN: {token_msg}")

    # Capital check
    if token_ok and capital < MIN_CAPITAL_WARN:
        issues.append(f"⚠️ CAPITAL: ₹{capital:,.0f} — too low to trade. Add funds immediately.")

    # Signal check
    if not signal_ok:
        issues.append(f"🚨 SIGNAL: {sig_src}")
    elif days_old >= 2:
        issues.append(f"⚠️ SIGNAL: {days_old}d old ({sig_src}) — run data_fetcher.py")

    # Lock check
    if lock_warn:
        issues.append(f"⚠️ LOCK: {lock_warn}")

    # Critical alert log
    if crit_log_msg:
        issues.append(f"⚠️ ALERTS: {crit_log_msg}")

    # ── Send Telegram ─────────────────────────────────────────────────────────
    if issues:
        issue_lines = "\n".join(f"  {i}" for i in issues)
        notify.send(
            f"🚨 <b>Pre-Market Alert — {today_label}</b>\n\n"
            f"System issues detected before 9:30 AM trade:\n\n"
            f"{issue_lines}\n\n"
            f"<b>Fix these before the trade fires in {eta}.</b>"
        )
    else:
        # All-clear heartbeat — one concise line
        signal_label = signal if signal_ok else "?"
        score_label  = f"{score:+d}" if signal_ok else "?"
        capital_str  = f"₹{capital:,.0f}" if token_ok else "?"
        sig_age      = f"{days_old}d old" if days_old > 0 else "today"
        conf_str     = f"  ·  ML {ml_conf:.0%}" if signal_ok and ml_conf > 0 else ""
        # When signal is from yesterday's evolver, warn that 9:30 AM will re-predict
        stale_note   = (
            f"\n<i>⚠️ This is last night's model — auto trader re-runs fresh prediction at 9:30 AM "
            f"with morning data (VIX open, gap) and may pick a different direction.</i>"
        ) if days_old >= 1 else ""

        notify.send(
            f"💚 <b>System OK — {today_label}</b>\n\n"
            f"Token      ✓  (capital {capital_str})\n"
            f"Signal     {signal_label}  (score {score_label}{conf_str}  ·  {sig_age}  ·  {sig_src})\n"
            f"{stale_note}\n"
            f"<i>Auto trader fires in {eta}.</i>"
        )


if __name__ == "__main__":
    main()
