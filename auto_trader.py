#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
auto_trader.py — BankNifty Options Full Automation
====================================================
Runs every trading day at 9:15 AM IST via cron.
No human interaction required.

Flow:
  1. Fetch latest market data + regenerate signal
  2. If NONE → Telegram "No trade today" → exit
  3. If CALL/PUT:
       a. Get available capital from Dhan
       b. Find ATM option security_id via option chain API
       c. Calculate lots / SL / target
       d. Send ONE clean trade-details message to Telegram
       e. Place Dhan Super Order (entry + SL + TP in one shot)
       f. Send ONE result message to Telegram

Cron (9:15 AM IST = 3:45 AM UTC):
  45 3 * * 1-5 cd ~/dhan-trading && python3 auto_trader.py >> logs/auto_trader.log 2>&1

Add --dry-run flag for testing without placing real orders.
"""

import os
import sys
import json
import time
import fcntl
import atexit
import subprocess
import requests
import pandas as pd
from datetime import date, datetime, timedelta
from math import floor, sqrt
from dotenv import load_dotenv

import notify

load_dotenv()

# ── Cron lock — prevent double execution if previous run hasn't finished ──────
_LOCK_FILE = "/tmp/auto_trader.lock"
_lock_fh   = None

def _acquire_lock():
    global _lock_fh
    # Warn if lock file is very old — fcntl locks auto-release on process death so
    # this won't block the trade, but it indicates the previous run was slow/crashed.
    if os.path.exists(_LOCK_FILE):
        age_secs = time.time() - os.path.getmtime(_LOCK_FILE)
        if age_secs > 3600:
            notify.log(
                f"⚠️ Lock file is {age_secs/60:.0f} min old — previous run may have been "
                f"slow or crashed. fcntl lock auto-released by OS; proceeding."
            )
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        notify.log("Another auto_trader instance is already running — exiting to avoid double trade.")
        sys.exit(0)

def _release_lock():
    if _lock_fh:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
            _lock_fh.close()
        except Exception:
            pass

atexit.register(_release_lock)
_acquire_lock()

# ── Pre-flight safety helpers (called from main before any trade) ─────────────

def _check_exit_marker():
    """
    Verify that exit_positions.py ran successfully yesterday.
    If marker is missing AND a BN position is still open → CRITICAL alert + exit.
    Skips check when yesterday was a weekend or NSE holiday (no trading, no exit needed).
    Called early in main() before any market data or order work.
    """
    yesterday = date.today() - timedelta(days=1)
    # No exit runs on weekends — skip
    if yesterday.weekday() >= 5:
        return
    # No exit needed on holidays — skip (NSE_HOLIDAYS_2026 defined later in file)
    # We check this lazily: if marker exists we skip fast before NSE_HOLIDAYS_2026 is needed
    marker = os.path.join(DATA_DIR, f"exit_completed_{yesterday.isoformat()}.marker")
    if os.path.exists(marker):
        return  # All good

    # Marker missing — check positions API before raising alarm
    # (may be a holiday, or no trade was placed yesterday → marker simply not written)
    notify.log(f"Exit marker for {yesterday} not found — checking positions API...")
    try:
        resp = requests.get("https://api.dhan.co/v2/positions",
                            headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            positions = data if isinstance(data, list) else data.get("data", [])
            bn_open = [
                p for p in positions
                if int(p.get("netQty", 0)) > 0
                and p.get("exchangeSegment", "") == "NSE_FNO"
                and "BANKNIFTY" in str(
                    p.get("tradingSymbol", p.get("securityId", ""))
                ).upper()
            ]
            if bn_open:
                syms = ", ".join(
                    p.get("tradingSymbol", str(p.get("securityId", "?")))
                    for p in bn_open
                )
                notify.send(
                    f"🚨 <b>CRITICAL: Open Position from Yesterday!</b>\n\n"
                    f"exit_positions.py did NOT run successfully on {yesterday}.\n"
                    f"Position still open: <code>{syms}</code>\n\n"
                    f"Close manually on Dhan app <b>IMMEDIATELY</b>.\n"
                    f"Today's trade is BLOCKED until you resolve this."
                )
                sys.exit(1)
            else:
                notify.log(
                    f"Exit marker missing for {yesterday} but no open BN position "
                    f"found — likely no trade yesterday. Proceeding."
                )
        else:
            notify.log(
                f"Exit marker check: positions API returned {resp.status_code} "
                f"— cannot confirm. Proceeding with caution."
            )
    except Exception as e:
        notify.log(f"Exit marker check: positions API failed ({e}) — proceeding.")


def _check_no_existing_position() -> bool:
    """
    Return True if a BankNifty FNO position (netQty > 0) already exists.
    Prevents placing a duplicate order if auto_trader.py is invoked twice.
    Fails OPEN (returns False) on connectivity issues — don't block the trade
    just because the API is temporarily unreachable.
    """
    try:
        resp = requests.get("https://api.dhan.co/v2/positions",
                            headers=HEADERS, timeout=10)
    except Exception as e:
        notify.log(f"Double-position check: positions API unreachable ({e}) — assuming no open position.")
        return False

    if resp.status_code != 200:
        notify.log(f"Double-position check: positions API returned {resp.status_code} — assuming none.")
        return False

    data = resp.json()
    positions = data if isinstance(data, list) else data.get("data", [])
    bn_open = [
        p for p in positions
        if int(p.get("netQty", 0)) > 0
        and p.get("exchangeSegment", "") == "NSE_FNO"
        and "BANKNIFTY" in str(
            p.get("tradingSymbol", p.get("securityId", ""))
        ).upper()
    ]
    return len(bn_open) > 0


def _verify_order_status(order_id: str, symbol: str):
    """
    Check order status ~30s after placement. Alert if REJECTED or CANCELLED.
    Dhan GET /v2/orders/{order_id} returns the order object.
    """
    try:
        resp = requests.get(f"https://api.dhan.co/v2/orders/{order_id}",
                            headers=HEADERS, timeout=10)
    except Exception as e:
        notify.log(f"Order status check failed ({e}) — verify manually on Dhan app.")
        return

    if resp.status_code != 200:
        notify.log(f"Order status API returned {resp.status_code} — verify on Dhan app.")
        return

    order = resp.json()
    # Some endpoints return a list, some return a single object
    if isinstance(order, list):
        order = next((o for o in order if str(o.get("orderId")) == str(order_id)), {})

    status = str(order.get("orderStatus", "")).upper()
    if status in ("REJECTED", "CANCELLED"):
        reason = (order.get("omsErrorDescription")
                  or order.get("rejectionReason")
                  or order.get("errorMessage")
                  or "unknown reason")
        notify.send(
            f"🚨 <b>CRITICAL: Order {status}!</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Symbol:   <code>{symbol}</code>\n"
            f"Reason:   {reason}\n\n"
            f"No position was opened. Act immediately on Dhan app."
        )
    elif status in ("TRADED", "PART_TRADED"):
        notify.log(f"Order status verified: {status} — position opened successfully.")
    elif status in ("PENDING", "TRANSIT", ""):
        notify.log(f"Order status: {status or 'not yet updated'} — still settling 30s out. Monitor on Dhan app.")
    else:
        notify.log(f"Order status: {status} — check Dhan app if concerned.")


TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")

DRY_RUN   = "--dry-run" in sys.argv

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR      = "data"
LOT_SIZE      = 30
SL_PCT        = 0.15   # 15% stop-loss on premium
RISK_PCT      = 0.05
MAX_LOTS      = 20
PREMIUM_K     = 0.004
ITM_WALK_MAX  = 2    # Walk up to 200pt ITM when capital is flush (higher delta)

RR = 2.5   # reward:risk ratio — SL=15%, TP=+37.5% of premium (RR=2.5x)
           # Grid result: 2.5x beats 2.0x on all metrics (+₹24L P&L, DD -8.8% vs -12.9%)

# ── ML confidence gate ────────────────────────────────────────────────────────
# Skip trade when ML model probability is below this threshold.
# Default 0.55: skip coin-flip days where max(P_CALL, P_PUT) < 55%.
# Raise to 0.60 for more aggressive filtering (fewer but higher-WR trades).
# Set to 0.0 to disable and trade every signal like before.
ML_CONF_THRESHOLD = 0.55

# ── Adaptive opening-wait parameters ─────────────────────────────────────────
# Root cause of bad 9:15 fills: large BN spot gap → inflated IV at open.
# If |live_spot - yesterday_close| > ENTRY_SPOT_GAP_THRESHOLD, wait proportionally:
#   0.5% gap → 5 min,  0.8% → 8 min,  1.0% → 10 min,  ≥1.2% → 12 min (cap)
# After wait: re-fetch option chain so SL/TP auto-reset to actual fill price.
ENTRY_SPOT_GAP_THRESHOLD = 0.005   # 0.5% BN spot gap (≈280 pts at 56k) triggers wait
ENTRY_WAIT_MAX_MINS      = 12      # never wait beyond 9:15 + 12 = 9:27 AM


# ── Helpers ───────────────────────────────────────────────────────────────────

def die(msg: str):
    notify.send(f"❌ <b>Auto Trader Error</b>\n\n{msg}\n\nCheck manually on Dhan app.")
    sys.exit(1)


def check_credentials():
    if not TOKEN or not CLIENT_ID:
        die("DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID missing from .env")
    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                            headers=HEADERS, timeout=10)
        if resp.status_code == 401:
            die("Dhan token expired (401). Regenerate at dhan.co → API settings.")
        if resp.status_code not in (200, 429):
            notify.log(f"Dhan API check returned {resp.status_code}. Proceeding.")
    except requests.exceptions.ConnectionError:
        die("Cannot reach Dhan API. Check VM internet / DNS.")


# ── Lot-size sanity checker ───────────────────────────────────────────────────

def _check_lot_size():
    """
    Verify LOT_SIZE constant matches the expected BankNifty lot size for today.
    If they differ, send a Telegram alert BEFORE any trade is placed.
    Does NOT block trading — the operator must fix the constant.
    """
    from datetime import date as _d
    import json as _json

    today = _d.today()

    # Baseline timeline (mirrors backtest_engine._baseline_lot_size)
    if today < _d(2024, 11, 20):
        expected = 15
    elif today < _d(2025, 6, 26):
        expected = 30
    elif today < _d(2026, 1, 27):
        expected = 35
    else:
        expected = 30  # Jan 2026 onwards

    # Override file (written by lot_expiry_scanner.py on NSE changes)
    try:
        ov_path = os.path.join(DATA_DIR, "lot_size_overrides.json")
        if os.path.exists(ov_path):
            with open(ov_path) as _f:
                ov = _json.load(_f)
            best_eff = _d(1900, 1, 1)
            for entry in ov.get("active", []):
                try:
                    eff = _d.fromisoformat(entry["effective_from"])
                except Exception:
                    continue
                if eff <= today and eff >= best_eff:
                    expected = int(entry["lot_size"])
                    best_eff = eff
    except Exception:
        pass

    if LOT_SIZE != expected:
        msg = (
            f"🚨 LOT SIZE MISMATCH — auto_trader.py LOT_SIZE={LOT_SIZE} "
            f"but expected {expected} for {today}. "
            f"Update LOT_SIZE in auto_trader.py BEFORE next trade or sizing will be WRONG."
        )
        notify.send(msg)
        notify.log(msg)
    else:
        notify.log(f"Lot-size check OK: LOT_SIZE={LOT_SIZE} matches expected {expected}")


# ── Step 1: Fetch data + generate signal ─────────────────────────────────────

def refresh_data_and_signal():
    notify.log("Fetching latest market data...")
    try:
        r1 = subprocess.run(
            [sys.executable, "data_fetcher.py"],
            capture_output=True, text=True, timeout=60
        )
        if r1.returncode != 0:
            notify.log(f"data_fetcher.py had errors:\n{r1.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        notify.log("data_fetcher.py timed out (60s) — continuing with existing data files")
    except FileNotFoundError:
        notify.log("data_fetcher.py not found — continuing with existing data files")
    except Exception as e:
        notify.log(f"data_fetcher.py launch failed: {e} — continuing with existing data files")

    notify.log("Generating signal...")
    try:
        r2 = subprocess.run(
            [sys.executable, "signal_engine.py"],
            capture_output=True, text=True, timeout=60
        )
        if r2.returncode != 0:
            die(f"signal_engine.py failed:\n{r2.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        die("signal_engine.py timed out (60s). Check if banknifty.csv / nifty50.csv are valid.")
    except FileNotFoundError:
        die("signal_engine.py not found. Check working directory.")

    notify.log("Running ML direction engine...")
    try:
        r3 = subprocess.run(
            [sys.executable, "ml_engine.py", "--predict-today"],
            capture_output=True, text=True, timeout=60
        )
        if r3.returncode != 0:
            notify.log(f"ml_engine.py --predict-today failed (falling back to rule signal):\n{r3.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        notify.log("ml_engine.py timed out (60s) — falling back to rule signal")
    except Exception as e:
        notify.log(f"ml_engine.py failed: {e} — falling back to rule signal")


def _write_today_trade(signal, strike, lots, dte, spot, oracle_premium,
                       sl_price, tp_price, security_id, score, iv=0.0,
                       expiry=None, ml_conf=0.0, order_id=None, order_mode=None):
    """
    Write oracle intent to data/today_trade.json so trade_journal.py can
    compare it against actual fills at EOD.  Overwrites any previous file.
    Data stays on VM only — gitignored.
    """
    payload = {
        "date":           date.today().isoformat(),
        "signal":         signal,
        "strike":         float(strike),
        "lots":           int(lots),
        "dte":            float(dte),
        "expiry":         expiry.isoformat() if expiry else None,
        "spot_at_signal": float(spot),
        "oracle_premium": float(oracle_premium),
        "sl_price":       round(float(sl_price), 2),
        "tp_price":       round(float(tp_price), 2),
        "security_id":    str(security_id),
        "signal_score":   int(score),
        "iv_at_entry":    float(iv),
        "ml_conf":        round(float(ml_conf), 4),
        "order_id":       str(order_id) if order_id else None,
        "order_mode":     str(order_mode) if order_mode else None,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(f"{DATA_DIR}/today_trade.json", "w") as f:
            json.dump(payload, f, indent=2)
        notify.log("trade intent written → data/today_trade.json")
    except Exception as e:
        notify.log(f"Could not write today_trade.json: {e}")


# ── NSE Trading Holidays 2026 ─────────────────────────────────────────────────
# Source: NSE circular (verify + update annually each December).
# nseindia.com → About NSE → NSE Holidays
# IMPORTANT: check tentative moon-based dates (Eid, Diwali, Holi) against the
# official NSE circular for the year — they shift by 1-2 days.
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 19),   # Chhatrapati Shivaji Maharaj Jayanti
    date(2026, 3, 20),   # Holi
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 6),    # Ram Navami
    date(2026, 4, 14),   # Dr. B.R. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 6, 27),   # Bakri Id (tentative — moon-based)
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 27),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 21),  # Dussehra (tentative)
    date(2026, 11, 1),   # Diwali Laxmi Pujan (tentative)
    date(2026, 11, 2),   # Diwali Balipratipada (tentative)
    date(2026, 11, 24),  # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),  # Christmas
}


def _is_trading_day() -> bool:
    """
    Return True if today is an NSE trading day (weekday + not in holiday list).

    REPLACES the old CSV-presence check which was broken: Dhan's historical
    API never returns today's candle at 9:15 AM (daily bar not closed yet),
    so the old check always returned False, firing the holiday guard on every
    real trading day (missed Apr 15 2026 trade).

    Update NSE_HOLIDAYS_2026 each December from the official NSE circular.
    """
    today = date.today()
    if today.weekday() >= 5:            # Saturday / Sunday
        return False
    return today not in NSE_HOLIDAYS_2026


def get_todays_signal() -> tuple:
    """
    Returns (signal_dict, sig_note_str).
    Reads signals_ml.csv (ML direction oracle) with fallback to signals.csv.

    sig_note is empty if today's date matches, or a label like "08 Apr ML" if fallback.
    """
    today = pd.Timestamp(date.today())

    # Try ML signals first, fall back to rule-based if unavailable
    for csv_path, label in [
        (f"{DATA_DIR}/signals_ml.csv", "ML"),
        (f"{DATA_DIR}/signals.csv",    "rule"),
    ]:
        try:
            df = pd.read_csv(csv_path, parse_dates=["date"])
            df = df.drop(columns=["threshold"], errors="ignore")

            row = df[df["date"] == today]
            if not row.empty:
                source = "ML" if "ml" in csv_path else "rule"
                if source == "rule":
                    notify.log("Using rule-based signal (signals_ml.csv unavailable)")
                return row.iloc[0].to_dict(), ""

            # Fallback only for data-pipeline failures (e.g. API outage yesterday).
            # Holidays are handled upstream via _is_trading_day() — we should not
            # reach here on a holiday.
            last     = df.iloc[-1]
            days_gap = (today - last["date"]).days

            if days_gap <= 4:
                note = f"signal from {last['date'].strftime('%d %b')} {label}"
                notify.log(f"Today's signal not in {csv_path} — using {note}")
                if days_gap >= 2:
                    notify.send(
                        f"⚠️ <b>Stale signal ({days_gap}d old)</b>\n\n"
                        f"Today's row missing from {csv_path.split('/')[-1]}.\n"
                        f"Using signal from <b>{last['date'].strftime('%d %b')}</b> — "
                        f"data pipeline may have failed yesterday.\n"
                        f"<i>Check: python3 data_fetcher.py && python3 signal_engine.py</i>"
                    )
                return last.to_dict(), note

        except Exception:
            continue  # try next file

    notify.send(
        f"⚠️ <b>Stale signal</b>\n\n"
        f"Neither signals_ml.csv nor signals.csv has a recent row.\n"
        f"Run data_fetcher.py + signal_engine.py + ml_engine.py manually."
    )
    return None, ""


# ── Step 2: Capital ───────────────────────────────────────────────────────────

def get_capital() -> float:
    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                            headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            d   = resp.json()
            notify.log(f"Fund limit API: {d}")
            bal = (d.get("availabelBalance") or
                   d.get("availableBalance") or
                   d.get("net") or 0)
            try:
                return float(bal)
            except (ValueError, TypeError) as e:
                notify.log(f"Balance field not numeric ({bal!r}): {e} — treating as ₹0")
                return 0.0
        notify.log(f"Fund limit API returned {resp.status_code}: {resp.text[:150]}")
    except requests.exceptions.Timeout:
        notify.log("Fund limit API timed out (10s)")
    except Exception as e:
        notify.log(f"Fund limit API exception: {e}")
    die("Could not fetch available capital from Dhan fund limit API.")


# ── Step 3: Expiry ────────────────────────────────────────────────────────────

def get_expiry() -> date:
    """
    Find the nearest valid BankNifty expiry using the /optionchain/expirylist endpoint.
    Returns the nearest upcoming expiry date. Falls back to last-Tuesday calculation
    if the API is unavailable. Phase 4 (Sep 2025+): monthly, last Tuesday of month.
    """
    today = date.today()
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        if resp.status_code == 200:
            expiries = resp.json().get("data", [])
            # Find the nearest expiry that is today or in the future
            # Guard against malformed date strings from API
            upcoming = []
            for e in expiries:
                try:
                    d = date.fromisoformat(str(e))
                    if d >= today:
                        upcoming.append(d)
                except (ValueError, TypeError):
                    notify.log(f"Skipping malformed expiry date: {e!r}")
            if upcoming:
                expiry = min(upcoming)
                notify.log(f"Expiry from API: {expiry}  (all: {[e for e in expiries[:4]]})")
                return expiry
        notify.log(f"Expiry list API returned {resp.status_code} — falling back to last-Tuesday calc")
    except Exception as e:
        notify.log(f"Expiry list API failed ({e}) — falling back to last-Tuesday calc")

    # Fallback: last Tuesday of current month (BN monthly expires last Tuesday).
    # If that date is in the past, use next month's last Tuesday.
    import calendar as _cal
    def _last_tue(year, month):
        last_day = _cal.monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() != 1:   # 1 = Tuesday
            d -= timedelta(days=1)
        return d

    lt = _last_tue(today.year, today.month)
    if lt < today:
        nxt = (today.replace(day=1) + timedelta(days=32))
        lt  = _last_tue(nxt.year, nxt.month)
    notify.log(f"Using last-Tuesday-of-month expiry (fallback): {lt}")
    return lt


# ── Step 4: ATM option security_id ───────────────────────────────────────────

def _fetch_option_chain(expiry: date) -> tuple:
    """
    Single attempt to fetch option chain for a given expiry.
    Returns (security_id, atm_strike, spot, opt_type_used) or (None, None, None, None).
    Called by get_atm_security_id with retry + fallback-expiry logic.
    """
    payload = {
        "UnderlyingScrip": 25,
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry.strftime("%Y-%m-%d"),
    }
    resp = requests.post(
        "https://api.dhan.co/v2/optionchain",
        headers=HEADERS, json=payload, timeout=15
    )
    if resp.status_code != 200:
        notify.log(f"Option chain {expiry} → {resp.status_code}: {resp.text[:120]}")
        return None, None, None, None

    data  = resp.json()
    inner = data.get("data") or {}
    spot  = float(
        data.get("last_price") or data.get("lastTradedPrice") or
        (inner.get("last_price")       if isinstance(inner, dict) else 0) or
        (inner.get("underlyingPrice")  if isinstance(inner, dict) else 0) or 0
    )
    if not spot:
        notify.log(f"Option chain {expiry} → got 200 but spot price is 0")
        return None, None, None, None

    atm_strike = round(spot / 100) * 100
    return data, atm_strike, spot, inner


def _parse_security_id(data, inner, atm_strike, opt_type) -> tuple:
    """Extract security_id from option chain response. Returns (sid, strike) or (None, None).

    Dhan API returns strike keys as float strings ("55900.000000") and option type
    keys as lowercase ("ce"/"pe") — handle both formats defensively.
    """
    oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
    if oc and atm_strike:
        for delta in [0, 100, -100, 200, -200]:
            strike = atm_strike + delta
            # Try float string key first ("55900.000000"), then int string ("55900")
            key = (f"{float(strike):.6f}" if f"{float(strike):.6f}" in oc
                   else str(int(strike))   if str(int(strike))        in oc
                   else None)
            if key is None:
                continue
            # Try lowercase first ("ce"/"pe"), then uppercase ("CE"/"PE")
            sub = (oc[key].get(opt_type.lower()) or
                   oc[key].get(opt_type)          or {})
            sid = sub.get("security_id") or sub.get("securityId")
            if sid:
                return str(sid), float(key)

    options = data.get("options") or data.get("OptionChain") or []
    if isinstance(options, list) and atm_strike:
        for item in options:
            s = float(item.get("strikePrice") or item.get("strike_price") or 0)
            t = (item.get("optionType") or item.get("option_type") or "").upper()
            if abs(s - atm_strike) < 1 and t == opt_type:
                sid = item.get("security_id") or item.get("securityId")
                if sid:
                    return str(sid), atm_strike

    return None, None


def _get_bn_ltp() -> float:
    """
    Fetch BankNifty last traded price from Dhan market-feed LTP endpoint.
    Works even when the option chain API is down (e.g. off-hours / weekends).
    Returns float spot price, or None if unavailable.
    """
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            headers=HEADERS,
            json={"IDX_I": [25]},   # 25 = BankNifty (integer per Dhan v2 docs)
            timeout=10,
        )
        if resp.status_code == 200:
            d = resp.json()
            # Response key may be integer 25 or string "25" — handle both
            idx_data = (d.get("data") or {}).get("IDX_I") or d.get("IDX_I") or {}
            ltp = (
                (idx_data.get(25) or idx_data.get("25") or {}).get("last_price") or
                (idx_data.get(25) or idx_data.get("25") or {}).get("lastTradedPrice") or
                d.get("last_price") or 0
            )
            if ltp and float(ltp) > 10000:   # sanity: BN is always > 10k
                return float(ltp)
            notify.log(f"BN LTP endpoint returned unexpected payload: {str(d)[:100]}")
    except Exception as e:
        notify.log(f"BN LTP fetch failed: {e}")
    return None


def _find_affordable_strike_in_chain(inner, atm_strike, signal, capital,
                                     max_otm_strikes=10):
    """
    Find the optimal strike in the live option chain for the given capital.

    Walk logic (mirrors capital reality):
      Phase 1 — OTM scan:  try dist=0 (ATM), 1, 2, … until capital fits dual
                guard (5% risk + 85% margin). If a non-ATM strike is needed,
                return it immediately (no ITM walk — can't afford ATM).
      Phase 2 — ITM probe: ATM fits → probe 200pt then 100pt ITM (deepest
                first). ITM has higher delta → better payoff on trend days.
                Return deepest ITM the capital supports.
                Fall back to ATM if no ITM fits.

    dist_100pts convention (return value index [4]):
      negative = ITM  (-1 = 100pt ITM, -2 = 200pt ITM)
      0        = ATM
      positive = OTM  (+1 = 100pt OTM, +2 = 200pt OTM, …)
    """
    opt_type_lc = "ce" if signal == "CALL" else "pe"
    opt_type_uc = opt_type_lc.upper()
    # OTM step: CALL raises strike (56000→56100), PUT lowers (56000→55900)
    otm_step = 100 if signal == "CALL" else -100

    oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
    if not oc or not atm_strike:
        return None

    def _check_strike(strike):
        """Return (sid, strike, ltp, lots) or None."""
        key = None
        for k_try in [f"{float(strike):.6f}",
                      str(int(strike)),
                      f"{float(strike):.1f}"]:
            if k_try in oc:
                key = k_try
                break
        if key is None:
            return None
        sub = oc[key].get(opt_type_lc) or oc[key].get(opt_type_uc) or {}
        sid = sub.get("security_id") or sub.get("securityId")
        ltp = float(sub.get("last_price") or sub.get("ltp") or
                    sub.get("lastPrice") or 0)
        if not sid or ltp <= 0:
            return None
        loss_per_lot   = LOT_SIZE * ltp * SL_PCT
        margin_per_lot = LOT_SIZE * ltp
        if loss_per_lot <= 0 or margin_per_lot <= 0:
            return None
        lots_by_risk   = floor(capital * RISK_PCT / loss_per_lot)
        lots_by_margin = floor(capital * 0.85    / margin_per_lot)
        lots = min(MAX_LOTS, lots_by_risk, lots_by_margin)
        if lots < 1 and lots_by_margin >= 1:
            lots = 1   # minimum floor: always trade 1 lot if physically affordable
        return (str(sid), float(strike), ltp, int(lots)) if lots >= 1 else None

    # ── Phase 1: find cheapest acceptable strike (ATM → OTM) ─────────────────
    atm_result = None
    for dist in range(0, max_otm_strikes + 1):
        strike = atm_strike + (dist * otm_step)
        r = _check_strike(strike)
        if r:
            if dist == 0:
                atm_result = r   # ATM fits — attempt ITM in Phase 2
            else:
                return (r[0], r[1], r[2], r[3], dist)   # OTM fallback
            break  # stop OTM walk whether ATM fit or not

    if atm_result is None:
        return None   # nothing fits even at max OTM

    # ── Phase 2: ATM fits → probe ITM for better delta ───────────────────────
    # ITM step is the reverse of OTM step:
    #   CALL ITM → lower strike (55900, 55800); PUT ITM → higher strike
    itm_step = -otm_step
    for itm_dist in range(ITM_WALK_MAX, 0, -1):   # try 200pt, then 100pt
        strike = atm_strike + (itm_dist * itm_step)
        r = _check_strike(strike)
        if r:
            return (r[0], r[1], r[2], r[3], -itm_dist)   # negative = ITM

    # ATM is the best achievable
    return (atm_result[0], atm_result[1], atm_result[2], atm_result[3], 0)


def get_affordable_option(signal: str, expiry: date, capital: float):
    """
    Find the closest-to-ATM option strike that fits within the user's capital.

    Walks from ATM outward (up to 1000 points OTM) and returns the FIRST strike
    whose real live premium allows at least 1 lot under the 5% risk rule and
    85% margin cap. Closest-to-ATM wins so delta stays as high as possible.

    Returns (security_id, strike, premium, lots, spot, otm_distance)
      otm_distance: 0 = ATM, 1 = 100pt OTM, 2 = 200pt OTM, ...
      Returns (None, None, None, 0, spot, -1) if option chain unavailable.
      Returns (None, None, None, 0, spot, -2) if even deepest OTM exceeds budget.
    """
    opt_type = "CE" if signal == "CALL" else "PE"

    expiry_candidates = [expiry, expiry + timedelta(days=7)]
    last_spot = None

    for exp in expiry_candidates:
        for attempt in range(3):
            try:
                data, atm_strike, spot, inner = _fetch_option_chain(exp)

                if data is None:
                    if attempt < 2:
                        notify.log(f"Retry {attempt+1}/3 for expiry {exp} in 3s...")
                        time.sleep(3)
                    continue

                last_spot = spot

                # Walk ATM → OTM in the live option chain
                affordable = _find_affordable_strike_in_chain(
                    inner, atm_strike, signal, capital, max_otm_strikes=10
                )

                if affordable:
                    sid, strike, ltp, lots, dist = affordable
                    if exp != expiry:
                        notify.log(f"Using fallback expiry {exp} (primary {expiry} failed)")
                    if dist == 0:
                        notify.log(f"ATM {opt_type} {int(strike)} @ ₹{ltp:.1f} → {lots} lot(s)")
                    elif dist < 0:
                        notify.log(f"Capital flush — selected {abs(dist)*100}pt ITM "
                                   f"{opt_type} {int(strike)} @ ₹{ltp:.1f} → {lots} lot(s) "
                                   f"(higher delta)")
                    else:
                        notify.log(f"ATM too expensive — selected {dist*100}pt OTM "
                                   f"{opt_type} {int(strike)} @ ₹{ltp:.1f} → {lots} lot(s)")
                    return sid, strike, ltp, lots, spot, dist

                # Option chain OK but no strike within OTM window fits capital
                notify.log(f"No affordable strike within 1000pt OTM for {exp}")
                return None, None, None, 0, spot, -2

            except Exception as e:
                notify.log(f"Option chain exception (expiry {exp}, attempt {attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(3)

        notify.log(f"All 3 attempts failed for expiry {exp} — trying next expiry")

    # ── Option chain entirely unavailable ────────────────────────────────────
    # For DRY RUN: degrade to approximated ATM so user can still see what a
    # live day would look like. For LIVE: refuse to trade blind.
    spot = last_spot or _get_bn_ltp()
    if not spot:
        try:
            bn_df = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
            spot = float(bn_df.iloc[-1]["close"])
            notify.log(f"Using stale CSV spot ₹{spot:,.0f}")
        except Exception:
            return None, None, None, 0, None, -1

    if DRY_RUN:
        dte = max(0.25, (expiry - date.today()).days + 1)
        approx_premium = spot * PREMIUM_K * sqrt(dte)
        atm_strike = round(spot / 100) * 100

        loss_per_lot   = LOT_SIZE * approx_premium * SL_PCT
        margin_per_lot = LOT_SIZE * approx_premium
        lots_by_risk   = floor(capital * RISK_PCT / loss_per_lot) if loss_per_lot > 0 else 0
        lots_by_margin = floor(capital * 0.85    / margin_per_lot) if margin_per_lot > 0 else 0
        lots = min(MAX_LOTS, lots_by_risk, lots_by_margin)
        if lots < 1 and lots_by_margin >= 1:
            lots = 1   # minimum floor: always trade 1 lot if physically affordable

        notify.log(f"DRY_RUN fallback: ATM {atm_strike} {opt_type} ≈ ₹{approx_premium:.0f} "
                   f"→ {lots} lots (approx)")
        return ("DRY_RUN_FALLBACK", float(atm_strike), approx_premium, lots, spot, 0)

    return None, None, None, 0, spot, -1


def get_atm_security_id(signal: str, expiry: date, spot_fallback: float = None):
    """
    Returns (security_id, atm_strike, spot).

    Retry logic:
      - For each expiry candidate (primary + next fallback expiry):
          - Try up to 3 times (1 initial + 2 retries) with 3s delay between attempts
          - If security_id found → return it
          - If all retries fail → move to next expiry candidate
      - If all expiries exhausted → use CSV spot (DRY RUN) or return None (LIVE)
    """
    opt_type = "CE" if signal == "CALL" else "PE"

    # Build expiry candidates: primary expiry + next week as backup
    expiry_candidates = [expiry, expiry + timedelta(days=7)]

    for exp in expiry_candidates:
        for attempt in range(3):   # 3 attempts per expiry
            try:
                result = _fetch_option_chain(exp)
                data, atm_strike, spot, inner = result

                if data is None:
                    if attempt < 2:
                        notify.log(f"Retry {attempt+1}/3 for expiry {exp} in 3s...")
                        time.sleep(3)
                    continue

                sid, strike = _parse_security_id(data, inner, atm_strike, opt_type)
                if sid:
                    if exp != expiry:
                        notify.log(f"Using fallback expiry {exp} (primary {expiry} failed)")
                    return sid, strike, spot

                notify.log(f"Got chain for {exp} but no {opt_type} security_id at ATM {atm_strike}")
                if attempt < 2:
                    time.sleep(3)

            except Exception as e:
                notify.log(f"Option chain exception (expiry {exp}, attempt {attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(3)

        notify.log(f"All 3 attempts failed for expiry {exp} — trying next expiry")

    # ── Option chain exhausted: try live LTP before falling back to CSV ────────
    spot = _get_bn_ltp()
    if spot:
        atm_strike = round(spot / 100) * 100
        notify.log(f"Option chain unavailable — using live BN LTP ₹{spot:,.0f} (ATM {int(atm_strike)})")
        if DRY_RUN:
            return "DRY_RUN_LIVE_LTP", atm_strike, spot
        return None, None, None  # live mode: can't place without real security_id

    # ── Final fallback: CSV close (stale — warn loudly) ───────────────────────
    try:
        bn_df      = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
        csv_close  = spot_fallback or float(bn_df.iloc[-1]["close"])
        csv_date   = bn_df.iloc[-1]["date"]
        atm_strike = round(csv_close / 100) * 100
        notify.log(
            f"⚠️  Option chain AND LTP unavailable — using stale CSV close "
            f"₹{csv_close:,.0f} ({csv_date.date() if hasattr(csv_date,'date') else csv_date}). "
            f"Strike/premium estimates will be wrong if spot has moved significantly."
        )
        if DRY_RUN:
            return "DRY_RUN_STALE", atm_strike, csv_close
    except Exception:
        pass

    return None, None, None


# ── Step 5: Place Super Order ─────────────────────────────────────────────────

def place_super_order(security_id: str, signal: str, lots: int,
                      spot: float, premium: float, rr: float) -> dict:
    qty      = lots * LOT_SIZE
    sl_price = round(premium * (1 - SL_PCT),      1)
    tp_price = round(premium * (1 + SL_PCT * rr), 1)

    if DRY_RUN:
        return {"status": "DRY_RUN", "sl": sl_price, "tp": tp_price}

    # Primary: Super Order (entry + SL + TP in one call)
    # Note: SuperOrderRequest spec does NOT include "validity" — omit it
    payload = {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"at_{date.today().strftime('%Y%m%d')}",
        "transactionType": "BUY",
        "exchangeSegment": "NSE_FNO",
        "productType":     "MARGIN",     # NRML — can carry forward if SL/TP not hit
        "orderType":       "MARKET",
        "securityId":      security_id,
        "quantity":        qty,
        "price":           0,
        "targetPrice":     tp_price,
        "stopLossPrice":   sl_price,
        # trailingJump = ₹5 step: SL ratchets up every ₹5 the option gains.
        # Tight trail intentionally catches reversals early — e.g. option hit ₹1047,
        # trail exited at ₹906, saved ₹107 vs static SL at ₹799 (which fell to ₹682).
        "trailingJump":    min(5, max(1, round(premium * SL_PCT, 1))),
    }
    market_closed = False
    try:
        resp   = requests.post("https://api.dhan.co/v2/super/orders",
                               headers=HEADERS, json=payload, timeout=15)
        result = resp.json()
        if resp.status_code == 200 and result.get("status") not in ("failure", "error"):
            return result
        # DH-906 = Market is Closed → fall through to AMO path below
        err_code = result.get("errorCode", "")
        # DH-906 = "Incorrect order request" — covers market-closed scenarios
        # (exchange rejects orders before open; empirically confirmed via live logs)
        market_closed = (err_code == "DH-906")
        notify.log(f"Super Order failed ({resp.status_code}): {resp.text[:150]}")
    except Exception as e:
        notify.log(f"Super Order exception: {e}")

    # ── AMO fallback: market is closed, place After-Market-Order ─────────────
    # AMO uses LIMIT order at last-traded-price so it queues for next open.
    # User can cancel from Dhan app before market opens if needed.
    if market_closed:
        notify.log("Market closed — retrying as AMO LIMIT order (cancel from Dhan app if needed)")
        amo_payload = {
            "dhanClientId":      CLIENT_ID,
            "correlationId":     f"amo_{date.today().strftime('%Y%m%d')}",
            "transactionType":   "BUY",
            "exchangeSegment":   "NSE_FNO",
            "productType":       "MARGIN",
            "orderType":         "LIMIT",
            "validity":          "DAY",
            "securityId":        security_id,
            "quantity":          qty,
            "price":             premium,   # LTP — fair price, queues for open
            "triggerPrice":      0,
            "disclosedQuantity": 0,
            "afterMarketOrder":  True,
            "amoTime":           "OPEN",   # required by Dhan v2 alongside afterMarketOrder
        }
        try:
            amo_resp = requests.post("https://api.dhan.co/v2/orders",
                                     headers=HEADERS, json=amo_payload, timeout=15)
            return {"buy_order": amo_resp.json(), "mode": "AMO",
                    "sl": sl_price, "tp": tp_price}
        except Exception as e:
            notify.log(f"AMO order exception: {e}")

    # ── Fallback: manual MARKET BUY + SL-M SELL ──────────────────────────────
    # Build a clean OrderRequest payload — no super-order-specific fields
    opt_sym_short = f"BN {security_id} x{qty}"   # compact label for emergency msgs
    buy_payload = {
        "dhanClientId":      CLIENT_ID,
        "correlationId":     f"at_buy_{date.today().strftime('%Y%m%d')}",
        "transactionType":   "BUY",
        "exchangeSegment":   "NSE_FNO",
        "productType":       "MARGIN",
        "orderType":         "MARKET",
        "validity":          "DAY",
        "securityId":        security_id,
        "quantity":          qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }

    # BUY — wrapped so a timeout/exception doesn't leave us in unknown state
    buy_result = {}
    buy_oid    = None
    try:
        buy_resp   = requests.post("https://api.dhan.co/v2/orders",
                                   headers=HEADERS, json=buy_payload, timeout=15)
        buy_result = buy_resp.json()
        buy_oid    = buy_result.get("orderId") or buy_result.get("order_id")
        if not buy_oid:
            notify.log(f"BUY response has no orderId — may have failed: {buy_result}")
    except Exception as e:
        notify.send(
            f"❌ <b>BUY order failed — no position opened</b>\n\n"
            f"Exception: {e}\n"
            f"Symbol: {opt_sym_short}\n"
            f"No action needed — check Dhan app to confirm."
        )
        return {"mode": "FAILED", "buy_order": {}, "sl": sl_price, "tp": tp_price}

    time.sleep(2)

    # SL — if this fails AFTER a BUY succeeded, position is unhedged — emergency alert
    sl_payload = {**buy_payload,
                  "transactionType": "SELL",
                  "orderType":       "STOP_LOSS_MARKET",
                  "triggerPrice":    sl_price,
                  "correlationId":   f"at_sl_{date.today().strftime('%Y%m%d')}"}
    sl_result = {}
    try:
        sl_resp   = requests.post("https://api.dhan.co/v2/orders",
                                  headers=HEADERS, json=sl_payload, timeout=15)
        sl_result = sl_resp.json()
        sl_oid    = sl_result.get("orderId") or sl_result.get("order_id")
        if not sl_oid:
            # SL may have silently failed — send urgent alert with manual action
            notify.send(
                f"⚠️ <b>SL order — no confirmation</b>\n\n"
                f"BUY orderId: {buy_oid or 'unknown'}\n"
                f"SL response: {str(sl_result)[:200]}\n\n"
                f"<b>Verify SL manually on Dhan app:</b>\n"
                f"Symbol: {opt_sym_short}\n"
                f"SL trigger: ₹{sl_price:.0f}  |  TP target: ₹{tp_price:.0f}"
            )
    except Exception as e:
        # CRITICAL: BUY succeeded but SL failed — position is unhedged
        notify.send(
            f"🚨 <b>CRITICAL — SL PLACEMENT FAILED</b>\n\n"
            f"BUY was placed (orderId: {buy_oid or 'unknown'}) but SL threw an exception.\n"
            f"Exception: {e}\n\n"
            f"<b>IMMEDIATE MANUAL ACTION REQUIRED:</b>\n"
            f"Open Dhan app → Orders → set SL on {opt_sym_short}\n"
            f"SL trigger: ₹{sl_price:.0f}\n"
            f"OR exit the position immediately."
        )
        return {"mode": "FALLBACK_NO_SL", "buy_order": buy_result,
                "sl": sl_price, "tp": tp_price}

    return {"buy_order": buy_result, "sl_order": sl_result, "mode": "FALLBACK",
            "sl": sl_price, "tp": tp_price}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode_label = "DRY RUN" if DRY_RUN else "LIVE"
    notify.log(f"BankNifty Auto Trader starting [{mode_label}]")

    # 0. Credentials + lot-size sanity check
    check_credentials()
    _check_lot_size()

    # 0b. Verify yesterday's exit ran — catch open overnight positions before trading
    if not DRY_RUN:
        _check_exit_marker()

    # 1. Refresh data + signal
    refresh_data_and_signal()

    # 1b. Holiday check — if BankNifty has no data for today, NSE is closed
    # (handles Diwali, Republic Day, Holi, etc. without a manual holiday list)
    if not _is_trading_day() and not DRY_RUN:
        today_label = date.today().strftime("%d %b %Y")
        notify.send(
            f"📆  <b>Market Holiday</b>\n\n"
            f"{today_label} — NSE is closed today.\n"
            f"No trade placed. See you tomorrow."
        )
        notify.log(f"Market holiday detected ({today_label}) — no BN data in CSV. Exiting.")
        return

    # 2. Read signal
    sig, sig_note = get_todays_signal()
    if sig is None:
        return

    # Guard: signal CSV row may have unexpected/missing fields
    try:
        signal     = str(sig.get("signal", "")).upper()
        # rule_score (from ml_engine compute_features) is authoritative;
        # fallback to score (from signals.csv) which may be stale if evolver hasn't run yet
        score      = int(sig.get("rule_score") or sig.get("score", 0))
        ml_conf    = float(sig.get("ml_conf", 0.5))
        ml_trained = bool(sig.get("ml_trained", False))
    except (ValueError, TypeError) as e:
        die(f"Signal CSV row has unexpected format: {e}\nRow: {sig}")
    today_wd     = date.today().strftime("%A")
    today_label  = date.today().strftime("%d %b %Y")

    # No-trade path — one clean message
    if signal not in ("CALL", "PUT"):
        if sig.get("event_day"):
            reason = "RBI MPC / Budget — forced no-trade day"
        elif score == 0:
            reason = "Score = 0  (indicators tied — no directional edge)"
        else:
            reason = f"Score {score:+d}  (below threshold)"
        notify.send(
            f"⏸  <b>No Trade Today</b>\n"
            f"─────────────────────\n"
            f"{today_wd}  ·  {today_label}\n\n"
            f"{reason}"
        )
        return

    score_max = 4
    # ML confidence is now the ensemble agreement score (avg prob of agreeing models).
    # No trade skipping / direction override — ensemble majority is the signal.
    # ML_CONF_THRESHOLD kept for future use; logging only for now.
    if ML_CONF_THRESHOLD > 0 and ml_trained and ml_conf < ML_CONF_THRESHOLD:
        notify.log(
            f"ML ensemble conf {ml_conf:.0%} < {ML_CONF_THRESHOLD:.0%} "
            f"(low agreement day) — proceeding with {signal} anyway"
        )

    # 3. Capital
    capital = get_capital()
    if capital <= 0 and not DRY_RUN:
        notify.send(
            f"⚠️  <b>No funds available</b>\n\n"
            f"Dhan account shows ₹0 available balance.\n"
            f"Add funds at dhan.co → Funds → Add Money.\n"
            f"No order placed today."
        )
        return
    if capital <= 0 and DRY_RUN:
        notify.log("DRY RUN: capital ₹0 → using ₹1,00,000 for simulation")
        capital = 100_000.0

    # 4. Expiry + find affordable strike (walks ATM → OTM if needed)
    expiry = get_expiry()
    security_id, atm_strike, premium, lots, spot, otm_distance = \
        get_affordable_option(signal, expiry, capital)

    if security_id is None and otm_distance == -2:
        # Option chain was available but even deepest OTM doesn't fit capital
        notify.send(
            f"⏸  <b>No Trade — Even Deep OTM Too Expensive</b>\n"
            f"─────────────────────\n"
            f"{today_wd}  ·  {today_label}\n\n"
            f"Signal:  <b>{signal}</b>  (score {score:+d}/{score_max})\n"
            f"Walked ATM → 1000pt OTM in the live option chain.\n"
            f"No strike within that window fits your ₹{capital:,.0f} budget\n"
            f"under the 5% risk rule + 85% margin cap.\n\n"
            f"<i>This is extreme — premium must be very high today.\n"
            f"Skipping to preserve capital.</i>"
        )
        return

    if not security_id:
        die(
            f"Option chain unavailable — cannot find tradable option for "
            f"BANKNIFTY {expiry} {'CE' if signal == 'CALL' else 'PE'}.\n"
            f"Check Dhan API status."
        )

    # Guard: premium must be valid — crashes SL/TP calc if None or zero
    if not premium or premium <= 0:
        die(
            f"Invalid premium ({premium}) from option chain — cannot calculate SL/TP.\n"
            f"Strike: {atm_strike}  |  Expiry: {expiry}  |  Check option chain API."
        )

    # Guard: spot must be valid — used in risk calculations and Telegram message
    if not spot or spot <= 0:
        if DRY_RUN:
            notify.log("Spot price unavailable — using ₹50,000 placeholder for DRY RUN display")
            spot = 50_000.0
        else:
            die("Spot price unavailable. Cannot confirm trade safety. Check Dhan LTP endpoint.")

    # Guard: lots must be at least 1 — belt-and-suspenders beyond strike selection
    if not lots or lots < 1:
        die(f"Lot sizing returned {lots} — insufficient capital or premium too high for 1 lot.")

    # 4b. Adaptive opening-wait — if BN spot gapped significantly from yesterday's
    #     close, opening IV is likely elevated. Wait proportionally, then re-fetch
    #     so SL/TP are anchored to the actual fill price, not the inflated open.
    sig_spot = float(sig.get("bn_close") or 0)
    if not DRY_RUN and sig_spot > 0 and spot > 0:
        spot_gap_pct = abs(spot - sig_spot) / sig_spot
        if spot_gap_pct >= ENTRY_SPOT_GAP_THRESHOLD:
            wait_mins = min(ENTRY_WAIT_MAX_MINS, round(spot_gap_pct * 1000))
            direction_word = "up" if spot > sig_spot else "down"
            notify.log(
                f"Adaptive wait: BN spot gapped {direction_word} {spot_gap_pct*100:.1f}% "
                f"(₹{sig_spot:.0f} → ₹{spot:.0f}). Option at ₹{premium:.0f}. "
                f"Waiting {wait_mins} min for opening IV to settle..."
            )
            notify.send(
                f"⏳ <b>Adaptive Entry</b>\n\n"
                f"BN gapped {direction_word} {spot_gap_pct*100:.1f}% at open  "
                f"(₹{sig_spot:.0f} → ₹{spot:.0f})\n"
                f"Option currently ₹{premium:.0f}  ·  Signal: <b>{signal}</b> {score:+d}\n\n"
                f"Waiting <b>{wait_mins} min</b> for opening IV to settle.\n"
                f"Will enter by ~{(datetime.now() + timedelta(minutes=wait_mins)).strftime('%H:%M')} IST regardless."
            )
            time.sleep(wait_mins * 60)
            # Re-fetch live prices — SL/TP will auto-recalculate from the new premium below
            notify.log("Adaptive wait complete — re-fetching live option price...")
            sid2, strike2, prem2, lots2, spot2, otm2 = get_affordable_option(signal, expiry, capital)
            if sid2 and prem2 and prem2 > 0:
                improvement = round(premium - prem2, 2)
                notify.log(
                    f"After wait: ₹{premium:.0f} → ₹{prem2:.0f}  "
                    f"({'better by ₹' + str(improvement) if improvement > 0 else 'no improvement ₹' + str(-improvement)})"
                )
                security_id, atm_strike, premium, lots, spot, otm_distance = \
                    sid2, strike2, prem2, lots2, spot2, otm2
            else:
                notify.log("Re-fetch returned no data — using original price from 9:15 open.")
    elif DRY_RUN and sig_spot > 0 and spot > 0:
        spot_gap_pct = abs(spot - sig_spot) / sig_spot
        if spot_gap_pct >= ENTRY_SPOT_GAP_THRESHOLD:
            wait_mins = min(ENTRY_WAIT_MAX_MINS, round(spot_gap_pct * 1000))
            direction_word = "up" if spot > sig_spot else "down"
            notify.log(
                f"[DRY RUN] Adaptive wait WOULD trigger: {spot_gap_pct*100:.1f}% gap "
                f"{direction_word}. Would wait {wait_mins} min before entering."
            )

    # 5. Sizing — DTE + risk/reward numbers come from the real premium returned above
    dte = max(0.25, (expiry - date.today()).days + 1)
    rr  = RR

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    margin_1lot   = LOT_SIZE * premium

    risk_amt   = lots * max_loss_1lot
    target_amt = lots * LOT_SIZE * premium * SL_PCT * rr - 40   # rough charge estimate
    sl_price   = premium * (1 - SL_PCT)
    tp_price   = premium * (1 + SL_PCT * rr)

    opt_type  = "CE" if signal == "CALL" else "PE"
    opt_emoji = "📈" if signal == "CALL" else "📉"
    opt_sym   = f"BANKNIFTY {expiry.strftime('%d%b%Y').upper()} {int(atm_strike)} {opt_type}"
    # Strike label — shows context for why this strike was chosen
    if otm_distance and otm_distance >= 1:
        otm_label = f"  ({otm_distance*100}pt OTM — ATM too pricey for budget)"
    elif otm_distance and otm_distance <= -1:
        otm_label = f"  ({abs(otm_distance)*100}pt ITM — capital flush, higher delta)"
    else:
        otm_label = "  (ATM)"
    cap_label = f"₹{capital:,.0f}" + ("  [DRY RUN]" if DRY_RUN else "")

    # Determine score description
    if abs(score) == score_max:
        score_desc = "  ● max signal ●"
    elif abs(score) >= 3:
        score_desc = "  ● strong ●"
    elif abs(score) == 2:
        score_desc = ""
    else:
        score_desc = "  ● weak ●"

    sig_line = f"\n<i>↳ {sig_note}</i>" if sig_note else ""

    # Stale-data warning (DRY RUN only — API unavailable, approximated fallback)
    stale_line = ""
    if security_id == "DRY_RUN_FALLBACK":
        stale_line = (
            "\n⚠️  <i>Option chain offline — spot/strike/premium are approximated. "
            "Actual Monday trade will use live option-chain prices.</i>"
        )

    # 6. Send ONE trade-details message to Telegram
    notify.send(
        f"{opt_emoji}  <b>BUY {signal}</b>  ·  {today_wd}, {today_label}{sig_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Score      {score:+d} / {score_max}{score_desc}\n"
        f"ML conf    {ml_conf:.0%}{'  ✓' if ml_conf >= ML_CONF_THRESHOLD else '  ⚠ low' if (ml_trained and ml_conf < ML_CONF_THRESHOLD) else ''}\n"
        f"Capital    {cap_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Option     <code>{opt_sym}</code>{otm_label}\n"
        f"Qty        {lots} lot{'s' if lots > 1 else ''}  ·  {lots*LOT_SIZE} shares\n"
        f"Spot       ₹{spot:,.0f}   Premium  ~₹{premium:.0f}\n"
        f"DTE        {dte:.1f} days  ·  Expiry {expiry.strftime('%d %b')}   RR  {rr}×\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Stop loss  ₹{sl_price:.0f}  (−{SL_PCT*100:.0f}%)\n"
        f"Target     ₹{tp_price:.0f}  (+{SL_PCT*rr*100:.0f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Risk  ₹{risk_amt:,.0f}   Reward  ₹{target_amt:,.0f}"
        f"{stale_line}"
    )

    # 6b. Double-position guard — abort if a BN position already exists
    #     (protects against cron double-fire or manual re-run on same day)
    if not DRY_RUN and _check_no_existing_position():
        notify.send(
            f"⚠️  <b>Duplicate Trade Blocked</b>\n\n"
            f"An open BankNifty position already exists on your account.\n"
            f"Skipping new order to avoid double exposure.\n\n"
            f"<i>Close the existing position on Dhan app if this is unexpected.</i>"
        )
        return

    # 7. Place order
    notify.log("Placing order...")
    result = place_super_order(security_id, signal, lots, spot, premium, rr)

    # 8. Send ONE result message
    if DRY_RUN:
        if security_id == "DRY_RUN_FALLBACK":
            footer = (
                "⚠️  <i>Option chain offline — strike/premium approximated.\n"
                "Real Monday trade will use live option-chain prices.</i>"
            )
        else:
            footer = "<i>Add funds to your Dhan account to go live.</i>"
        notify.send(
            f"✅  <b>Dry Run Complete</b>\n\n"
            f"Would have bought:\n"
            f"<code>{opt_sym}</code>\n"
            f"{lots} lot{'s' if lots > 1 else ''}  ·  "
            f"SL ₹{sl_price:.0f}  ·  TP ₹{tp_price:.0f}\n\n"
            f"{footer}"
        )
        return

    # Extract mode and orderId before writing today_trade.json
    mode = result.get("mode", "SUPER_ORDER")
    oid  = (result.get("orderId") or result.get("order_id") or
            (result.get("buy_order") or {}).get("orderId"))

    # Write oracle intent for EOD trade journal (live trades only)
    iv_val = float(sig.get("iv_at_entry", sig.get("iv", 0.0)) or 0.0)
    _write_today_trade(signal, atm_strike, lots, dte, spot,
                       oracle_premium=premium,
                       sl_price=sl_price, tp_price=tp_price,
                       security_id=security_id, score=score, iv=iv_val,
                       expiry=expiry, ml_conf=ml_conf,
                       order_id=oid, order_mode=mode)

    # Emergency modes — critical alerts already sent inside place_super_order
    if mode == "FAILED":
        notify.log("Order placement failed entirely — no position opened. See earlier error.")
        return
    if mode == "FALLBACK_NO_SL":
        notify.log("FALLBACK_NO_SL — BUY placed but SL failed. Emergency alert sent. Manual action needed.")
        return

    corr_id = f"at_{date.today().strftime('%Y%m%d')}"
    if oid:
        if mode == "AMO":
            notify.send(
                f"🕐  <b>AMO Order Queued!</b>  [After-Market]\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Order ID   <code>{oid}</code>\n"
                f"Option     <code>{opt_sym}</code>\n"
                f"Qty        {lots*LOT_SIZE}  ·  Limit ₹{premium:.0f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🚨 <b>NO automated SL or TP on AMO orders.</b>\n"
                f"After fill, set manually on Dhan app:\n"
                f"  SL ₹{sl_price:.0f}  (−{SL_PCT*100:.0f}%)\n"
                f"  TP ₹{tp_price:.0f}  (+{SL_PCT*RR*100:.0f}%)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Will execute at next market open.\n"
                f"Cancel from Dhan app before open if you change your mind.</i>"
            )
        elif mode == "FALLBACK":
            # Manual BUY + SL-M: SL is automated, TP is NOT (no TP order placed)
            notify.send(
                f"✅  <b>Order Placed!</b>  [FALLBACK — BUY+SL-M]\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Order ID   <code>{oid}</code>\n"
                f"Ref ID     <code>{corr_id}</code>\n"
                f"Option     <code>{opt_sym}</code>\n"
                f"Qty        {lots*LOT_SIZE}  ·  Market entry\n"
                f"SL ₹{sl_price:.0f}  (automated SL-M order)\n"
                f"TP ₹{tp_price:.0f}  ⚠️ manual — no TP order placed\n"
                f"Risk  ₹{risk_amt:,.0f}   Reward  ₹{target_amt:,.0f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>Exit manually at ₹{tp_price:.0f} or let SL-M protect you.</i>"
            )
        else:
            notify.send(
                f"✅  <b>Order Placed!</b>  [{mode}]\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Order ID   <code>{oid}</code>\n"
                f"Ref ID     <code>{corr_id}</code>\n"
                f"Option     <code>{opt_sym}</code>\n"
                f"Qty        {lots*LOT_SIZE}  ·  "
                f"SL ₹{sl_price:.0f}  ·  TP ₹{tp_price:.0f}\n"
                f"Risk  ₹{risk_amt:,.0f}   Reward  ₹{target_amt:,.0f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>NRML order — carries forward if SL/TP not hit by close.</i>"
            )
    else:
        notify.send(
            f"⚠️  <b>Order response — no order ID found</b>\n\n"
            f"Response: {str(result)[:300]}\n\n"
            f"Check Dhan app → Orders to confirm."
        )

    # 9. Order status verification — wait 30s then confirm not REJECTED
    #    Skip for AMO (status stays PENDING until market opens) and error modes
    if oid and mode not in ("AMO", "FAILED", "FALLBACK_NO_SL"):
        notify.log("Waiting 30s to verify order status is not REJECTED...")
        time.sleep(30)
        _verify_order_status(oid, opt_sym)


if __name__ == "__main__":
    main()
