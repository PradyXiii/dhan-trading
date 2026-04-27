#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
auto_trader.py — Nifty50 Iron Condor Full Automation
====================================================
Runs every trading day at 9:30 AM IST via cron.
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

Cron (9:30 AM IST = 4:00 AM UTC):
  0 4 * * 1-5 cd ~/dhan-trading && python3 auto_trader.py >> logs/auto_trader.log 2>&1

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
from datetime import date, datetime, timedelta, timezone
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
    # Remove the lock file on clean exit so the mtime staleness check only
    # fires when a previous run actually crashed (leaving the file behind).
    try:
        os.remove(_LOCK_FILE)
    except OSError:
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
    yesterday = _ist_today() - timedelta(days=1)
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
            if IRON_CONDOR_MODE:
                open_chk = [
                    p for p in positions
                    if int(p.get("netQty", 0)) != 0
                    and p.get("exchangeSegment", "") == "NSE_FNO"
                    and "NIFTY" in str(p.get("tradingSymbol", "")).upper()
                    and "BANKNIFTY" not in str(p.get("tradingSymbol", "")).upper()
                ]
            else:
                open_chk = [
                    p for p in positions
                    if int(p.get("netQty", 0)) != 0
                    and p.get("exchangeSegment", "") == "NSE_FNO"
                    and "BANKNIFTY" in str(
                        p.get("tradingSymbol", p.get("securityId", ""))
                    ).upper()
                ]
            if open_chk:
                syms = ", ".join(
                    p.get("tradingSymbol", str(p.get("securityId", "?")))
                    for p in open_chk
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
                    f"Exit marker missing for {yesterday} but no open NF/BN position "
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
    Return True if an open FNO position for the active instrument already exists.
    IRON_CONDOR_MODE: checks for NIFTY (not BANKNIFTY) positions.
    Fails OPEN (returns False) on connectivity issues.
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
    if IRON_CONDOR_MODE:
        # NF: symbol contains "NIFTY" but NOT "BANKNIFTY"
        open_pos = [
            p for p in positions
            if int(p.get("netQty", 0)) != 0
            and p.get("exchangeSegment", "") == "NSE_FNO"
            and "NIFTY" in str(p.get("tradingSymbol", "")).upper()
            and "BANKNIFTY" not in str(p.get("tradingSymbol", "")).upper()
        ]
    else:
        open_pos = [
            p for p in positions
            if int(p.get("netQty", 0)) != 0
            and p.get("exchangeSegment", "") == "NSE_FNO"
            and "BANKNIFTY" in str(
                p.get("tradingSymbol", p.get("securityId", ""))
            ).upper()
        ]
    return len(open_pos) > 0


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

# ── PAPER MODE — set True to disable LIVE order placement ────────────────────
# When True: full signal flow runs, "would have placed" trade is logged to
# data/paper_trades.csv, Telegram message is prefixed with [PAPER]. No real
# order goes to Dhan. Flip back to False when strategy is fixed and ready.
# Reason for activation (Apr 2026): real-options backtest + 4-trade live
# sample showed naked-options buying is structurally losing (theta decay +
# IV crush). Paper-trading new spread strategy before risking ₹51K capital.
PAPER_MODE = False

# IST timezone helper — VM runs UTC. date.today() at 00:00-05:30 IST returns
# yesterday UTC, mismatching IST-based trading schedule. Use this for today_trade.json
# date field + any stale-check roundtrip.
_IST_TZ = timezone(timedelta(hours=5, minutes=30))
def _ist_today():
    return datetime.now(_IST_TZ).date()


# ── Iron Condor Mode — Nifty50 (confirmed strategy, April 2026) ──────────────
# NF IC: SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE
# 5yr backtest: WR 84.6%, ₹1.17Cr total, ₹25L/yr avg, max DD -0.8%
# Trades EVERY CALL/PUT signal day (~235 trades/year — always weekly).
# Do NOT add VIX filter — no-filter gives maximum P&L per optimize_params.py.
#
# IRON_CONDOR_MODE = True  → NF IC (4-leg, both CALL+PUT signal days)
# IRON_CONDOR_MODE = False → 2-leg credit spread (BNF legacy path, keep for fallback)
IRON_CONDOR_MODE   = True
UNDERLYING_SCRIP   = 13     # 13 = Nifty50 (weekly), 25 = BankNifty (monthly, fallback)
UNDERLYING_SEG     = "IDX_I"

# ── Credit Spread / IC params ─────────────────────────────────────────────────
CREDIT_SPREAD_MODE = True
SPREAD_WIDTH       = 150    # NF: 50pt strike spacing × 3 = 150pts  (BNF was 300)
CREDIT_SL_FRAC     = 0.5    # NF IC: SL: spread expands 50% above credit received
CREDIT_TP_FRAC     = 0.65   # TP when spread cost falls to net_credit × 0.35 (backtest-validated)
IC_MARGIN_PER_LOT        = 100_000  # Fallback only — live code queries Dhan /margincalculator/multi for actual margin
BULL_PUT_MARGIN_PER_LOT  =  55_000  # Fallback for Bull Put 2-leg (actual Dhan SPAN ≈ ₹50-55K/lot)

# ── Day-of-week filter ────────────────────────────────────────────────────────
# NF expiry = Tuesday (from Sep 1 2025, NSE circular).
# Post-Sep-2025 backtest (7 months, real 1-min data):
#   Tue DTE=0: 100% WR, ₹4,011/lot gross → IC ✅
#   Mon DTE=1:  97% WR, ₹1,123/lot gross → IC ✅
#   Fri DTE=4: IC 100% WR +₹20.8K (Tue-expiry regime — Bear Call dumped, negative every day)
# DOW backtest (Sep 2025+, Tue-expiry regime): IC profitable ALL 5 days.
#   Mon DTE=1: 88.5% WR +₹32.7K  Tue DTE=0: 89.5% WR +₹38.9K
#   Wed DTE=6: 95.8% WR +₹14.0K  Thu DTE=5: 100% WR +₹15.2K  Fri DTE=4: 100% WR +₹20.8K
# "Wed is bad" rule was old Thu-expiry data — stale. Bear Call dumped (negative every day).
# Trade every weekday. No skip days.
IC_SKIP_DAYS       = set()      # no skip — IC profitable all 5 days in Tue-expiry regime

STRADDLE_MARGIN_PER_LOT = 230_000   # upgrade threshold: actual Dhan SPAN ≈₹2,26,492 + ₹3,508 buffer
MAX_LOTS_STRADDLE       = 5         # straddle uses ~2.5× IC margin — lower cap

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR      = "data"
# NF lot size: 65 from Jan 6 2026, 75 before. Dynamic at startup.
LOT_SIZE      = 65 if date.today() >= date(2026, 1, 6) else 75
SL_PCT        = 0.15   # 15% stop-loss (legacy naked path only)
RISK_PCT      = 0.05
MAX_LOTS      = 10     # IC: half the naked MAX_LOTS — margin tied on both sides
PREMIUM_K     = 0.004
ITM_WALK_MAX  = 2    # Walk up to 200pt ITM when capital is flush (higher delta)

RR = 2.5   # reward:risk ratio — SL=15%, TP=+37.5% of premium (RR=2.5x)
           # Grid result: 2.5x beats 2.0x on all metrics (+₹24L P&L, DD -8.8% vs -12.9%)

# ── ML confidence gate ────────────────────────────────────────────────────────
# Skip trade when ML ensemble confidence is below this threshold.
# Analysis of 252-day holdout: all-days accuracy=52%, conf≥10%=55.8%.
# Set to 0.0 to disable.
ML_CONF_THRESHOLD = 0.55

# ── VIX regime filter ─────────────────────────────────────────────────────────
# MIN: dynamically updated by analyze_confidence.py --write-threshold (called nightly
# by autoloop_nf.py). As model accuracy improves, the threshold relaxes so we
# trade on more days. Fallback: 13.0 (VIX<13 historically 46.7% accuracy).
# MAX: ceiling — panic-VIX days (real 1-min option backtest Oct-24 → Apr-26:
# VIX∈[12,20] band kept 63.2% of trades, saved ₹44K vs unfiltered baseline).
def _load_vix_threshold() -> tuple[float, float]:
    try:
        with open(f"{DATA_DIR}/vix_threshold.json") as _f:
            d = json.load(_f)
            return (float(d.get("vix_min_trade", 13.0)),
                    float(d.get("vix_max_trade", 20.0)))
    except Exception:
        return (13.0, 20.0)

VIX_MIN_TRADE, VIX_MAX_TRADE = _load_vix_threshold()

# ── Adaptive opening-wait parameters ─────────────────────────────────────────
# Root cause of bad 9:30 fills: large BN spot gap → inflated IV at open.
# If |live_spot - yesterday_close| > ENTRY_SPOT_GAP_THRESHOLD, wait proportionally:
#   0.5% gap → 5 min,  0.8% → 8 min,  1.0% → 10 min,  ≥1.2% → 12 min (cap)
# After wait: re-fetch option chain so SL/TP auto-reset to actual fill price.
ENTRY_SPOT_GAP_THRESHOLD = 0.005   # 0.5% BN spot gap (≈280 pts at 56k) triggers wait
ENTRY_WAIT_MAX_MINS      = 12      # never wait beyond 9:30 + 12 = 9:42 AM


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_vix_level() -> float:
    """Read latest India VIX close from CSV — yesterday's close, available at 9:30 AM."""
    try:
        vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv", parse_dates=["date"])
        return float(vix["close"].iloc[-1])
    except Exception:
        return 15.0   # default to neutral regime if file missing


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
    Verify LOT_SIZE matches expected NF lot size for today.
    NF: 75 before Jan 6 2026, 65 from Jan 6 2026.
    Does NOT block trading — alerts operator to fix the constant.
    """
    today = _ist_today()
    if IRON_CONDOR_MODE:
        # Nifty50 lot size timeline
        expected = 65 if today >= date(2026, 1, 6) else 75
    else:
        # Legacy naked-option timeline (pre-IC rebuild)
        if today < date(2024, 11, 20):
            expected = 15
        elif today < date(2025, 6, 26):
            expected = 30
        elif today < date(2026, 1, 27):
            expected = 35
        else:
            expected = 30

    if LOT_SIZE != expected:
        msg = (
            f"🚨 LOT SIZE MISMATCH — LOT_SIZE={LOT_SIZE} "
            f"but expected {expected} for {today} ({'NF' if IRON_CONDOR_MODE else 'BNF'}). "
            f"Update LOT_SIZE in auto_trader.py BEFORE next trade."
        )
        notify.send(msg)
        notify.log(msg)
    else:
        instr = "NF" if IRON_CONDOR_MODE else "BNF"
        notify.log(f"Lot-size check OK: LOT_SIZE={LOT_SIZE} matches expected {expected} ({instr})")


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
        die("signal_engine.py timed out (60s). Check if nifty50.csv is valid.")
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


def _log_paper_trade(security_id, signal, lots, qty, premium, sl_price, tp_price, spot):
    """
    Append one paper-trade row to data/paper_trades.csv. Used when PAPER_MODE=True.
    Schema mirrors live_trades.csv so we can backtest the exact trades that would
    have been placed live (no Dhan order goes out).
    """
    import csv as _csv
    path  = f"{DATA_DIR}/paper_trades.csv"
    cols  = [
        "date", "signal", "security_id", "lots", "qty", "lot_size",
        "spot", "entry_premium", "sl_price", "tp_price",
        "max_loss_inr", "max_profit_inr",
    ]
    row = {
        "date":           _ist_today().isoformat(),
        "signal":         signal,
        "security_id":    str(security_id),
        "lots":           int(lots),
        "qty":            int(qty),
        "lot_size":       int(LOT_SIZE),
        "spot":           round(float(spot), 2),
        "entry_premium":  round(float(premium), 2),
        "sl_price":       round(float(sl_price), 2),
        "tp_price":       round(float(tp_price), 2),
        "max_loss_inr":   round((premium - sl_price) * qty, 2),
        "max_profit_inr": round((tp_price - premium) * qty, 2),
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            if new_file:
                w.writeheader()
            w.writerow(row)
        notify.log(f"PAPER trade logged → {path}")
    except Exception as e:
        notify.log(f"Could not write paper_trades.csv: {e}")


def _log_paper_spread_trade(short_sid, long_sid, signal, lots, qty,
                            net_credit, short_strike, long_strike,
                            short_ltp, long_ltp, spot):
    """Log a paper credit spread trade to data/paper_trades.csv."""
    import csv as _csv
    path = f"{DATA_DIR}/paper_trades.csv"
    cols = [
        "date", "signal", "strategy",
        "short_sid", "long_sid", "short_strike", "long_strike",
        "lots", "qty", "lot_size", "spot",
        "short_ltp", "long_ltp", "net_credit",
        "max_loss_inr", "max_profit_inr",
    ]
    max_loss_per_share = SPREAD_WIDTH - net_credit
    row = {
        "date":           _ist_today().isoformat(),
        "signal":         signal,
        "strategy":       "bull_put_credit",   # CALL days use IC (not Bear Call — permanently removed Apr 2026)
        "short_sid":      str(short_sid),
        "long_sid":       str(long_sid),
        "short_strike":   int(short_strike),
        "long_strike":    int(long_strike),
        "lots":           int(lots),
        "qty":            int(qty),
        "lot_size":       int(LOT_SIZE),
        "spot":           round(float(spot), 2),
        "short_ltp":      round(float(short_ltp), 2),
        "long_ltp":       round(float(long_ltp), 2),
        "net_credit":     round(float(net_credit), 2),
        "max_loss_inr":   round(max_loss_per_share * qty, 2),
        "max_profit_inr": round(net_credit * qty, 2),
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)
        notify.log(f"PAPER spread trade logged → {path}")
    except Exception as e:
        notify.log(f"Could not write paper_trades.csv: {e}")


def get_spread_legs(signal: str, expiry: date, capital: float):
    """
    Fetch both legs of the credit spread from the live option chain.

    CALL → Bear Call Spread: SELL ATM CE + BUY (ATM+SPREAD_WIDTH) CE
    PUT  → Bull Put  Spread: SELL ATM PE + BUY (ATM-SPREAD_WIDTH) PE

    Returns (short_sid, long_sid, short_strike, long_strike,
             short_ltp, long_ltp, net_credit, lots, spot)
    or (None, None, None, None, 0, 0, 0, 0, None) on failure.
    """
    opt_type_lc  = "ce" if signal == "CALL" else "pe"
    long_offset  = +SPREAD_WIDTH if signal == "CALL" else -SPREAD_WIDTH

    expiry_candidates = [expiry, expiry + timedelta(days=7)]

    for exp in expiry_candidates:
        for attempt in range(3):
            try:
                data, atm_strike, spot, inner = _fetch_option_chain(exp)
                if data is None:
                    if attempt < 2:
                        notify.log(f"Spread: retry {attempt+1}/3 for {exp} in 3s...")
                        time.sleep(3)
                    continue

                oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
                if not oc:
                    notify.log(f"Spread: empty OC for {exp}")
                    break

                def _get_leg(strike):
                    for k in [f"{float(strike):.6f}", str(int(strike)), f"{float(strike):.1f}"]:
                        if k in oc:
                            sub = (oc[k].get(opt_type_lc) or
                                   oc[k].get(opt_type_lc.upper()) or {})
                            sid = sub.get("security_id") or sub.get("securityId")
                            ltp = float(sub.get("last_price") or sub.get("ltp") or 0)
                            if sid and ltp > 0:
                                return str(sid), ltp
                    return None, 0.0

                short_strike = atm_strike
                long_strike  = atm_strike + long_offset
                short_sid, short_ltp = _get_leg(short_strike)
                long_sid,  long_ltp  = _get_leg(long_strike)

                if not short_sid or not long_sid:
                    notify.log(f"Spread: leg missing — short={short_sid}/{short_ltp:.0f} "
                               f"long={long_sid}/{long_ltp:.0f} (exp={exp})")
                    break  # chain OK but legs missing — skip to next expiry

                net_credit = short_ltp - long_ltp
                if net_credit <= 0:
                    notify.log(f"Spread: net credit ≤ 0 ({net_credit:.1f}) — legs inverted?")
                    break

                # Max loss per lot = (spread_width - net_credit) × lot_size
                max_loss_per_lot = (SPREAD_WIDTH - net_credit) * LOT_SIZE
                sl_risk_per_lot  = net_credit * CREDIT_SL_FRAC * LOT_SIZE
                risk_per_lot     = min(max_loss_per_lot, sl_risk_per_lot)

                lots_by_risk   = floor(capital * RISK_PCT  / risk_per_lot)   if risk_per_lot   > 0 else 0
                lots_by_margin = floor(capital * 0.85      / max_loss_per_lot) if max_loss_per_lot > 0 else 0
                lots = min(MAX_LOTS, lots_by_risk, lots_by_margin)
                if lots < 1 and lots_by_margin >= 1:
                    lots = 1

                if lots < 1:
                    notify.log(f"Spread: insufficient capital for 1 lot "
                               f"(max_loss/lot ₹{max_loss_per_lot:.0f})")
                    return None, None, None, None, 0, 0, 0, 0, spot

                if exp != expiry:
                    notify.log(f"Spread: using fallback expiry {exp} (primary {expiry} failed)")
                opt_uc = opt_type_lc.upper()
                notify.log(
                    f"Credit spread: SELL {opt_uc} {int(short_strike)} ₹{short_ltp:.0f} / "
                    f"BUY {opt_uc} {int(long_strike)} ₹{long_ltp:.0f} "
                    f"= net credit ₹{net_credit:.0f} → {lots} lot(s)"
                )
                return (short_sid, long_sid, short_strike, long_strike,
                        short_ltp, long_ltp, net_credit, lots, spot)

            except Exception as e:
                notify.log(f"Spread legs exception (exp={exp}, attempt={attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(3)

        notify.log(f"Spread: all attempts failed for {exp} — trying next expiry")

    return None, None, None, None, 0, 0, 0, 0, None


def place_credit_spread(short_sid: str, long_sid: str, signal: str, lots: int,
                        net_credit: float, short_strike: float, long_strike: float,
                        short_ltp: float, long_ltp: float, spot: float) -> dict:
    """
    Place credit spread: BUY long (hedge) leg first, then SELL short (credit) leg.
    MANDATORY ORDER: BUY hedge leg FIRST — Dhan hedge-margin rule requires the
    long leg on books before the short gets reduced margin treatment.
    """
    qty      = lots * LOT_SIZE
    opt_type = "CE" if signal == "CALL" else "PE"

    if DRY_RUN:
        return {"status": "DRY_RUN", "net_credit": net_credit}

    if PAPER_MODE:
        _log_paper_spread_trade(short_sid, long_sid, signal, lots, qty,
                                net_credit, short_strike, long_strike,
                                short_ltp, long_ltp, spot)
        return {
            "status":     "PAPER",
            "mode":       "PAPER",
            "orderId":    f"paper_spread_{date.today():%Y%m%d}",
            "net_credit": net_credit,
        }

    # ── Step 1: BUY long (hedge) leg ─────────────────────────────────────────
    buy_payload = {
        "dhanClientId":      CLIENT_ID,
        "correlationId":     f"spread_buy_{date.today().strftime('%Y%m%d')}",
        "transactionType":   "BUY",
        "exchangeSegment":   "NSE_FNO",
        "productType":       "MARGIN",
        "orderType":         "MARKET",
        "validity":          "DAY",
        "securityId":        long_sid,
        "quantity":          qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }
    try:
        buy_resp   = requests.post("https://api.dhan.co/v2/orders",
                                   headers=HEADERS, json=buy_payload, timeout=15)
        buy_result = buy_resp.json()
        buy_oid    = buy_result.get("orderId") or buy_result.get("order_id")
        if not buy_oid:
            notify.send(
                f"❌ <b>Spread BUY (hedge leg) failed — no orderId</b>\n\n"
                f"{opt_type} {int(long_strike)} qty={qty}\n"
                f"Response: {str(buy_result)[:200]}\n\nNo position opened."
            )
            return {"mode": "FAILED"}
        notify.log(f"Spread BUY leg: {opt_type} {int(long_strike)} qty={qty} orderId={buy_oid}")
    except Exception as e:
        notify.send(f"❌ <b>Spread BUY leg exception</b>\n\nException: {e}\nNo position opened.")
        return {"mode": "FAILED"}

    time.sleep(2)   # let BUY settle on exchange before SELL goes in

    # ── Step 2: SELL short (credit) leg ──────────────────────────────────────
    sell_payload = {**buy_payload,
                    "correlationId":   f"spread_sell_{date.today().strftime('%Y%m%d')}",
                    "transactionType": "SELL",
                    "securityId":      short_sid}
    try:
        sell_resp   = requests.post("https://api.dhan.co/v2/orders",
                                    headers=HEADERS, json=sell_payload, timeout=15)
        sell_result = sell_resp.json()
        sell_oid    = sell_result.get("orderId") or sell_result.get("order_id")
        if not sell_oid:
            notify.send(
                f"🚨 <b>CRITICAL — Spread SELL (credit leg) no orderId</b>\n\n"
                f"BUY leg orderId: {buy_oid}\n"
                f"SELL response: {str(sell_result)[:200]}\n\n"
                f"<b>NAKED LONG {opt_type} {int(long_strike)} x{qty}!</b>\n"
                f"Open Dhan app → SELL {opt_type} {int(short_strike)} qty={qty} IMMEDIATELY."
            )
            return {"mode": "PARTIAL_SPREAD", "buy_oid": buy_oid}
        notify.log(f"Spread SELL leg: {opt_type} {int(short_strike)} qty={qty} orderId={sell_oid}")
    except Exception as e:
        notify.send(
            f"🚨 <b>CRITICAL — Spread SELL leg exception</b>\n\n"
            f"BUY leg orderId: {buy_oid}\n"
            f"Exception: {e}\n\n"
            f"<b>NAKED LONG {opt_type} {int(long_strike)} x{qty}!</b>\n"
            f"IMMEDIATELY SELL {opt_type} {int(short_strike)} qty={qty} on Dhan app."
        )
        return {"mode": "PARTIAL_SPREAD", "buy_oid": buy_oid}

    return {
        "mode":       "CREDIT_SPREAD",
        "buy_oid":    buy_oid,
        "sell_oid":   sell_oid,
        "net_credit": net_credit,
    }


def _write_today_spread_trade(signal, short_sid, long_sid, short_strike, long_strike,
                              short_ltp, long_ltp, net_credit, lots, dte, spot,
                              score, expiry, ml_conf, order_mode,
                              buy_oid=None, sell_oid=None):
    """
    Write spread-trade intent to data/today_trade.json.
    Read by spread_monitor.py (intraday SL/TP), exit_positions.py (EOD),
    trade_journal.py (EOD journal).
    """
    strategy = "bull_put_credit"   # CALL days use IC path — this func only called for 2-leg spreads
    payload = {
        "date":           _ist_today().isoformat(),
        "strategy":       strategy,
        "signal":         signal,
        "short_sid":      str(short_sid),
        "long_sid":       str(long_sid),
        "short_strike":   float(short_strike),
        "long_strike":    float(long_strike),
        "short_entry":    float(short_ltp),
        "long_entry":     float(long_ltp),
        "net_credit":     float(net_credit),
        "spread_width":   float(abs(long_strike - short_strike)),
        "sl_frac":        CREDIT_SL_FRAC,
        "tp_frac":        CREDIT_TP_FRAC,
        "lots":           int(lots),
        "lot_size":       int(LOT_SIZE),
        "dte":            float(dte),
        "expiry":         expiry.isoformat() if expiry else None,
        "spot_at_signal": float(spot),
        "signal_score":   int(score),
        "ml_conf":        round(float(ml_conf), 4),
        "order_mode":     str(order_mode) if order_mode else None,
        "buy_oid":        str(buy_oid) if buy_oid else None,
        "sell_oid":       str(sell_oid) if sell_oid else None,
        "exit_done":      False,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(f"{DATA_DIR}/today_trade.json", "w") as f:
            json.dump(payload, f, indent=2)
        notify.log("Spread intent written → data/today_trade.json")
    except Exception as e:
        notify.log(f"Could not write today_trade.json: {e}")


def _write_today_trade(signal, strike, lots, dte, spot, oracle_premium,
                       sl_price, tp_price, security_id, score, iv=0.0,
                       expiry=None, ml_conf=0.0, order_id=None, order_mode=None):
    """
    Write oracle intent to data/today_trade.json so trade_journal.py can
    compare it against actual fills at EOD.  Overwrites any previous file.
    Data stays on VM only — gitignored.
    """
    payload = {
        "date":           _ist_today().isoformat(),
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
    today = _ist_today()
    if today.weekday() >= 5:            # Saturday / Sunday
        return False
    return today not in NSE_HOLIDAYS_2026


def get_todays_signal() -> tuple:
    """
    Returns (signal_dict, sig_note_str).
    Reads signals_ml.csv (ML direction oracle) with fallback to signals.csv.

    sig_note is empty if today's date matches, or a label like "08 Apr ML" if fallback.
    """
    today = pd.Timestamp(_ist_today())

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
    Find the nearest valid expiry using the /optionchain/expirylist endpoint.
    NF (IRON_CONDOR_MODE): weekly Tuesday (from Sep 1 2025 per NSE circular). BNF: monthly last Tuesday.
    Falls back to a calculated expiry if the API is unavailable.

    Uses IST date explicitly — VM runs UTC, so date.today() can return yesterday
    relative to IST late-night (00:00–05:30 IST = prev-day UTC). Would pick an
    already-expired contract.
    """
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=HEADERS,
            json={"UnderlyingScrip": UNDERLYING_SCRIP, "UnderlyingSeg": UNDERLYING_SEG},
            timeout=10,
        )
        if resp.status_code == 200:
            expiries = resp.json().get("data", [])
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
        notify.log(f"Expiry list API returned {resp.status_code} — falling back to calc")
    except Exception as e:
        notify.log(f"Expiry list API failed ({e}) — falling back to calc")

    if IRON_CONDOR_MODE:
        # NF: weekly Tuesday expiry (from Sep 1 2025 per NSE circular).
        d = today
        while d.weekday() != 1:   # 1 = Tuesday
            d += timedelta(days=1)
        notify.log(f"Using next-Tuesday expiry (NF fallback): {d}")
        return d
    else:
        # BNF: last Tuesday of current month (monthly expiry since Nov 2024).
        import calendar as _cal
        def _last_tue(year, month):
            last_day = _cal.monthrange(year, month)[1]
            d = date(year, month, last_day)
            while d.weekday() != 1:
                d -= timedelta(days=1)
            return d
        lt = _last_tue(today.year, today.month)
        if lt < today:
            nxt = (today.replace(day=1) + timedelta(days=32))
            lt  = _last_tue(nxt.year, nxt.month)
        notify.log(f"Using last-Tuesday-of-month expiry (BNF fallback): {lt}")
        return lt


# ── Step 4: ATM option security_id ───────────────────────────────────────────

def _fetch_option_chain(expiry: date) -> tuple:
    """
    Single attempt to fetch option chain for a given expiry.
    Returns (security_id, atm_strike, spot, opt_type_used) or (None, None, None, None).
    Called by get_atm_security_id with retry + fallback-expiry logic.
    """
    payload = {
        "UnderlyingScrip": UNDERLYING_SCRIP,
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

    # NF strikes are multiples of 50, BNF are multiples of 100
    strike_unit = 50 if IRON_CONDOR_MODE else 100
    atm_strike  = round(spot / strike_unit) * strike_unit
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


def _get_nf_ltp() -> float:
    """
    Fetch Nifty50 last traded price from Dhan market-feed LTP endpoint.
    Works even when the option chain API is down (e.g. off-hours / weekends).
    Returns float spot price, or None if unavailable.
    """
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            headers=HEADERS,
            json={"IDX_I": [13]},   # 13 = Nifty50 (integer per Dhan v2 docs)
            timeout=10,
        )
        if resp.status_code == 200:
            d = resp.json()
            # Response key may be integer 13 or string "13" — handle both
            idx_data = (d.get("data") or {}).get("IDX_I") or d.get("IDX_I") or {}
            ltp = (
                (idx_data.get(13) or idx_data.get("13") or {}).get("last_price") or
                (idx_data.get(13) or idx_data.get("13") or {}).get("lastTradedPrice") or
                d.get("last_price") or 0
            )
            if ltp and float(ltp) > 10000:   # sanity: NF spot always > 10k
                return float(ltp)
            notify.log(f"NF LTP endpoint returned unexpected payload: {str(d)[:100]}")
    except Exception as e:
        notify.log(f"NF LTP fetch failed: {e}")
    return None


def compute_chain_signals(expiry: date, spot: float) -> dict:
    """
    Compute max-pain strike and net gamma exposure (GEX) from the live option chain.

    Max pain = strike where total ITM option value (buyers' gain) is minimised.
    Near expiry, price gravitates toward max pain as market makers protect profits.

    GEX = Σ(call_OI × gamma) − Σ(put_OI × gamma).
    Positive GEX → MMs net long gamma → they dampen moves (ranging day).
    Negative GEX → MMs net short gamma → they amplify moves (trending day).

    Returns dict or {} on failure (always safe to ignore).
    """
    import math

    try:
        data, _, chain_spot, inner = _fetch_option_chain(expiry)
        if data is None or not inner:
            return {}
        oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
        if not oc:
            return {}

        strikes, call_oi, put_oi, call_iv, put_iv = [], {}, {}, {}, {}
        for key, val in oc.items():
            try:
                k   = float(key)
                ce  = val.get("ce") or val.get("CE") or {}
                pe  = val.get("pe") or val.get("PE") or {}
                call_oi[k] = float(ce.get("oi") or 0)
                put_oi[k]  = float(pe.get("oi") or 0)
                call_iv[k] = float(ce.get("implied_volatility") or 0)
                put_iv[k]  = float(pe.get("implied_volatility") or 0)
                strikes.append(k)
            except (ValueError, TypeError):
                continue

        if not strikes:
            return {}
        strikes.sort()

        # ── Max pain ─────────────────────────────────────────────────────────
        min_itm = float("inf")
        max_pain_strike = spot
        for test_p in strikes:
            call_itm = sum((test_p - k) * call_oi[k] for k in strikes if k < test_p)
            put_itm  = sum((k - test_p) * put_oi[k]  for k in strikes if k > test_p)
            total    = call_itm + put_itm
            if total < min_itm:
                min_itm = total
                max_pain_strike = test_p

        max_pain_dist = (spot - max_pain_strike) / spot * 100  # + = spot above pain

        # ── Net Gamma Exposure (GEX) ──────────────────────────────────────────
        T      = max(0.5, (expiry - date.today()).days + 1) / 365.0
        r      = 0.07
        sqrt_T = math.sqrt(T)

        def _gamma(S, K, sigma):
            if sigma <= 0:
                return 0.0
            d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
            return math.exp(-0.5 * d1**2) / (math.sqrt(2 * math.pi) * S * sigma * sqrt_T)

        S   = chain_spot or spot
        gex = 0.0
        for k in strikes:
            c_iv = call_iv.get(k, 0) / 100
            p_iv = put_iv.get(k, 0) / 100
            if c_iv > 0:
                gex += call_oi[k] * _gamma(S, k, c_iv)
            if p_iv > 0:
                gex -= put_oi[k]  * _gamma(S, k, p_iv)

        # ── ATM straddle premium ──────────────────────────────────────────────
        atm     = round(spot / 100) * 100
        atm_key = f"{float(atm):.6f}"
        atm_d   = oc.get(atm_key, {})
        atm_c   = float((atm_d.get("ce") or {}).get("last_price") or 0)
        atm_p   = float((atm_d.get("pe") or {}).get("last_price") or 0)

        return {
            "max_pain_strike": max_pain_strike,
            "max_pain_dist":   round(max_pain_dist, 2),
            "gex_positive":    gex > 0,   # True = ranging, False = trending
            "straddle":        round(atm_c + atm_p, 1),
            "n_strikes":       len(strikes),
        }
    except Exception as e:
        notify.log(f"compute_chain_signals error: {e}")
        return {}


def _append_chain_signals(chain_sig: dict, spot: float) -> None:
    """Append today's chain signals to options_atm_daily.csv for future ML training."""
    try:
        import csv
        path     = f"{DATA_DIR}/options_atm_daily.csv"
        today_s  = _ist_today().isoformat()
        fieldnames = ["date", "call_premium", "put_premium",
                      "max_pain_strike", "max_pain_dist", "gex_positive", "straddle"]

        # Read existing rows, drop today's if re-running
        rows = []
        if os.path.exists(path):
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("date") != today_s:
                        rows.append(row)

        # Append today's row (preserve call_premium/put_premium from existing data)
        rows.append({
            "date":             today_s,
            "call_premium":     "",          # filled by data_fetcher.py rollingoption
            "put_premium":      "",
            "max_pain_strike":  chain_sig.get("max_pain_strike", ""),
            "max_pain_dist":    chain_sig.get("max_pain_dist", ""),
            "gex_positive":     chain_sig.get("gex_positive", ""),
            "straddle":         chain_sig.get("straddle", ""),
        })

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        notify.log(f"_append_chain_signals write error: {e}")


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
    spot = last_spot or _get_nf_ltp()
    if not spot:
        try:
            nf_df = pd.read_csv(f"{DATA_DIR}/nifty50.csv", parse_dates=["date"])
            spot = float(nf_df.iloc[-1]["close"])
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
    spot = _get_nf_ltp()
    if spot:
        atm_strike = round(spot / 100) * 100
        notify.log(f"Option chain unavailable — using live NF LTP ₹{spot:,.0f} (ATM {int(atm_strike)})")
        if DRY_RUN:
            return "DRY_RUN_LIVE_LTP", atm_strike, spot
        return None, None, None  # live mode: can't place without real security_id

    # ── Final fallback: CSV close (stale — warn loudly) ───────────────────────
    try:
        nf_df      = pd.read_csv(f"{DATA_DIR}/nifty50.csv", parse_dates=["date"])
        csv_close  = spot_fallback or float(nf_df.iloc[-1]["close"])
        csv_date   = nf_df.iloc[-1]["date"]
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

    if PAPER_MODE:
        _log_paper_trade(security_id, signal, lots, qty, premium, sl_price, tp_price, spot)
        return {
            "status":  "PAPER",
            "mode":    "PAPER",
            "orderId": f"paper_{date.today():%Y%m%d}",
            "sl":      sl_price,
            "tp":      tp_price,
        }

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


# ── Iron Condor helpers ───────────────────────────────────────────────────────

def _fetch_ic_margin_per_lot(ce_short_sid, ce_short_ltp,
                              ce_long_sid,  ce_long_ltp,
                              pe_short_sid, pe_short_ltp,
                              pe_long_sid,  pe_long_ltp) -> float:
    """Call Dhan /margincalculator/multi for 1 NF IC lot (all 4 legs).
    Returns actual SPAN+Exposure margin. Falls back to IC_MARGIN_PER_LOT on error."""
    try:
        payload = {
            "dhanClientId":   CLIENT_ID,
            "includePosition": True,
            "includeOrders":   True,
            "scripList": [
                {"exchangeSegment": "NSE_FNO", "transactionType": "SELL",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(ce_short_sid), "price": float(ce_short_ltp), "triggerPrice": 0},
                {"exchangeSegment": "NSE_FNO", "transactionType": "BUY",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(ce_long_sid),  "price": float(ce_long_ltp),  "triggerPrice": 0},
                {"exchangeSegment": "NSE_FNO", "transactionType": "SELL",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(pe_short_sid), "price": float(pe_short_ltp), "triggerPrice": 0},
                {"exchangeSegment": "NSE_FNO", "transactionType": "BUY",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(pe_long_sid),  "price": float(pe_long_ltp),  "triggerPrice": 0},
            ],
        }
        resp = requests.post("https://api.dhan.co/v2/margincalculator/multi",
                             headers=HEADERS, json=payload, timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            margin = float(d.get("total_margin") or d.get("totalMargin") or 0)
            # 10K–200K: plausible hedged IC margin range. Above 200K = API summed
            # unhedged individual SELL margins (not applying hedge offset) → use fallback.
            if 10_000 < margin < 200_000:
                notify.log(f"IC margin/lot from Dhan: ₹{margin:,.0f}")
                return margin
            notify.log(f"IC margin API returned {margin:,.0f} (out of range) — using fallback ₹{IC_MARGIN_PER_LOT:,.0f}")
        else:
            notify.log(f"Margin API {resp.status_code}: {resp.text[:120]} — using fallback")
    except Exception as e:
        notify.log(f"Margin API error: {e} — using fallback ₹{IC_MARGIN_PER_LOT:,.0f}")
    return float(IC_MARGIN_PER_LOT)


def _fetch_spread_margin_per_lot(short_sid, short_ltp, long_sid, long_ltp) -> float:
    """Call Dhan /margincalculator/multi for 1 NF spread lot (SELL short + BUY long).
    Returns actual SPAN+Exposure margin. Falls back to BULL_PUT_MARGIN_PER_LOT on error."""
    try:
        payload = {
            "dhanClientId":    CLIENT_ID,
            "includePosition": True,
            "includeOrders":   True,
            "scripList": [
                {"exchangeSegment": "NSE_FNO", "transactionType": "SELL",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(short_sid), "price": float(short_ltp), "triggerPrice": 0},
                {"exchangeSegment": "NSE_FNO", "transactionType": "BUY",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(long_sid), "price": float(long_ltp), "triggerPrice": 0},
            ],
        }
        resp = requests.post("https://api.dhan.co/v2/margincalculator/multi",
                             headers=HEADERS, json=payload, timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            margin = float(d.get("total_margin") or d.get("totalMargin") or 0)
            # 5K–100K: plausible hedged Bull Put margin. Above 100K = unhedged SELL margin
            # (API not applying hedge offset from the BUY leg) → use fallback.
            if 5_000 < margin < 100_000:
                notify.log(f"Bull Put margin/lot from Dhan: ₹{margin:,.0f}")
                return margin
            notify.log(f"Bull Put margin API returned {margin:,.0f} (out of range) — using fallback ₹{BULL_PUT_MARGIN_PER_LOT:,.0f}")
        else:
            notify.log(f"Margin API {resp.status_code}: {resp.text[:120]} — using fallback")
    except Exception as e:
        notify.log(f"Margin API error: {e} — using fallback ₹{BULL_PUT_MARGIN_PER_LOT:,.0f}")
    return float(BULL_PUT_MARGIN_PER_LOT)


def _setup_pnl_exit(net_credit: float, lots: int):
    """
    Safety net: POST /v2/pnlExit — fires only if spread_monitor.py misses the SL
    (VM crash, LTP API down). lossValue mirrors our SL level so Dhan auto-exits
    before losses exceed the SL threshold. Resets at end of trading session.
    """
    if DRY_RUN or PAPER_MODE:
        return
    qty          = lots * LOT_SIZE
    loss_value   = net_credit * CREDIT_SL_FRAC * qty
    profit_value = net_credit * qty * 5  # 5× theoretical max — won't trigger normally
    payload = {
        "profitValue":      f"{profit_value:.2f}",
        "lossValue":        f"{loss_value:.2f}",
        "productType":      ["INTRADAY", "DELIVERY"],
        "enableKillSwitch": False,
    }
    try:
        resp = requests.post("https://api.dhan.co/v2/pnlExit",
                             headers=HEADERS, json=payload, timeout=10)
        data = resp.json()
        if data.get("pnlExitStatus") == "ACTIVE":
            notify.log(
                f"P&L exit active: loss ₹{loss_value:,.0f}  profit ₹{profit_value:,.0f}")
        else:
            notify.log(f"P&L exit setup non-ACTIVE: {data}")
    except Exception as e:
        notify.log(f"P&L exit setup failed (non-critical): {e}")


def _get_oc_leg(oc: dict, strike: float, opt_type_lc: str):
    """Return (security_id, ltp) for a strike from the OC dict, or (None, 0.0)."""
    for k in [f"{float(strike):.6f}", str(int(strike)), f"{float(strike):.1f}"]:
        if k in oc:
            sub = oc[k].get(opt_type_lc) or oc[k].get(opt_type_lc.upper()) or {}
            sid = sub.get("security_id") or sub.get("securityId")
            ltp = float(sub.get("last_price") or sub.get("ltp") or 0)
            if sid and ltp > 0:
                return str(sid), ltp
    return None, 0.0


def get_ic_legs(expiry: date, capital: float) -> dict | None:
    """
    Fetch all 4 Nifty IC legs from the live option chain.
    IC: SELL ATM CE + BUY (ATM+SPREAD_WIDTH) CE
        SELL ATM PE + BUY (ATM-SPREAD_WIDTH) PE
    Returns dict with leg data + lot sizing, or None on failure.
    """
    expiry_candidates = [expiry, expiry + timedelta(days=7)]

    for exp in expiry_candidates:
        for attempt in range(3):
            try:
                data, atm_strike, spot, inner = _fetch_option_chain(exp)
                if data is None:
                    if attempt < 2:
                        notify.log(f"IC: retry {attempt+1}/3 for {exp} in 3s...")
                        time.sleep(3)
                    continue

                oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
                if not oc:
                    notify.log(f"IC: empty OC for {exp}")
                    break

                ce_short_strike = atm_strike
                ce_long_strike  = atm_strike + SPREAD_WIDTH
                pe_short_strike = atm_strike
                pe_long_strike  = atm_strike - SPREAD_WIDTH

                ce_short_sid, ce_short_ltp = _get_oc_leg(oc, ce_short_strike, "ce")
                ce_long_sid,  ce_long_ltp  = _get_oc_leg(oc, ce_long_strike,  "ce")
                pe_short_sid, pe_short_ltp = _get_oc_leg(oc, pe_short_strike, "pe")
                pe_long_sid,  pe_long_ltp  = _get_oc_leg(oc, pe_long_strike,  "pe")

                missing = [name for name, sid in [
                    (f"CE ATM({atm_strike})",     ce_short_sid),
                    (f"CE +{SPREAD_WIDTH}",        ce_long_sid),
                    (f"PE ATM({atm_strike})",     pe_short_sid),
                    (f"PE -{SPREAD_WIDTH}",        pe_long_sid),
                ] if not sid]
                if missing:
                    notify.log(f"IC: missing legs {missing} (exp={exp})")
                    break

                ce_credit  = ce_short_ltp - ce_long_ltp
                pe_credit  = pe_short_ltp - pe_long_ltp
                net_credit = ce_credit + pe_credit

                if net_credit <= 0:
                    notify.log(f"IC: net credit ≤ 0 ({net_credit:.1f}) — legs inverted?")
                    break

                max_loss_per_lot = (SPREAD_WIDTH - net_credit) * LOT_SIZE

                # Lot sizing: query Dhan margin calculator for actual SPAN+Exposure
                # required for 1 IC lot (all 4 legs). Falls back to IC_MARGIN_PER_LOT.
                margin_1lot = _fetch_ic_margin_per_lot(
                    ce_short_sid, ce_short_ltp, ce_long_sid, ce_long_ltp,
                    pe_short_sid, pe_short_ltp, pe_long_sid, pe_long_ltp,
                )
                lots = min(MAX_LOTS, int(capital // margin_1lot))
                if lots < 1:
                    notify.log(f"IC: insufficient capital for 1 lot "
                               f"(need ₹{margin_1lot:,.0f}, have ₹{capital:,.0f})")
                    return None

                notify.log(
                    f"IC legs: ATM={atm_strike}  "
                    f"CE {ce_short_strike}=₹{ce_short_ltp:.0f} / {ce_long_strike}=₹{ce_long_ltp:.0f} "
                    f"(credit ₹{ce_credit:.0f})  "
                    f"PE {pe_short_strike}=₹{pe_short_ltp:.0f} / {pe_long_strike}=₹{pe_long_ltp:.0f} "
                    f"(credit ₹{pe_credit:.0f})  net ₹{net_credit:.0f}  → {lots} lot(s)"
                )
                return {
                    "ce_short_sid":    ce_short_sid,  "ce_short_strike": ce_short_strike,
                    "ce_short_ltp":    ce_short_ltp,
                    "ce_long_sid":     ce_long_sid,   "ce_long_strike":  ce_long_strike,
                    "ce_long_ltp":     ce_long_ltp,
                    "pe_short_sid":    pe_short_sid,  "pe_short_strike": pe_short_strike,
                    "pe_short_ltp":    pe_short_ltp,
                    "pe_long_sid":     pe_long_sid,   "pe_long_strike":  pe_long_strike,
                    "pe_long_ltp":     pe_long_ltp,
                    "ce_credit":       ce_credit,
                    "pe_credit":       pe_credit,
                    "net_credit":      net_credit,
                    "lots":            lots,
                    "margin_per_lot":  round(margin_1lot, 0),
                    "spot":            spot,
                    "atm_strike":      atm_strike,
                    "expiry":          exp,
                }
            except Exception as e:
                notify.log(f"IC legs exception (exp={exp}, attempt={attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(3)

        notify.log(f"IC: all attempts failed for {exp} — trying next expiry")

    return None


def _log_paper_ic_trade(ic: dict, qty: int):
    """Log a paper Nifty Iron Condor trade to data/paper_trades.csv."""
    import csv as _csv
    path = f"{DATA_DIR}/paper_trades.csv"
    cols = [
        "date", "signal", "strategy",
        "ce_short_sid", "ce_long_sid", "pe_short_sid", "pe_long_sid",
        "ce_short_strike", "ce_long_strike", "pe_short_strike", "pe_long_strike",
        "lots", "qty", "lot_size", "spot", "atm_strike",
        "ce_short_ltp", "ce_long_ltp", "pe_short_ltp", "pe_long_ltp",
        "ce_credit", "pe_credit", "net_credit",
        "max_loss_inr", "max_profit_inr",
    ]
    max_loss_per_share = SPREAD_WIDTH - ic["net_credit"]
    row = {
        "date":            _ist_today().isoformat(),
        "signal":          "IC",
        "strategy":        "nf_iron_condor",
        "ce_short_sid":    str(ic["ce_short_sid"]),
        "ce_long_sid":     str(ic["ce_long_sid"]),
        "pe_short_sid":    str(ic["pe_short_sid"]),
        "pe_long_sid":     str(ic["pe_long_sid"]),
        "ce_short_strike": int(ic["ce_short_strike"]),
        "ce_long_strike":  int(ic["ce_long_strike"]),
        "pe_short_strike": int(ic["pe_short_strike"]),
        "pe_long_strike":  int(ic["pe_long_strike"]),
        "lots":            int(ic["lots"]),
        "qty":             int(qty),
        "lot_size":        int(LOT_SIZE),
        "spot":            round(float(ic["spot"]), 2),
        "atm_strike":      int(ic["atm_strike"]),
        "ce_short_ltp":    round(float(ic["ce_short_ltp"]), 2),
        "ce_long_ltp":     round(float(ic["ce_long_ltp"]), 2),
        "pe_short_ltp":    round(float(ic["pe_short_ltp"]), 2),
        "pe_long_ltp":     round(float(ic["pe_long_ltp"]), 2),
        "ce_credit":       round(float(ic["ce_credit"]), 2),
        "pe_credit":       round(float(ic["pe_credit"]), 2),
        "net_credit":      round(float(ic["net_credit"]), 2),
        "max_loss_inr":    round(max_loss_per_share * qty, 2),
        "max_profit_inr":  round(ic["net_credit"] * qty, 2),
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)
        notify.log(f"PAPER IC trade logged → {path}")
    except Exception as e:
        notify.log(f"Could not write paper_trades.csv: {e}")


def _write_today_ic_trade(ic: dict, signal: str, dte: float, score: int,
                          expiry: date, ml_conf: float, order_result: dict):
    """Write 4-leg IC trade intent to data/today_trade.json."""
    payload = {
        "date":            _ist_today().isoformat(),
        "strategy":        "nf_iron_condor",
        "signal":          signal,
        "atm_strike":      float(ic["atm_strike"]),
        "ce_short_sid":    str(ic["ce_short_sid"]),
        "ce_short_strike": float(ic["ce_short_strike"]),
        "ce_short_entry":  float(ic["ce_short_ltp"]),
        "ce_long_sid":     str(ic["ce_long_sid"]),
        "ce_long_strike":  float(ic["ce_long_strike"]),
        "ce_long_entry":   float(ic["ce_long_ltp"]),
        "pe_short_sid":    str(ic["pe_short_sid"]),
        "pe_short_strike": float(ic["pe_short_strike"]),
        "pe_short_entry":  float(ic["pe_short_ltp"]),
        "pe_long_sid":     str(ic["pe_long_sid"]),
        "pe_long_strike":  float(ic["pe_long_strike"]),
        "pe_long_entry":   float(ic["pe_long_ltp"]),
        "ce_credit":       float(ic["ce_credit"]),
        "pe_credit":       float(ic["pe_credit"]),
        "net_credit":      float(ic["net_credit"]),
        "spread_width":    float(SPREAD_WIDTH),
        "sl_frac":         CREDIT_SL_FRAC,
        "tp_frac":         CREDIT_TP_FRAC,
        "lots":            int(ic["lots"]),
        "lot_size":        int(LOT_SIZE),
        "dte":             float(dte),
        "expiry":          expiry.isoformat() if expiry else None,
        "spot_at_signal":  float(ic["spot"]),
        "signal_score":    int(score),
        "ml_conf":         round(float(ml_conf), 4),
        "order_mode":      order_result.get("mode"),
        "ce_buy_oid":      order_result.get("ce_buy_oid"),
        "ce_sell_oid":     order_result.get("ce_sell_oid"),
        "pe_buy_oid":      order_result.get("pe_buy_oid"),
        "pe_sell_oid":     order_result.get("pe_sell_oid"),
        "exit_done":       False,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(f"{DATA_DIR}/today_trade.json", "w") as f:
            json.dump(payload, f, indent=2)
        notify.log("IC trade intent written → data/today_trade.json")
    except Exception as e:
        notify.log(f"Could not write today_trade.json: {e}")


def place_iron_condor(ic: dict, expiry: date) -> dict:
    """
    Place all 4 IC legs in hedge-margin order:
    1. BUY CE long  2. SELL CE short  3. BUY PE long  4. SELL PE short
    Returns result dict with mode and order IDs.
    """
    qty     = ic["lots"] * LOT_SIZE
    exp_str = expiry.strftime('%Y%m%d')

    if DRY_RUN:
        return {"mode": "DRY_RUN", "net_credit": ic["net_credit"]}

    if PAPER_MODE:
        _log_paper_ic_trade(ic, qty)
        return {
            "mode":     "PAPER",
            "orderId":  f"paper_ic_{date.today():%Y%m%d}",
            "net_credit": ic["net_credit"],
        }

    def _place_leg(trans, sid, leg_label, corr_suffix):
        payload = {
            "dhanClientId":      CLIENT_ID,
            "correlationId":     f"ic_{corr_suffix}_{exp_str}",
            "transactionType":   trans,
            "exchangeSegment":   "NSE_FNO",
            "productType":       "MARGIN",
            "orderType":         "MARKET",
            "validity":          "DAY",
            "securityId":        sid,
            "quantity":          qty,
            "price":             0,
            "triggerPrice":      0,
            "disclosedQuantity": 0,
        }
        resp   = requests.post("https://api.dhan.co/v2/orders",
                               headers=HEADERS, json=payload, timeout=15)
        result = resp.json()
        oid    = result.get("orderId") or result.get("order_id")
        if not oid:
            raise RuntimeError(f"{leg_label} {trans}: no orderId — {str(result)[:150]}")
        notify.log(f"IC {trans} {leg_label} qty={qty} orderId={oid}")
        return oid

    # Leg 1: BUY CE long (hedge — must be first for margin benefit)
    try:
        ce_buy_oid = _place_leg("BUY", ic["ce_long_sid"],
                                f"CE {int(ic['ce_long_strike'])}", "ce_buy")
    except Exception as e:
        notify.send(
            f"❌ <b>IC Failed — CE BUY (hedge) error</b>\n\n"
            f"CE +{SPREAD_WIDTH} {int(ic['ce_long_strike'])}, qty={qty}\n{e}\n"
            f"No position opened."
        )
        return {"mode": "FAILED"}

    time.sleep(2)

    # Leg 2: SELL CE short (credit)
    try:
        ce_sell_oid = _place_leg("SELL", ic["ce_short_sid"],
                                 f"CE {int(ic['ce_short_strike'])}", "ce_sell")
    except Exception as e:
        notify.send(
            f"🚨 <b>CRITICAL — IC CE SELL failed after CE BUY placed</b>\n\n"
            f"CE BUY orderId: {ce_buy_oid}\n{e}\n\n"
            f"NAKED LONG CE {int(ic['ce_long_strike'])} x{qty}!\n"
            f"IMMEDIATELY SELL CE {int(ic['ce_short_strike'])} qty={qty} on Dhan app."
        )
        return {"mode": "PARTIAL_IC", "ce_buy_oid": ce_buy_oid}

    time.sleep(2)

    # Leg 3: BUY PE long (hedge)
    try:
        pe_buy_oid = _place_leg("BUY", ic["pe_long_sid"],
                                f"PE {int(ic['pe_long_strike'])}", "pe_buy")
    except Exception as e:
        notify.send(
            f"🚨 <b>CRITICAL — IC PE BUY failed (CE spread placed)</b>\n\n"
            f"CE: BUY={ce_buy_oid} SELL={ce_sell_oid}\n{e}\n\n"
            f"CE spread is on. IMMEDIATELY also BUY PE {int(ic['pe_long_strike'])} "
            f"AND SELL PE {int(ic['pe_short_strike'])} qty={qty} on Dhan app."
        )
        return {"mode": "PARTIAL_IC_CE_ONLY",
                "ce_buy_oid": ce_buy_oid, "ce_sell_oid": ce_sell_oid}

    time.sleep(2)

    # Leg 4: SELL PE short (credit)
    try:
        pe_sell_oid = _place_leg("SELL", ic["pe_short_sid"],
                                 f"PE {int(ic['pe_short_strike'])}", "pe_sell")
    except Exception as e:
        notify.send(
            f"🚨 <b>CRITICAL — IC PE SELL failed after PE BUY placed</b>\n\n"
            f"CE: BUY={ce_buy_oid} SELL={ce_sell_oid}\n"
            f"PE: BUY={pe_buy_oid}\n{e}\n\n"
            f"NAKED LONG PE {int(ic['pe_long_strike'])} x{qty}!\n"
            f"IMMEDIATELY SELL PE {int(ic['pe_short_strike'])} qty={qty} on Dhan app."
        )
        return {"mode": "PARTIAL_IC",
                "ce_buy_oid": ce_buy_oid, "ce_sell_oid": ce_sell_oid,
                "pe_buy_oid": pe_buy_oid}

    return {
        "mode":         "IRON_CONDOR",
        "ce_buy_oid":   ce_buy_oid,
        "ce_sell_oid":  ce_sell_oid,
        "pe_buy_oid":   pe_buy_oid,
        "pe_sell_oid":  pe_sell_oid,
        "net_credit":   ic["net_credit"],
    }


# ── Short Straddle helpers ─────────────────────────────────────────────────────

def _fetch_straddle_margin_per_lot(ce_sid, ce_ltp, pe_sid, pe_ltp) -> float:
    """Dhan /margincalculator/multi for 1 NF short straddle lot (SELL ATM CE + SELL ATM PE)."""
    try:
        payload = {
            "dhanClientId":    CLIENT_ID,
            "includePosition": True,
            "includeOrders":   True,
            "scripList": [
                {"exchangeSegment": "NSE_FNO", "transactionType": "SELL",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(ce_sid), "price": float(ce_ltp), "triggerPrice": 0},
                {"exchangeSegment": "NSE_FNO", "transactionType": "SELL",
                 "quantity": LOT_SIZE, "productType": "MARGIN",
                 "securityId": str(pe_sid), "price": float(pe_ltp), "triggerPrice": 0},
            ],
        }
        resp = requests.post("https://api.dhan.co/v2/margincalculator/multi",
                             headers=HEADERS, json=payload, timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            margin = float(d.get("total_margin") or d.get("totalMargin") or 0)
            if margin > 50_000:
                notify.log(f"Straddle margin/lot from Dhan: ₹{margin:,.0f}")
                return margin
        notify.log(f"Straddle margin API issue — using fallback ₹{STRADDLE_MARGIN_PER_LOT:,.0f}")
    except Exception as e:
        notify.log(f"Straddle margin API error: {e} — using fallback")
    return float(STRADDLE_MARGIN_PER_LOT)


def get_straddle_legs(expiry: date, capital: float) -> dict | None:
    """
    Fetch both straddle legs from the live option chain.
    Short Straddle: SELL ATM CE + SELL ATM PE (no wing protection)
    Returns dict with leg data + lot sizing, or None on failure.
    """
    expiry_candidates = [expiry, expiry + timedelta(days=7)]

    for exp in expiry_candidates:
        for attempt in range(3):
            try:
                data, atm_strike, spot, inner = _fetch_option_chain(exp)
                if data is None:
                    if attempt < 2:
                        notify.log(f"Straddle: retry {attempt+1}/3 for {exp}...")
                        time.sleep(3)
                    continue

                oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
                if not oc:
                    notify.log(f"Straddle: empty OC for {exp}")
                    break

                ce_sid, ce_ltp = _get_oc_leg(oc, atm_strike, "ce")
                pe_sid, pe_ltp = _get_oc_leg(oc, atm_strike, "pe")

                if not ce_sid or not pe_sid:
                    notify.log(
                        f"Straddle: ATM legs missing (CE={ce_sid} PE={pe_sid}) exp={exp}")
                    break

                net_credit = ce_ltp + pe_ltp
                if net_credit <= 0:
                    notify.log(f"Straddle: net credit ≤ 0 ({net_credit:.1f})")
                    break

                margin_1lot = _fetch_straddle_margin_per_lot(ce_sid, ce_ltp, pe_sid, pe_ltp)
                lots = min(MAX_LOTS_STRADDLE, int(capital // margin_1lot))
                if lots < 1:
                    notify.log(
                        f"Straddle: insufficient capital "
                        f"(need ₹{margin_1lot:,.0f}, have ₹{capital:,.0f})")
                    return None

                notify.log(
                    f"Straddle: ATM={atm_strike} CE ₹{ce_ltp:.0f} + PE ₹{pe_ltp:.0f} "
                    f"= net credit ₹{net_credit:.0f} → {lots} lot(s)")
                return {
                    "ce_sid":         ce_sid,
                    "pe_sid":         pe_sid,
                    "ce_ltp":         ce_ltp,
                    "pe_ltp":         pe_ltp,
                    "net_credit":     net_credit,
                    "atm_strike":     atm_strike,
                    "lots":           lots,
                    "margin_per_lot": round(margin_1lot, 0),
                    "spot":           spot,
                    "expiry":         exp,
                }
            except Exception as e:
                notify.log(f"Straddle legs exception (exp={exp}, attempt={attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(3)
        notify.log(f"Straddle: all attempts failed for {exp} — trying next expiry")

    return None


def _log_paper_straddle_trade(st: dict, qty: int):
    import csv as _csv
    path = f"{DATA_DIR}/paper_trades.csv"
    cols = [
        "date", "signal", "strategy",
        "ce_sid", "pe_sid", "atm_strike",
        "lots", "qty", "lot_size", "spot",
        "ce_ltp", "pe_ltp", "net_credit", "max_profit_inr",
    ]
    row = {
        "date":           _ist_today().isoformat(),
        "signal":         "STRADDLE",
        "strategy":       "nf_short_straddle",
        "ce_sid":         str(st["ce_sid"]),
        "pe_sid":         str(st["pe_sid"]),
        "atm_strike":     int(st["atm_strike"]),
        "lots":           int(st["lots"]),
        "qty":            int(qty),
        "lot_size":       int(LOT_SIZE),
        "spot":           round(float(st["spot"]), 2),
        "ce_ltp":         round(float(st["ce_ltp"]), 2),
        "pe_ltp":         round(float(st["pe_ltp"]), 2),
        "net_credit":     round(float(st["net_credit"]), 2),
        "max_profit_inr": round(st["net_credit"] * qty, 2),
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)
        notify.log(f"PAPER straddle trade logged → {path}")
    except Exception as e:
        notify.log(f"Could not write paper_trades.csv (straddle): {e}")


def _write_today_straddle_trade(st: dict, signal: str, dte: float, score: int,
                                expiry: date, ml_conf: float, order_result: dict):
    """Write short straddle trade intent to data/today_trade.json."""
    payload = {
        "date":           _ist_today().isoformat(),
        "strategy":       "nf_short_straddle",
        "signal":         signal,
        "atm_strike":     float(st["atm_strike"]),
        "ce_sid":         str(st["ce_sid"]),
        "pe_sid":         str(st["pe_sid"]),
        "ce_entry":       float(st["ce_ltp"]),
        "pe_entry":       float(st["pe_ltp"]),
        "net_credit":     float(st["net_credit"]),
        "sl_frac":        CREDIT_SL_FRAC,
        "lots":           int(st["lots"]),
        "lot_size":       int(LOT_SIZE),
        "dte":            float(dte),
        "expiry":         expiry.isoformat() if expiry else None,
        "spot_at_signal": float(st["spot"]),
        "signal_score":   int(score),
        "ml_conf":        round(float(ml_conf), 4),
        "order_mode":     order_result.get("mode"),
        "ce_sell_oid":    order_result.get("ce_sell_oid"),
        "pe_sell_oid":    order_result.get("pe_sell_oid"),
        "exit_done":      False,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(f"{DATA_DIR}/today_trade.json", "w") as f:
            json.dump(payload, f, indent=2)
        notify.log("Straddle trade intent written → data/today_trade.json")
    except Exception as e:
        notify.log(f"Could not write today_trade.json (straddle): {e}")


def place_straddle(st: dict, expiry: date) -> dict:
    """
    Place Short Straddle: SELL ATM CE + SELL ATM PE.
    No wing legs — plain SELL both ATM options. Higher margin than IC.
    """
    qty     = st["lots"] * LOT_SIZE
    exp_str = expiry.strftime('%Y%m%d')

    if DRY_RUN:
        return {"mode": "DRY_RUN", "net_credit": st["net_credit"]}

    if PAPER_MODE:
        _log_paper_straddle_trade(st, qty)
        return {
            "mode":       "PAPER",
            "orderId":    f"paper_straddle_{date.today():%Y%m%d}",
            "net_credit": st["net_credit"],
        }

    base = {
        "dhanClientId":      CLIENT_ID,
        "exchangeSegment":   "NSE_FNO",
        "productType":       "MARGIN",
        "orderType":         "MARKET",
        "validity":          "DAY",
        "quantity":          qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }

    # SELL ATM CE
    try:
        r1 = requests.post(
            "https://api.dhan.co/v2/orders", headers=HEADERS,
            json={**base, "correlationId": f"straddle_ce_{exp_str}",
                  "transactionType": "SELL", "securityId": st["ce_sid"]},
            timeout=15,
        )
        res1 = r1.json()
        ce_sell_oid = res1.get("orderId") or res1.get("order_id")
        if not ce_sell_oid:
            notify.send(
                f"❌ <b>Straddle CE SELL failed — no orderId</b>\n\n"
                f"ATM CE {int(st['atm_strike'])} qty={qty}\n"
                f"Response: {str(res1)[:200]}\nNo position opened."
            )
            return {"mode": "FAILED"}
        notify.log(
            f"Straddle SELL CE {int(st['atm_strike'])} qty={qty} orderId={ce_sell_oid}")
    except Exception as e:
        notify.send(f"❌ <b>Straddle CE SELL exception</b>\n\n{e}\nNo position opened.")
        return {"mode": "FAILED"}

    time.sleep(2)

    # SELL ATM PE
    try:
        r2 = requests.post(
            "https://api.dhan.co/v2/orders", headers=HEADERS,
            json={**base, "correlationId": f"straddle_pe_{exp_str}",
                  "transactionType": "SELL", "securityId": st["pe_sid"]},
            timeout=15,
        )
        res2 = r2.json()
        pe_sell_oid = res2.get("orderId") or res2.get("order_id")
        if not pe_sell_oid:
            notify.send(
                f"🚨 <b>CRITICAL — Straddle PE SELL no orderId after CE SELL</b>\n\n"
                f"CE SELL orderId: {ce_sell_oid}\n"
                f"ATM PE {int(st['atm_strike'])} qty={qty}\n"
                f"Response: {str(res2)[:200]}\n\n"
                f"<b>NAKED SHORT CE {int(st['atm_strike'])} x{qty}!</b>\n"
                f"IMMEDIATELY SELL PE {int(st['atm_strike'])} qty={qty} on Dhan app."
            )
            return {"mode": "PARTIAL_STRADDLE", "ce_sell_oid": ce_sell_oid}
        notify.log(
            f"Straddle SELL PE {int(st['atm_strike'])} qty={qty} orderId={pe_sell_oid}")
    except Exception as e:
        notify.send(
            f"🚨 <b>CRITICAL — Straddle PE SELL exception</b>\n\n"
            f"CE SELL orderId: {ce_sell_oid}\nException: {e}\n\n"
            f"<b>NAKED SHORT CE {int(st['atm_strike'])} x{qty}!</b>\n"
            f"IMMEDIATELY SELL PE {int(st['atm_strike'])} qty={qty} on Dhan app."
        )
        return {"mode": "PARTIAL_STRADDLE", "ce_sell_oid": ce_sell_oid}

    return {
        "mode":         "STRADDLE",
        "ce_sell_oid":  ce_sell_oid,
        "pe_sell_oid":  pe_sell_oid,
        "net_credit":   st["net_credit"],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        mode_label = "DRY RUN"
    elif PAPER_MODE:
        mode_label = "PAPER"
    else:
        mode_label = "LIVE"
    instr_label = "Nifty IC" if IRON_CONDOR_MODE else "Nifty50"
    notify.log(f"{instr_label} Auto Trader starting [{mode_label}]")

    # 0. Credentials + lot-size sanity check
    check_credentials()
    _check_lot_size()

    # 0b. Verify yesterday's exit ran — catch open overnight positions before trading
    # Skipped in PAPER mode: paper trades have no real position to exit.
    if not DRY_RUN and not PAPER_MODE:
        _check_exit_marker()

    # 1. Refresh data + signal
    refresh_data_and_signal()

    # 1b. Holiday check — if Nifty50 has no data for today, NSE is closed
    # (handles Diwali, Republic Day, Holi, etc. without a manual holiday list)
    if not _is_trading_day() and not DRY_RUN:
        today_label = _ist_today().strftime("%d %b %Y")
        notify.send(
            f"📆  <b>Market Holiday</b>\n\n"
            f"{today_label} — NSE is closed today.\n"
            f"No trade placed. See you tomorrow."
        )
        notify.log(f"Market holiday detected ({today_label}) — no BN data in CSV. Exiting.")
        return

    # 2. Wednesday hard skip (no profitable strategy — all options lose on DTE 6)
    from datetime import datetime as _dt
    _today_ist     = _dt.now(timezone(timedelta(hours=5, minutes=30))).date()
    _today_weekday = _today_ist.weekday()
    today_wd       = _ist_today().strftime("%A")
    today_label    = _ist_today().strftime("%d %b %Y")
    if IRON_CONDOR_MODE and _today_weekday in IC_SKIP_DAYS:
        # IC_SKIP_DAYS = set() — this block never runs in the Tue-expiry regime.
        # All 5 weekdays are valid. Kept for future use if regime changes.
        notify.send(
            f"⏸  <b>No Trade — {today_wd}</b>\n"
            f"─────────────────────\n"
            f"{today_label}\n\nCapital preserved."
        )
        notify.log(f"DOW filter: {today_wd} — no trade")
        return

    # 3. Read signal
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
    _use_bull_put_today  = (signal == "PUT")  # PUT signal days → Bull Put (2 legs, Dhan-margin-sized lots)

    # ── News sentiment (morning_brief.py output, written at 9:15 AM) ────────
    news_vote    = 0    # +1 aligns with signal, -1 opposes
    news_note    = ""
    try:
        ns_path = f"{DATA_DIR}/news_sentiment.json"
        if os.path.exists(ns_path):
            with open(ns_path) as _f:
                ns = json.load(_f)
            # Only use if generated today and within 6 hours
            ns_date = ns.get("date", "")
            ns_gen  = ns.get("generated", "")
            ns_age  = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(ns_gen)).total_seconds() / 3600
            if ns_date == _ist_today().isoformat() and ns_age < 6:
                direction  = ns.get("direction", "NEUTRAL")
                confidence = ns.get("confidence", "LOW")
                conf_weight = {"HIGH": 1, "MEDIUM": 1, "LOW": 0}.get(confidence, 0)
                if conf_weight:
                    if (direction == "BULLISH" and signal == "CALL") or \
                       (direction == "BEARISH" and signal == "PUT"):
                        news_vote = 1
                        news_note = f"News: {direction} ({confidence}) — aligns ✓"
                    elif (direction == "BULLISH" and signal == "PUT") or \
                         (direction == "BEARISH" and signal == "CALL"):
                        news_vote = -1
                        news_note = f"News: {direction} ({confidence}) — CONFLICTS ⚠"
                    else:
                        news_note = f"News: NEUTRAL — no vote"
                else:
                    news_note = f"News: {direction} (LOW confidence — no vote)"
    except Exception:
        pass

    # ── VIX regime filter ────────────────────────────────────────────────────
    # Model accuracy: VIX<13 = 46.7% (losing), VIX 13-18 = 57.4%, VIX>18 = 62.9%
    # Ceiling: VIX>20 = panic regime; real-options backtest showed P&L drops sharply.
    # IC bypasses VIX filter — no-filter gives maximum P&L per optimize_params.py.
    vix_now = get_vix_level()
    if not IRON_CONDOR_MODE and VIX_MIN_TRADE > 0 and vix_now < VIX_MIN_TRADE:
        notify.send(
            f"⏸  <b>No Trade — Low Volatility Day</b>\n"
            f"─────────────────────\n"
            f"{today_wd}  ·  {today_label}\n\n"
            f"VIX at {vix_now:.1f}  (below {VIX_MIN_TRADE:.0f} threshold)\n"
            f"In calm markets our model is 47% accurate — worse than a coin flip.\n"
            f"Waiting for a higher-conviction setup. Capital preserved."
        )
        notify.log(f"VIX regime filter: VIX={vix_now:.1f} < {VIX_MIN_TRADE} — no trade")
        return
    if not IRON_CONDOR_MODE and VIX_MAX_TRADE > 0 and vix_now > VIX_MAX_TRADE:
        notify.send(
            f"⏸  <b>No Trade — Panic Volatility Day</b>\n"
            f"─────────────────────\n"
            f"{today_wd}  ·  {today_label}\n\n"
            f"VIX at {vix_now:.1f}  (above {VIX_MAX_TRADE:.0f} ceiling)\n"
            f"In panic markets our entry timing edge evaporates — option premiums are\n"
            f"already inflated so +15% target becomes nearly unreachable.\n"
            f"Waiting for a calmer setup. Capital preserved."
        )
        notify.log(f"VIX regime filter: VIX={vix_now:.1f} > {VIX_MAX_TRADE} — no trade")
        return

    # ── IC P&L skip-filter (shadow logging by default; gate trades when ENABLED) ─
    # Loads shadow predictor from models/ic_pnl_predictor.pkl (Phase 1 = log only).
    # To enable actual skipping: set ENABLE_SKIP_FILTER=1 in .env (after 30 trades validate).
    _skip_filter_threshold = 0.40   # P(strategy_wins) below this → skip
    _skip_decision = None
    try:
        import joblib as _joblib
        _ic_pkl  = "models/ic_pnl_predictor.pkl"
        _ic_meta_path = "models/ic_pnl_predictor_meta.json"
        if os.path.exists(_ic_pkl) and os.path.exists(_ic_meta_path):
            with open(_ic_meta_path) as _mf:
                _ic_meta_loaded = json.load(_mf)
            _ic_features = _ic_meta_loaded.get("feature_cols", [])
            _ic_predictor = _joblib.load(_ic_pkl)
            from ml_engine import get_today_features as _get_today_feats
            _Xt = _get_today_feats(_ic_features)
            if _Xt is not None and len(_Xt) > 0:
                _proba = _ic_predictor.predict_proba(_Xt)[0]
                _classes = list(_ic_predictor.classes_)
                _p_win = float(_proba[_classes.index(1)]) if 1 in _classes else 0.5
                _enabled = os.getenv("ENABLE_SKIP_FILTER", "0") == "1"
                _skip_decision = {
                    "p_win": _p_win,
                    "would_skip": _p_win < _skip_filter_threshold,
                    "enabled":    _enabled,
                }
                # Log to CSV regardless
                _skip_csv = f"{DATA_DIR}/skip_decisions.csv"
                _is_new = not os.path.exists(_skip_csv)
                with open(_skip_csv, "a") as _sf:
                    if _is_new:
                        _sf.write("date,signal,ml_conf,p_win,would_skip,enabled,actual_pnl\n")
                    _sf.write(
                        f"{_ist_today().isoformat()},{signal},{ml_conf:.4f},"
                        f"{_p_win:.4f},{_skip_decision['would_skip']},{_enabled},\n"
                    )
                notify.log(
                    f"Skip filter (shadow): P(win)={_p_win:.1%}  "
                    f"would_skip={_skip_decision['would_skip']}  enabled={_enabled}"
                )
                # Actually skip only if user enabled the filter
                if _enabled and _skip_decision["would_skip"]:
                    notify.send(
                        f"⏸  <b>No Trade — Skip Filter Triggered</b>\n\n"
                        f"IC P&L predictor says: {_p_win:.0%} chance of profit today\n"
                        f"Below {_skip_filter_threshold:.0%} threshold → skipping.\n\n"
                        f"This filter activates trades with high win-conviction only."
                    )
                    notify.log(f"Skip filter: P(win)={_p_win:.1%} below threshold — no trade")
                    return
    except Exception as _e:
        notify.log(f"Skip filter check failed: {_e}")

    # ── ML confidence gate ───────────────────────────────────────────────────
    # IC bypasses ML confidence filter — no-filter gives maximum P&L.
    if not IRON_CONDOR_MODE and ML_CONF_THRESHOLD > 0 and ml_trained and ml_conf < ML_CONF_THRESHOLD:
        notify.send(
            f"⏸  <b>No Trade — Low ML Confidence</b>\n"
            f"─────────────────────\n"
            f"{today_wd}  ·  {today_label}\n\n"
            f"ML ensemble confidence {ml_conf:.0%}  (need ≥{ML_CONF_THRESHOLD:.0%})\n"
            f"Signal was {signal} but models don't agree strongly enough.\n"
            f"Skipping to preserve capital on low-conviction days."
        )
        notify.log(f"ML conf filter: {ml_conf:.0%} < {ML_CONF_THRESHOLD:.0%} — no trade")
        return

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

    # Straddle auto-upgrade: if capital ≥ 1 straddle lot margin, switch all days to Short Straddle.
    # Straddle has no wing protection → higher margin (~₹2.3L vs IC ₹93K) but more credit.
    # Skips IC/Bull Put routing when active (straddle replaces IC on CALL days only).
    _use_straddle_today = (
        IRON_CONDOR_MODE
        and capital >= STRADDLE_MARGIN_PER_LOT
    )
    if _use_straddle_today:
        notify.log(
            f"Straddle auto-upgrade active: "
            f"capital ₹{capital:,.0f} ≥ ₹{STRADDLE_MARGIN_PER_LOT:,.0f}/lot"
        )

    # 4. Expiry
    expiry = get_expiry()
    dte    = max(0.25, (expiry - _ist_today()).days + 1)

    # Shared display helpers (used by both credit spread and naked option paths)
    opt_type  = "CE" if signal == "CALL" else "PE"
    opt_emoji = "📈" if signal == "CALL" else "📉"
    cap_label = f"₹{capital:,.0f}" + ("  [DRY RUN]" if DRY_RUN else "")
    if abs(score) == score_max:
        score_desc = "  ● max signal ●"
    elif abs(score) >= 3:
        score_desc = "  ● strong ●"
    elif abs(score) == 2:
        score_desc = ""
    else:
        score_desc = "  ● weak ●"
    sig_line  = f"\n<i>↳ {sig_note}</i>" if sig_note else ""
    news_row  = f"News       {news_note}\n" if news_note else ""
    # ML-vs-rule conflict: rule score disagrees with ML signal direction
    # e.g. score=-1 (rules say PUT) but signal=CALL (ML overrides)
    rule_says_call = score > 0
    ml_says_call   = (signal == "CALL")
    _ml_conflict = ml_trained and (rule_says_call != ml_says_call) and score != 0
    ml_conflict_row = (
        f"⚠️ ML override  rules say {'CALL' if rule_says_call else 'PUT'} "
        f"(score {score:+d}) but ML says {signal} — ML wins\n"
    ) if _ml_conflict else ""

    # ── Plain-English helpers for Telegram messages ──────────────────────────
    if abs(score) == score_max:
        score_plain = f"all {score_max}/{score_max} checks green ✓"
    elif abs(score) >= 3:
        score_plain = f"{abs(score)}/{score_max} checks green ✓"
    elif abs(score) >= 2:
        score_plain = f"{abs(score)}/{score_max} checks lean one way"
    else:
        score_plain = f"{abs(score)}/{score_max} checks lean — weak signal"
    if ml_conf >= 0.70:
        conf_plain = "high confidence"
    elif ml_conf >= 0.60:
        conf_plain = "moderate confidence"
    else:
        conf_plain = "low confidence"
    ml_conflict_plain = (
        f"⚠️  Basic checks lean {'up' if rule_says_call else 'down'} "
        f"but computer model says {'up' if ml_says_call else 'down'} — going with model\n"
    ) if _ml_conflict else ""
    if news_vote == 1:
        _nd = "positive" if "BULLISH" in news_note else "mixed"
        news_plain_line = f"📰 Headlines: {_nd} — supports trade ✓\n"
    elif news_vote == -1:
        _nd = "negative" if "BEARISH" in news_note else "mixed"
        news_plain_line = f"📰 Headlines: {_nd} — conflicts with trade ⚠️\n"
    elif news_note:
        news_plain_line = "📰 Headlines: neutral — no strong view\n"
    else:
        news_plain_line = ""

    # ══════════════════════════════════════════════════════════════════════════
    # SHORT STRADDLE PATH  (_use_straddle_today = True — capital auto-upgrade)
    # SELL ATM CE + SELL ATM PE — unhedged, all Mon/Tue/Thu/Fri signal days
    # Triggers when capital ≥ STRADDLE_MARGIN_PER_LOT (≈₹2.3L). Overrides IC/Bull Put.
    # ══════════════════════════════════════════════════════════════════════════
    if IRON_CONDOR_MODE and _use_straddle_today:
        st = get_straddle_legs(expiry, capital)
        if st is None:
            # Leg fetch failed or margin spiked above capital — fall through to IC/Bull Put routing.
            # This can happen on high-IV days when Dhan SPAN > STRADDLE_MARGIN_PER_LOT threshold.
            notify.send(
                f"⚠️  <b>Straddle downgrade</b>\n\n"
                f"Capital ₹{capital:,.0f} but straddle legs unavailable or margin too high today.\n"
                f"Falling back to IC / Bull Put routing."
            )
            notify.log("Straddle legs None — falling back to IC/spread routing")
            _use_straddle_today = False

        if _use_straddle_today and st is not None:
            exp_used = st.get("expiry", expiry)
            nf_expiry_str = exp_used.strftime('%d%b%Y').upper()
            sl_trig = st["net_credit"] * (1 + CREDIT_SL_FRAC)

            notify.send(
                f"⚔️  <b>Nifty Short Straddle</b>  ·  {today_wd}, {today_label}{sig_line}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Score      {score:+d} / {score_max}{score_desc}\n"
                f"ML conf    {ml_conf:.0%}{'  ✓' if ml_conf >= ML_CONF_THRESHOLD else ''}\n"
                f"{news_row}"
                f"Capital    {cap_label}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} CE</code>  @ ₹{st['ce_ltp']:.0f}\n"
                f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} PE</code>  @ ₹{st['pe_ltp']:.0f}\n"
                f"Net credit  ₹{st['net_credit']:.0f} / share   "
                f"({st['lots']} lot{'s' if st['lots'] > 1 else ''}  ·  {st['lots']*LOT_SIZE} shares)\n"
                f"Spot        ₹{st['spot']:,.0f}   DTE {dte:.1f}   Expiry {exp_used.strftime('%d %b')}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"SL   buyback cost > ₹{sl_trig:.0f}  ({CREDIT_SL_FRAC*100:.0f}% above credit)\n"
                f"Exit EOD 3:15 PM  (no TP — holds for maximum theta decay)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Max profit ₹{st['lots'] * st['net_credit'] * LOT_SIZE:,.0f}  (credit kept if expires flat)\n"
                f"<i>Unhedged — no wings. Margin ≈₹{st['margin_per_lot']:,.0f}/lot.</i>"
            )

            if not DRY_RUN and not PAPER_MODE and _check_no_existing_position():
                notify.send(
                    f"⚠️  <b>Duplicate Trade Blocked</b>\n\n"
                    f"An open Nifty position already exists.\n"
                    f"Skipping straddle to avoid double exposure."
                )
                return

            order_result = place_straddle(st, exp_used)
            if not DRY_RUN:
                _write_today_straddle_trade(
                    st, signal, dte, score, exp_used, ml_conf, order_result)

            mode = order_result.get("mode")
            if mode == "DRY_RUN":
                notify.send(
                    f"✅  <b>Dry Run — Nifty Short Straddle</b>\n\n"
                    f"Would have placed:\n"
                    f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} CE</code>  @ ₹{st['ce_ltp']:.0f}\n"
                    f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} PE</code>  @ ₹{st['pe_ltp']:.0f}\n"
                    f"{st['lots']} lot{'s' if st['lots'] > 1 else ''}  ·  Net credit ₹{st['net_credit']:.0f}\n\n"
                    f"<i>DRY RUN — no real order placed.</i>"
                )
                return
            if mode == "PAPER":
                notify.send(
                    f"📝  <b>[PAPER] Nifty Short Straddle — No Real Order</b>\n\n"
                    f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} CE</code>  @ ₹{st['ce_ltp']:.0f}\n"
                    f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} PE</code>  @ ₹{st['pe_ltp']:.0f}\n"
                    f"{st['lots']} lot{'s' if st['lots'] > 1 else ''}  ·  Net credit ₹{st['net_credit']:.0f}\n"
                    f"<i>PAPER MODE. Real money safe. Tracking in data/paper_trades.csv.</i>"
                )
                return
            if mode == "FAILED":
                notify.log("Straddle placement failed — no position. See earlier error.")
                return
            if mode and "PARTIAL" in mode:
                notify.log(f"Partial straddle ({mode}) — emergency alert sent. Check Dhan app immediately.")
                return

            # Full success — safe to arm account-level P&L safety net.
            if not DRY_RUN:
                _setup_pnl_exit(st["net_credit"], st["lots"])

            notify.send(
                f"✅  <b>Nifty Short Straddle Placed!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"CE SELL  <code>{order_result.get('ce_sell_oid')}</code>\n"
                f"PE SELL  <code>{order_result.get('pe_sell_oid')}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} CE</code>  @ ₹{st['ce_ltp']:.0f}\n"
                f"SELL  <code>NIFTY {nf_expiry_str} {int(st['atm_strike'])} PE</code>  @ ₹{st['pe_ltp']:.0f}\n"
                f"Net credit ₹{st['net_credit']:.0f}  ·  "
                f"{st['lots']} lot{'s' if st['lots'] > 1 else ''}  ·  {st['lots']*LOT_SIZE} shares\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"SL   buyback > ₹{sl_trig:.0f}  (auto-exit: spread_monitor.py)\n"
                f"<i>spread_monitor.py watches every 1 min — both legs exit on SL.</i>"
            )
            return

    # Bear Call path permanently removed Apr 2026 — 13.5% WR, -₹24.03L over 7yr.
    # See STRATEGY_RESEARCH.md + docs/wiki/strategy/ic_research.md.

    # ══════════════════════════════════════════════════════════════════════════
    # BULL PUT PATH  (PUT signal days, all weekdays — _use_bull_put_today = True)
    # NF Bull Put: BUY ATM-150 PE + SELL ATM PE (2 legs, directional credit)
    # Backtest Sep 2025–Apr 2026: 100% WR, ₹3,794/trade avg (51 trades)
    # ══════════════════════════════════════════════════════════════════════════
    if IRON_CONDOR_MODE and _use_bull_put_today:
        (short_sid, long_sid, short_strike, long_strike,
         short_ltp, long_ltp, net_credit, _, spot) = get_spread_legs(
             signal, expiry, capital)   # signal == "PUT"

        if short_sid is None:
            die(f"Could not fetch Bull Put legs for expiry {expiry}.")

        # Dhan margin API gives actual SPAN lot sizing (formula in get_spread_legs over-sizes)
        margin_1lot = _fetch_spread_margin_per_lot(short_sid, short_ltp, long_sid, long_ltp)
        lots = min(MAX_LOTS, int(capital // margin_1lot))
        if lots < 1:
            notify.send(
                f"ℹ️  <b>Bull Put unaffordable — switching to Iron Condor</b>\n\n"
                f"Bull Put needs ₹{margin_1lot:,.0f}/lot but capital is ₹{capital:,.0f}.\n"
                f"IC is market-neutral and costs ~₹93K/lot — placing IC instead."
            )
            _use_bull_put_today = False   # fall through to IC path below
        if _use_bull_put_today:
            nf_expiry_str = expiry.strftime('%d%b%Y').upper()
            max_loss_per_lot = (SPREAD_WIDTH - net_credit) * LOT_SIZE
            sl_trig = net_credit * (1 + CREDIT_SL_FRAC)

            _bp_total_credit = int(net_credit * lots * LOT_SIZE)
            _bp_sl_loss      = int(net_credit * CREDIT_SL_FRAC * lots * LOT_SIZE)
            _bp_tp_gain      = int(net_credit * CREDIT_TP_FRAC * lots * LOT_SIZE)

            notify.send(
                f"🐂  <b>Bull Put placed</b>  ·  {today_wd}, {today_label}{sig_line}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>What the model checked</b>\n"
                f"Market checklist: {score_plain}\n"
                f"Computer model: {ml_conf:.0%} confident ({conf_plain})\n"
                f"Direction: market expected to stay flat or go up\n"
                f"{ml_conflict_plain}"
                f"{news_plain_line}"
                f"Capital: {cap_label}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>The trade</b>\n"
                f"Collected <b>₹{_bp_total_credit:,}</b> upfront from 2 option contracts\n"
                f"(₹{net_credit:.0f}/share × {lots*LOT_SIZE:,} shares, {lots} lot{'s' if lots > 1 else ''})\n\n"
                f"Profit zone: Nifty stays ABOVE ₹{int(short_strike):,} until expiry\n"
                f"Safety net: ₹{int(long_strike):,} (caps our max loss if market falls hard)\n"
                f"Nifty now: ₹{spot:,.0f}  ·  Expiry {expiry.strftime('%d %b')} ({int(dte)}d to go)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Risk</b>\n"
                f"Best case (Nifty stays above ₹{int(short_strike):,}):  +₹{_bp_total_credit:,}\n"
                f"Take-profit (65% locked in, exit early):          +₹{_bp_tp_gain:,}\n"
                f"Worst case (stop-loss fires):                      −₹{_bp_sl_loss:,}\n"
                f"Stop-loss fires if spread cost > ₹{sl_trig:.0f}/share\n\n"
                f"Take-profit or 3:15 PM exit — whichever comes first"
            )

            if not DRY_RUN and not PAPER_MODE and _check_no_existing_position():
                notify.send(
                    f"⚠️  <b>Duplicate Trade Blocked</b>\n\n"
                    f"An open Nifty position already exists.\n"
                    f"Skipping Bull Put to avoid double exposure."
                )
                return

            order_result = place_credit_spread(
                short_sid, long_sid, signal, lots, net_credit,
                short_strike, long_strike, short_ltp, long_ltp, spot,
            )
            if not DRY_RUN:
                _write_today_spread_trade(
                    signal=signal,
                    short_sid=short_sid, long_sid=long_sid,
                    short_strike=short_strike, long_strike=long_strike,
                    short_ltp=short_ltp, long_ltp=long_ltp,
                    net_credit=net_credit, lots=lots, dte=dte, spot=spot,
                    score=score, expiry=expiry, ml_conf=ml_conf,
                    order_mode=order_result.get("mode", "CREDIT_SPREAD"),
                    buy_oid=order_result.get("buy_oid"),
                    sell_oid=order_result.get("sell_oid"),
                )
                # Arm account-level P&L safety net ONLY on full success.
                # PARTIAL / FAILED → no pnlExit (would be configured against a
                # non-existent or partial position and trigger at wrong threshold).
                _bp_mode = order_result.get("mode", "")
                if _bp_mode and "PARTIAL" not in _bp_mode and _bp_mode != "FAILED":
                    _setup_pnl_exit(net_credit, lots)
                else:
                    notify.log(
                        f"Bull Put mode={_bp_mode} — skipping pnlExit setup. "
                        f"Manual intervention may be required."
                    )
            return

    # ══════════════════════════════════════════════════════════════════════════
    # IRON CONDOR PATH  (CALL signal days; also PUT days when Bull Put unaffordable)
    # NF IC: SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE
    # Backtest Sep 2025–Apr 2026: 97.5% WR (67 CALL days out of 118 total)
    # ══════════════════════════════════════════════════════════════════════════
    if IRON_CONDOR_MODE and not _use_bull_put_today:
        ic = get_ic_legs(expiry, capital)
        if ic is None:
            if capital < IC_MARGIN_PER_LOT:
                notify.send(
                    f"ℹ️  <b>No trade today — insufficient capital</b>\n\n"
                    f"IC needs ~₹{IC_MARGIN_PER_LOT:,.0f}/lot but balance is ₹{capital:,.0f}.\n"
                    f"No position placed. Waiting for capital to rebuild."
                )
                return
            die(
                f"Could not fetch Nifty IC legs for expiry {expiry}.\n"
                f"All 4 legs (ATM CE/PE + ATM±{SPREAD_WIDTH} CE/PE) must have live prices."
            )

        # Pull the expiry the IC function actually used (may be fallback)
        exp_used = ic.get("expiry", expiry)

        # Chain intelligence (max pain + GEX) — informational only
        time.sleep(3)
        chain_sig = compute_chain_signals(exp_used, ic["spot"])
        if chain_sig:
            _append_chain_signals(chain_sig, ic["spot"])

        lots       = ic["lots"]
        spot       = ic["spot"]
        net_credit = ic["net_credit"]
        max_loss_per_lot = (SPREAD_WIDTH - net_credit) * LOT_SIZE

        chain_line = ""
        if chain_sig:
            mp      = chain_sig["max_pain_strike"]
            mp_d    = chain_sig["max_pain_dist"]
            gex_lbl = "Calm — market likely stays in a range" if chain_sig["gex_positive"] else "Active — directional move likely"
            _mp_gap = abs(int(spot - mp))
            _mp_rel = "above" if mp > spot else "below"
            chain_line = (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 Option market snapshot\n"
                f"Pin target (where most options expire worthless): ₹{mp:,.0f}  (₹{_mp_gap:,} {_mp_rel} Nifty now)\n"
                f"Day type: {gex_lbl}\n"
                f"Market pricing ±₹{chain_sig['straddle']:.0f}/share swing today"
            )

        nf_expiry_str = exp_used.strftime('%d%b%Y').upper()
        ce_short_sym  = f"NIFTY {nf_expiry_str} {int(ic['ce_short_strike'])} CE"
        ce_long_sym   = f"NIFTY {nf_expiry_str} {int(ic['ce_long_strike'])} CE"
        pe_short_sym  = f"NIFTY {nf_expiry_str} {int(ic['pe_short_strike'])} PE"
        pe_long_sym   = f"NIFTY {nf_expiry_str} {int(ic['pe_long_strike'])} PE"

        _ic_total_credit = int(net_credit * lots * LOT_SIZE)
        _ic_sl_loss      = int(net_credit * CREDIT_SL_FRAC * lots * LOT_SIZE)
        _ic_sl_trig      = net_credit * (1 + CREDIT_SL_FRAC)
        _ic_pe_short     = int(ic['pe_short_strike'])
        _ic_ce_short     = int(ic['ce_short_strike'])
        _ic_pe_long      = int(ic['pe_long_strike'])
        _ic_ce_long      = int(ic['ce_long_strike'])

        notify.send(
            f"🦅  <b>Iron Condor placed</b>  ·  {today_wd}, {today_label}{sig_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>What the model checked</b>\n"
            f"Market checklist: {score_plain}\n"
            f"Computer model: {ml_conf:.0%} confident ({conf_plain})\n"
            f"{ml_conflict_plain}"
            f"{news_plain_line}"
            f"Capital: {cap_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>The trade</b>\n"
            f"Collected <b>₹{_ic_total_credit:,}</b> upfront from 4 option contracts\n"
            f"(₹{net_credit:.0f}/share × {lots*LOT_SIZE:,} shares, {lots} lot{'s' if lots > 1 else ''})\n\n"
            f"Profit zone: Nifty stays between ₹{_ic_pe_short:,} and ₹{_ic_ce_short:,}\n"
            f"Safety wings: ₹{_ic_pe_long:,} (lower cap) · ₹{_ic_ce_long:,} (upper cap)\n"
            f"Nifty now: ₹{spot:,.0f}  ·  Expiry {exp_used.strftime('%d %b')} ({int(dte)}d to go)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Risk</b>\n"
            f"Best case (market stays in range):  +₹{_ic_total_credit:,}\n"
            f"Worst case (stop-loss fires):        −₹{_ic_sl_loss:,}\n"
            f"Stop-loss fires if spread cost > ₹{_ic_sl_trig:.0f}/share\n\n"
            f"Exits at 3:15 PM — time passing shrinks option premiums (works in our favour)"
            f"{chain_line}"
        )

        # Duplicate-position guard (today_trade.json is reliable for 4-leg IC)
        if not DRY_RUN and not PAPER_MODE:
            try:
                with open(f"{DATA_DIR}/today_trade.json") as _ttj:
                    ttj = json.load(_ttj)
                if (ttj.get("date") == datetime.now(_IST).date().isoformat()
                        and ttj.get("strategy") == "nf_iron_condor"
                        and not ttj.get("exit_done", False)):
                    notify.send(
                        f"⚠️  <b>Duplicate IC Blocked</b>\n\n"
                        f"today_trade.json shows IC already placed today.\n"
                        f"Skipping to avoid double exposure."
                    )
                    return
            except Exception:
                pass

        # Place IC
        notify.log("Placing Nifty Iron Condor (4 legs)...")
        order_result = place_iron_condor(ic, exp_used)

        if not DRY_RUN:
            _write_today_ic_trade(ic, signal, dte, score, exp_used, ml_conf, order_result)

        mode = order_result.get("mode")

        if mode == "DRY_RUN":
            notify.send(
                f"✅  <b>Dry Run — Nifty Iron Condor</b>\n\n"
                f"Would have placed:\n"
                f"SELL  <code>{ce_short_sym}</code>  @ ₹{ic['ce_short_ltp']:.0f}\n"
                f"BUY   <code>{ce_long_sym}</code>  @ ₹{ic['ce_long_ltp']:.0f}\n"
                f"SELL  <code>{pe_short_sym}</code>  @ ₹{ic['pe_short_ltp']:.0f}\n"
                f"BUY   <code>{pe_long_sym}</code>  @ ₹{ic['pe_long_ltp']:.0f}\n"
                f"{lots} lot{'s' if lots > 1 else ''}  ·  Net credit ₹{net_credit:.0f}\n\n"
                f"<i>DRY RUN — no real order placed.</i>"
            )
            return

        if mode == "PAPER":
            notify.send(
                f"📝  <b>[PAPER] Nifty Iron Condor — No Real Order</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"SELL  <code>{ce_short_sym}</code>  @ ₹{ic['ce_short_ltp']:.0f}\n"
                f"BUY   <code>{ce_long_sym}</code>  @ ₹{ic['ce_long_ltp']:.0f}\n"
                f"SELL  <code>{pe_short_sym}</code>  @ ₹{ic['pe_short_ltp']:.0f}\n"
                f"BUY   <code>{pe_long_sym}</code>  @ ₹{ic['pe_long_ltp']:.0f}\n"
                f"Net credit ₹{net_credit:.0f} / share  ·  "
                f"{lots} lot{'s' if lots > 1 else ''}  ·  {lots*LOT_SIZE} shares\n"
                f"Max risk ₹{lots * max_loss_per_lot:,.0f}   "
                f"Max profit ₹{lots * net_credit * LOT_SIZE:,.0f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>PAPER MODE. Real money safe. Tracking in data/paper_trades.csv.</i>"
            )
            return

        if mode == "FAILED":
            notify.log("IC placement failed — no position opened. See earlier error.")
            return

        if mode and "PARTIAL" in mode:
            notify.log(f"Partial IC ({mode}) — emergency alert sent. Check Dhan app immediately.")
            return

        # All 4 legs placed — safe to arm account-level P&L safety net.
        if not DRY_RUN:
            _setup_pnl_exit(net_credit, lots)

        notify.send(
            f"✅  <b>Nifty Iron Condor Placed!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"CE BUY   <code>{order_result.get('ce_buy_oid')}</code>\n"
            f"CE SELL  <code>{order_result.get('ce_sell_oid')}</code>\n"
            f"PE BUY   <code>{order_result.get('pe_buy_oid')}</code>\n"
            f"PE SELL  <code>{order_result.get('pe_sell_oid')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SELL  <code>{ce_short_sym}</code>  @ ₹{ic['ce_short_ltp']:.0f}\n"
            f"BUY   <code>{ce_long_sym}</code>  @ ₹{ic['ce_long_ltp']:.0f}\n"
            f"SELL  <code>{pe_short_sym}</code>  @ ₹{ic['pe_short_ltp']:.0f}\n"
            f"BUY   <code>{pe_long_sym}</code>  @ ₹{ic['pe_long_ltp']:.0f}\n"
            f"Net credit ₹{net_credit:.0f}  ·  "
            f"{lots} lot{'s' if lots > 1 else ''}  ·  {lots*LOT_SIZE} shares\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SL   spread > ₹{net_credit * (1 + CREDIT_SL_FRAC):.0f}  "
            f"(auto-exit: spread_monitor.py)\n"
            f"TP   spread < ₹{net_credit * (1 - CREDIT_TP_FRAC):.0f}  "
            f"(65% of credit locked in)\n"
            f"<i>spread_monitor.py watches every 1 min — all 4 legs exit on SL/TP.</i>"
        )

        ce_buy_oid = order_result.get("ce_buy_oid")
        if ce_buy_oid:
            notify.log("Waiting 30s to verify CE buy leg status...")
            time.sleep(30)
            _verify_order_status(ce_buy_oid, ce_short_sym)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # CREDIT SPREAD PATH  (CREDIT_SPREAD_MODE = True, legacy fallback when IRON_CONDOR_MODE = False)
    # Bull Put Spread (PUT) — theta works FOR us. Bear Call permanently removed Apr 2026.
    # ══════════════════════════════════════════════════════════════════════════
    if CREDIT_SPREAD_MODE:
        (short_sid, long_sid, short_strike, long_strike,
         short_ltp, long_ltp, net_credit, lots, spot) = get_spread_legs(signal, expiry, capital)

        if short_sid is None:
            die(
                f"Could not fetch credit spread legs for {signal} / {expiry}.\n"
                f"Both ATM and ATM±{SPREAD_WIDTH}pt {opt_type} must have live prices."
            )

        if not spot or spot <= 0:
            if DRY_RUN:
                spot = 50_000.0
            else:
                die("Spot price unavailable from option chain.")

        if lots < 1:
            notify.send(
                f"⏸  <b>No Trade — Insufficient Capital for Spread</b>\n"
                f"─────────────────────\n"
                f"{today_wd}  ·  {today_label}\n\n"
                f"Signal: <b>{signal}</b>\n"
                f"Max loss per lot = ₹{(SPREAD_WIDTH - net_credit) * LOT_SIZE:,.0f}  "
                f"(spread ₹{SPREAD_WIDTH} − credit ₹{net_credit:.0f}) × {LOT_SIZE} shares\n"
                f"Capital ₹{capital:,.0f} can't cover even 1 lot safely.\n"
                f"Add funds or wait for higher credit on a different day."
            )
            return

        # Chain intelligence (max pain + GEX) — informational only.
        # Dhan /v2/optionchain throttles ~1 req/3s; need a real pause since
        # get_spread_legs just hit the same endpoint.
        time.sleep(3)
        chain_sig = compute_chain_signals(expiry, spot)
        if chain_sig:
            _append_chain_signals(chain_sig, spot)

        chain_line = ""
        if chain_sig:
            mp      = chain_sig["max_pain_strike"]
            mp_d    = chain_sig["max_pain_dist"]
            gex_lbl = "Calm — market likely stays in a range" if chain_sig["gex_positive"] else "Active — directional move likely"
            _mp_gap = abs(int(spot - mp))
            _mp_rel = "above" if mp > spot else "below"
            chain_line = (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 Option market snapshot\n"
                f"Pin target (where most options expire worthless): ₹{mp:,.0f}  (₹{_mp_gap:,} {_mp_rel} Nifty now)\n"
                f"Day type: {gex_lbl}\n"
                f"Market pricing ±₹{chain_sig['straddle']:.0f}/share swing today"
            )

        max_loss_per_lot = (SPREAD_WIDTH - net_credit) * LOT_SIZE
        risk_amt   = lots * net_credit * CREDIT_SL_FRAC * LOT_SIZE
        target_amt = lots * net_credit * LOT_SIZE

        strategy_name = "Bull Put Spread"   # legacy CREDIT_SPREAD_MODE; Bear Call removed Apr 2026
        short_sym = f"NIFTY {expiry.strftime('%d%b%Y').upper()} {int(short_strike)} {opt_type}"
        long_sym  = f"NIFTY {expiry.strftime('%d%b%Y').upper()} {int(long_strike)} {opt_type}"

        # 6. Telegram trade-details message (spread format)
        notify.send(
            f"{opt_emoji}  <b>{strategy_name}</b>  ·  {today_wd}, {today_label}{sig_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Score      {score:+d} / {score_max}{score_desc}\n"
            f"ML conf    {ml_conf:.0%}{'  ✓' if ml_conf >= ML_CONF_THRESHOLD else '  ⚠ low' if (ml_trained and ml_conf < ML_CONF_THRESHOLD) else ''}\n"
            f"{news_row}"
            f"Capital    {cap_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SELL  <code>{short_sym}</code>  @ ₹{short_ltp:.0f}\n"
            f"BUY   <code>{long_sym}</code>  @ ₹{long_ltp:.0f}\n"
            f"Net credit  ₹{net_credit:.0f} / share   ({lots} lot{'s' if lots > 1 else ''}  ·  {lots*LOT_SIZE} shares)\n"
            f"Spot        ₹{spot:,.0f}   DTE {dte:.1f}   Expiry {expiry.strftime('%d %b')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SL   spread value > ₹{net_credit * (1 + CREDIT_SL_FRAC):.0f}  (credit grew {CREDIT_SL_FRAC*100:.0f}%)\n"
            f"TP   spread value < ₹{net_credit * (1 - CREDIT_TP_FRAC):.0f}  ({CREDIT_TP_FRAC*100:.0f}% of credit in pocket)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Max risk   ₹{lots * max_loss_per_lot:,.0f}   Max profit ₹{target_amt:,.0f}"
            f"{chain_line}"
        )

        # 6b. Double-position guard
        if not DRY_RUN and not PAPER_MODE and _check_no_existing_position():
            notify.send(
                f"⚠️  <b>Duplicate Trade Blocked</b>\n\n"
                f"An open Nifty position already exists on your account.\n"
                f"Skipping new order to avoid double exposure.\n\n"
                f"<i>Close the existing position on Dhan app if this is unexpected.</i>"
            )
            return

        # 7. Place spread orders
        notify.log("Placing credit spread...")
        result = place_credit_spread(
            short_sid, long_sid, signal, lots,
            net_credit, short_strike, long_strike,
            short_ltp, long_ltp, spot
        )

        # Write intent for spread_monitor.py + exit_positions.py + trade_journal.py
        if not DRY_RUN:
            _write_today_spread_trade(
                signal=signal,
                short_sid=short_sid, long_sid=long_sid,
                short_strike=short_strike, long_strike=long_strike,
                short_ltp=short_ltp, long_ltp=long_ltp,
                net_credit=net_credit, lots=lots, dte=dte, spot=spot,
                score=score, expiry=expiry, ml_conf=ml_conf,
                order_mode=result.get("mode", "CREDIT_SPREAD"),
                buy_oid=result.get("buy_oid"),
                sell_oid=result.get("sell_oid"),
            )

        # 8. Result handling
        if DRY_RUN:
            notify.send(
                f"✅  <b>Dry Run Complete — {strategy_name}</b>\n\n"
                f"Would have placed:\n"
                f"SELL  <code>{short_sym}</code>  @ ₹{short_ltp:.0f}\n"
                f"BUY   <code>{long_sym}</code>  @ ₹{long_ltp:.0f}\n"
                f"{lots} lot{'s' if lots > 1 else ''}  ·  Net credit ₹{net_credit:.0f}\n\n"
                f"<i>Add funds to your Dhan account to go live.</i>"
            )
            return

        mode = result.get("mode", "CREDIT_SPREAD")

        if mode == "FAILED":
            notify.log("Spread placement failed — no position opened. See earlier error.")
            return

        if mode == "PARTIAL_SPREAD":
            notify.log("PARTIAL SPREAD — only BUY (hedge) leg placed. Emergency alert sent.")
            return

        if mode == "PAPER":
            notify.send(
                f"📝  <b>[PAPER] Credit Spread Logged — No Real Order</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Strategy   {strategy_name}\n"
                f"SELL  <code>{short_sym}</code>  @ ₹{short_ltp:.0f}\n"
                f"BUY   <code>{long_sym}</code>  @ ₹{long_ltp:.0f}\n"
                f"Qty        {lots*LOT_SIZE}  ({lots} lot)\n"
                f"Net credit ₹{net_credit:.0f} / share\n"
                f"Max loss   ₹{lots * max_loss_per_lot:,.0f}   Max profit ₹{target_amt:,.0f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>PAPER MODE. Real money safe. Tracking in data/paper_trades.csv.</i>"
            )
            return

        buy_oid  = result.get("buy_oid")
        sell_oid = result.get("sell_oid")
        notify.send(
            f"✅  <b>{strategy_name} Placed!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"BUY  orderId  <code>{buy_oid}</code>\n"
            f"SELL orderId  <code>{sell_oid}</code>\n"
            f"SELL  <code>{short_sym}</code>  @ ₹{short_ltp:.0f}\n"
            f"BUY   <code>{long_sym}</code>  @ ₹{long_ltp:.0f}\n"
            f"Net credit ₹{net_credit:.0f}  ·  {lots} lot{'s' if lots > 1 else ''}  ·  {lots*LOT_SIZE} shares\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SL   spread > ₹{net_credit * (1 + CREDIT_SL_FRAC):.0f}  (auto-exit: spread_monitor.py)\n"
            f"TP   spread < ₹{net_credit * (1 - CREDIT_TP_FRAC):.0f}  (65% of credit locked in)\n"
            f"<i>spread_monitor.py watches every 1 min — both legs close automatically on SL/TP hit.</i>"
        )

        if buy_oid:
            notify.log("Waiting 30s to verify buy leg status...")
            time.sleep(30)
            _verify_order_status(buy_oid, short_sym)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # NAKED OPTION PATH  (CREDIT_SPREAD_MODE = False)
    # Original single-leg BUY — kept for fallback / comparison
    # ══════════════════════════════════════════════════════════════════════════
    security_id, atm_strike, premium, lots, spot, otm_distance = \
        get_affordable_option(signal, expiry, capital)

    if security_id is None and otm_distance == -2:
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
            f"NIFTY {expiry} {'CE' if signal == 'CALL' else 'PE'}.\n"
            f"Check Dhan API status."
        )

    if not premium or premium <= 0:
        die(
            f"Invalid premium ({premium}) from option chain — cannot calculate SL/TP.\n"
            f"Strike: {atm_strike}  |  Expiry: {expiry}  |  Check option chain API."
        )

    if not spot or spot <= 0:
        if DRY_RUN:
            notify.log("Spot price unavailable — using ₹50,000 placeholder for DRY RUN display")
            spot = 50_000.0
        else:
            die("Spot price unavailable. Cannot confirm trade safety. Check Dhan LTP endpoint.")

    if not lots or lots < 1:
        die(f"Lot sizing returned {lots} — insufficient capital or premium too high for 1 lot.")

    # Dhan /v2/optionchain throttles ~1 req/3s — pause after get_affordable_option.
    time.sleep(3)
    chain_sig = compute_chain_signals(expiry, spot)
    if chain_sig:
        _append_chain_signals(chain_sig, spot)

    sig_spot = float(sig.get("nf_close") or 0)
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
                notify.log("Re-fetch returned no data — using original price from 9:30 open.")
    elif DRY_RUN and sig_spot > 0 and spot > 0:
        spot_gap_pct = abs(spot - sig_spot) / sig_spot
        if spot_gap_pct >= ENTRY_SPOT_GAP_THRESHOLD:
            wait_mins = min(ENTRY_WAIT_MAX_MINS, round(spot_gap_pct * 1000))
            direction_word = "up" if spot > sig_spot else "down"
            notify.log(
                f"[DRY RUN] Adaptive wait WOULD trigger: {spot_gap_pct*100:.1f}% gap "
                f"{direction_word}. Would wait {wait_mins} min before entering."
            )

    rr = RR
    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    risk_amt   = lots * max_loss_1lot
    target_amt = lots * LOT_SIZE * premium * SL_PCT * rr - 40
    sl_price   = premium * (1 - SL_PCT)
    tp_price   = premium * (1 + SL_PCT * rr)

    opt_sym = f"NIFTY {expiry.strftime('%d%b%Y').upper()} {int(atm_strike)} {opt_type}"
    if otm_distance and otm_distance >= 1:
        otm_label = f"  ({otm_distance*100}pt OTM — ATM too pricey for budget)"
    elif otm_distance and otm_distance <= -1:
        otm_label = f"  ({abs(otm_distance)*100}pt ITM — capital flush, higher delta)"
    else:
        otm_label = "  (ATM)"

    chain_line = ""
    if chain_sig:
        mp      = chain_sig["max_pain_strike"]
        mp_d    = chain_sig["max_pain_dist"]
        gex_lbl = "Calm — market likely stays in a range" if chain_sig["gex_positive"] else "Active — directional move likely"
        _mp_gap = abs(int(spot - mp))
        _mp_rel = "above" if mp > spot else "below"
        chain_line = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Option market snapshot\n"
            f"Pin target (where most options expire worthless): ₹{mp:,.0f}  (₹{_mp_gap:,} {_mp_rel} Nifty now)\n"
            f"Day type: {gex_lbl}\n"
            f"Market pricing ±₹{chain_sig['straddle']:.0f}/share swing today"
        )

    stale_line = ""
    if security_id == "DRY_RUN_FALLBACK":
        stale_line = (
            "\n⚠️  <i>Option chain offline — spot/strike/premium are approximated. "
            "Actual Monday trade will use live option-chain prices.</i>"
        )

    notify.send(
        f"{opt_emoji}  <b>BUY {signal}</b>  ·  {today_wd}, {today_label}{sig_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Score      {score:+d} / {score_max}{score_desc}\n"
        f"ML conf    {ml_conf:.0%}{'  ✓' if ml_conf >= ML_CONF_THRESHOLD else '  ⚠ low' if (ml_trained and ml_conf < ML_CONF_THRESHOLD) else ''}\n"
        f"{news_row}"
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
        f"{chain_line}"
        f"{stale_line}"
    )

    if not DRY_RUN and not PAPER_MODE and _check_no_existing_position():
        notify.send(
            f"⚠️  <b>Duplicate Trade Blocked</b>\n\n"
            f"An open Nifty position already exists on your account.\n"
            f"Skipping new order to avoid double exposure.\n\n"
            f"<i>Close the existing position on Dhan app if this is unexpected.</i>"
        )
        return

    notify.log("Placing order...")
    result = place_super_order(security_id, signal, lots, spot, premium, rr)

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

    mode = result.get("mode", "SUPER_ORDER")
    oid  = (result.get("orderId") or result.get("order_id") or
            (result.get("buy_order") or {}).get("orderId"))

    iv_val = float(sig.get("iv_at_entry", sig.get("iv", 0.0)) or 0.0)
    _write_today_trade(signal, atm_strike, lots, dte, spot,
                       oracle_premium=premium,
                       sl_price=sl_price, tp_price=tp_price,
                       security_id=security_id, score=score, iv=iv_val,
                       expiry=expiry, ml_conf=ml_conf,
                       order_id=oid, order_mode=mode)

    if mode == "FAILED":
        notify.log("Order placement failed entirely — no position opened. See earlier error.")
        return
    if mode == "FALLBACK_NO_SL":
        notify.log("FALLBACK_NO_SL — BUY placed but SL failed. Emergency alert sent. Manual action needed.")
        return

    if mode == "PAPER":
        notify.send(
            f"📝  <b>[PAPER] Trade Logged — No Real Order</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Signal     {signal}\n"
            f"Option     <code>{opt_sym}</code>\n"
            f"Qty        {lots*LOT_SIZE}  ({lots} lot)\n"
            f"Entry      ₹{premium:.0f}\n"
            f"SL ₹{sl_price:.0f}  ·  TP ₹{tp_price:.0f}\n"
            f"Max loss   ₹{(premium - sl_price) * lots * LOT_SIZE:,.0f}\n"
            f"Max profit ₹{(tp_price - premium) * lots * LOT_SIZE:,.0f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>PAPER MODE on. Real money safe. Tracking what would have happened\n"
            f"in data/paper_trades.csv to evaluate the new strategy before going live.</i>"
        )
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

    if oid and mode not in ("AMO", "FAILED", "FALLBACK_NO_SL"):
        notify.log("Waiting 30s to verify order status is not REJECTED...")
        time.sleep(30)
        _verify_order_status(oid, opt_sym)


if __name__ == "__main__":
    main()
