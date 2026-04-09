#!/usr/bin/env python3
"""
auto_trader.py — BankNifty Options Full Automation
====================================================
Runs every trading day at 9:15 AM IST via cron.
No human interaction required.

Flow:
  1. Fetch latest market data
  2. Generate today's signal
  3. If NONE  → Telegram "No trade today" → exit
  4. If CALL/PUT:
       a. Get available capital from Dhan
       b. Find ATM option security_id via option chain API
       c. Calculate lots / SL / target
       d. Place Dhan Super Order (entry + SL + TP in one shot)
       e. Send Telegram confirmation

Cron (9:15 AM IST = 3:45 AM UTC):
  45 3 * * 1-5 cd ~/dhan-trading && python3 auto_trader.py >> logs/auto_trader.log 2>&1

Add --dry-run flag for testing without placing real orders.
"""

import os
import sys
import time
import subprocess
import requests
import pandas as pd
from datetime import date, timedelta
from math import floor, sqrt
from dotenv import load_dotenv

import notify

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")

DRY_RUN   = "--dry-run" in sys.argv

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR  = "data"
LOT_SIZE  = 30
SL_PCT    = 0.30
RISK_PCT  = 0.05
MAX_LOTS  = 20
PREMIUM_K = 0.004

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}


# ── Helpers ───────────────────────────────────────────────────────────────────

def die(msg: str):
    notify.send(f"❌ AUTO TRADER ERROR\n{msg}\nManual intervention may be needed.")
    sys.exit(1)


def check_credentials():
    if not TOKEN or not CLIENT_ID:
        die("DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID missing from .env")

    # Quick API check
    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                            headers=HEADERS, timeout=10)
        if resp.status_code == 401:
            die("Dhan token expired (401). Go to dhan.co → API settings → regenerate token.")
        if resp.status_code not in (200, 429):
            notify.send(f"⚠️ Dhan API check returned {resp.status_code}. Proceeding anyway.")
    except requests.exceptions.ConnectionError:
        die("Cannot reach Dhan API. Check VM internet / DNS.")


# ── Step 1: Fetch data + generate signal ─────────────────────────────────────

def refresh_data_and_signal():
    """Re-run data_fetcher + signal_engine as subprocesses."""
    notify.send("📥 Fetching latest market data...", silent=True)
    r1 = subprocess.run(
        [sys.executable, "data_fetcher.py"],
        capture_output=True, text=True, timeout=120
    )
    if r1.returncode != 0:
        notify.send(f"⚠️ data_fetcher.py had errors:\n{r1.stderr[-300:]}")

    notify.send("🔢 Generating signal...", silent=True)
    r2 = subprocess.run(
        [sys.executable, "signal_engine.py"],
        capture_output=True, text=True, timeout=60
    )
    if r2.returncode != 0:
        die(f"signal_engine.py failed:\n{r2.stderr[-300:]}")


def get_todays_signal() -> dict:
    """Read today's row from signals.csv."""
    try:
        df  = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        df  = df.drop(columns=["threshold"], errors="ignore")
        today = pd.Timestamp(date.today())

        row = df[df["date"] == today]
        if row.empty:
            # Signal file not yet updated for today (holiday or run before market)
            last = df.iloc[-1]
            notify.send(
                f"⚠️ No signal for {today.date()}.  "
                f"Last available: {last['date'].date()} → {last['signal']}"
            )
            return None
        return row.iloc[0].to_dict()
    except Exception as e:
        die(f"Cannot read signals.csv: {e}")


# ── Step 2: Capital ───────────────────────────────────────────────────────────

def get_capital() -> float:
    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                            headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            d   = resp.json()
            bal = (d.get("availabelBalance") or
                   d.get("availableBalance") or
                   d.get("net") or 0)
            return float(bal)
    except Exception:
        pass
    die("Could not fetch available capital from Dhan fund limit API.")


# ── Step 3: Expiry ────────────────────────────────────────────────────────────

def get_expiry() -> date:
    today = date.today()
    if today.weekday() == 2:          # today IS Wednesday (expiry day)
        return today
    days_ahead = (2 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


# ── Step 4: ATM option security_id ───────────────────────────────────────────

def get_atm_security_id(signal: str, expiry: date):
    """
    Call Dhan option chain API → return (security_id, atm_strike, spot).
    Handles both known Dhan response formats.
    """
    opt_type = "CE" if signal == "CALL" else "PE"
    payload  = {
        "UnderlyingScrip": 25,
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry.strftime("%Y-%m-%d"),
    }

    try:
        resp = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS, json=payload, timeout=15
        )
        if resp.status_code != 200:
            notify.send(f"⚠️ Option chain API {resp.status_code}: {resp.text[:200]}")
            return None, None, None

        data = resp.json()

        # ── Extract underlying spot price ─────────────────────────────────────
        inner = data.get("data") or {}
        spot = float(
            data.get("last_price") or
            data.get("lastTradedPrice") or
            (inner.get("last_price") if isinstance(inner, dict) else 0) or
            (inner.get("underlyingPrice") if isinstance(inner, dict) else 0) or
            0
        )

        atm_strike = round(spot / 100) * 100 if spot else None

        # ── Format A: data.oc[strike][CE/PE].security_id  (primary Dhan format) ──
        oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}
        if oc and atm_strike:
            for delta in [0, 100, -100, 200, -200]:
                key = str(int(atm_strike + delta))
                if key in oc:
                    sub = oc[key].get(opt_type, {})
                    sid = sub.get("security_id") or sub.get("securityId")
                    if sid:
                        atm_strike = float(key)
                        return str(sid), atm_strike, spot

        # ── Format B: flat list with strikePrice + optionType fields ─────────
        options = data.get("options") or data.get("OptionChain") or []
        if isinstance(options, list) and atm_strike:
            for item in options:
                s = float(item.get("strikePrice") or item.get("strike_price") or 0)
                t = (item.get("optionType") or item.get("option_type") or "").upper()
                if abs(s - atm_strike) < 1 and t == opt_type:
                    sid = item.get("security_id") or item.get("securityId")
                    if sid:
                        return str(sid), atm_strike, spot

        notify.send(
            f"⚠️ Could not find {opt_type} at strike {atm_strike} in option chain.\n"
            f"Top-level keys: {list(data.keys())}"
        )
        return None, atm_strike, spot

    except Exception as e:
        notify.send(f"⚠️ Option chain exception: {e}")
        return None, None, None


# ── Step 5: Place Super Order ─────────────────────────────────────────────────

def place_super_order(security_id: str, signal: str, lots: int,
                      spot: float, premium: float, rr: float) -> dict:
    """
    Dhan Super Order = entry + target + stop-loss in one API call.
    Fallback: INTRADAY market buy + separate SL-M order.
    """
    qty        = lots * LOT_SIZE
    sl_price   = round(premium * (1 - SL_PCT),       1)
    tp_price   = round(premium * (1 + SL_PCT * rr),  1)

    if DRY_RUN:
        return {
            "status":  "DRY_RUN",
            "signal":  signal,
            "qty":     qty,
            "premium": premium,
            "sl":      sl_price,
            "tp":      tp_price,
        }

    # ── Primary: Super Order ─────────────────────────────────────────────────
    payload = {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"at_{date.today().strftime('%Y%m%d')}",
        "transactionType": "BUY",
        "exchangeSegment": "NSE_FNO",
        "productType":     "INTRADAY",
        "orderType":       "MARKET",
        "validity":        "DAY",
        "securityId":      security_id,
        "quantity":        qty,
        "price":           0,
        "targetPrice":     tp_price,
        "stopLossPrice":   sl_price,
        "trailingJump":    0,
    }
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/super-order",
            headers=HEADERS, json=payload, timeout=15
        )
        result = resp.json()
        if resp.status_code == 200 and result.get("status") not in ("failure", "error"):
            return result
        notify.send(f"⚠️ Super Order failed ({resp.status_code}): {resp.text[:200]}\nTrying fallback...")
    except Exception as e:
        notify.send(f"⚠️ Super Order exception: {e}. Trying fallback...")

    # ── Fallback: MARKET buy + SL-M sell ────────────────────────────────────
    # Step A: Market buy
    buy_payload = {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"at_buy_{date.today().strftime('%Y%m%d')}",
        "transactionType": "BUY",
        "exchangeSegment": "NSE_FNO",
        "productType":     "INTRADAY",
        "orderType":       "MARKET",
        "validity":        "DAY",
        "securityId":      security_id,
        "quantity":        qty,
        "price":           0,
        "triggerPrice":    0,
        "disclosedQuantity": 0,
        "afterMarketOrder":  False,
    }
    buy_resp   = requests.post("https://api.dhan.co/v2/orders",
                               headers=HEADERS, json=buy_payload, timeout=15)
    buy_result = buy_resp.json()

    # Step B: SL-M sell (stop-loss only — target handled by MIS auto-square at 3:15)
    time.sleep(2)   # wait for buy to register
    sl_payload = {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"at_sl_{date.today().strftime('%Y%m%d')}",
        "transactionType": "SELL",
        "exchangeSegment": "NSE_FNO",
        "productType":     "INTRADAY",
        "orderType":       "STOP_LOSS_MARKET",
        "validity":        "DAY",
        "securityId":      security_id,
        "quantity":        qty,
        "price":           0,
        "triggerPrice":    sl_price,
        "disclosedQuantity": 0,
        "afterMarketOrder":  False,
    }
    sl_resp   = requests.post("https://api.dhan.co/v2/orders",
                              headers=HEADERS, json=sl_payload, timeout=15)
    sl_result = sl_resp.json()

    return {"buy_order": buy_result, "sl_order": sl_result, "mode": "FALLBACK"}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode_tag = "🔵 DRY RUN" if DRY_RUN else "🟢 LIVE"
    notify.send(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 BankNifty Auto Trader starting  [{mode_tag}]\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", silent=True)

    # 0. Credentials check
    check_credentials()

    # 1. Refresh data + signal
    refresh_data_and_signal()

    # 2. Read today's signal
    sig = get_todays_signal()
    if sig is None:
        notify.send("⏸ No signal available for today. No trade.")
        return

    weekday = sig["weekday"]
    signal  = sig["signal"]
    score   = int(sig["score"])
    sig_date = sig["date"]

    if signal not in ("CALL", "PUT"):
        reason = "event day (RBI/Budget)" if sig.get("event_day") else f"score = {score:+d}"
        notify.send(
            f"⏸ <b>NO TRADE</b> today  ({weekday}, {sig_date})\n"
            f"Reason: {reason}"
        )
        return

    opt_type = "CE" if signal == "CALL" else "PE"
    notify.send(
        f"📊 Signal: <b>{signal}</b>  ({weekday})\n"
        f"Score: {score:+d}  |  Action: BUY BankNifty ATM {opt_type}"
    )

    # 3. Capital
    capital = get_capital()
    notify.send(f"💰 Available capital: ₹{capital:,.0f}", silent=True)

    # 4. Expiry
    expiry = get_expiry()

    # 5. ATM option lookup
    security_id, atm_strike, spot = get_atm_security_id(signal, expiry)

    if not security_id:
        die(
            f"Could not find security_id for BANKNIFTY {expiry} {atm_strike} {opt_type}.\n"
            f"Option chain API may be down. Check manually on Dhan app."
        )

    # 6. Sizing
    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = spot * PREMIUM_K * sqrt(dte)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    lots          = min(MAX_LOTS, max(1, floor(capital * RISK_PCT / max_loss_1lot)))
    risk_amt      = lots * max_loss_1lot
    target_amt    = lots * LOT_SIZE * premium * SL_PCT * rr - 40  # after charges
    sl_price      = premium * (1 - SL_PCT)
    tp_price      = premium * (1 + SL_PCT * rr)

    opt_sym = f"BANKNIFTY {expiry.strftime('%d%b%Y').upper()} {int(atm_strike)} {opt_type}"

    notify.send(
        f"📋 <b>Trade details</b>\n"
        f"Option : {opt_sym}\n"
        f"Lots   : {lots}  ({lots*LOT_SIZE} qty)  |  DTE={dte}  RR={rr}\n"
        f"Premium: ~₹{premium:.0f}  |  Spot: ₹{spot:,.0f}\n"
        f"SL     : ₹{sl_price:.0f}  (−{SL_PCT*100:.0f}%)\n"
        f"Target : ₹{tp_price:.0f}  (+{SL_PCT*rr*100:.0f}%)\n"
        f"Max loss : ₹{risk_amt:,.0f}  |  Max gain : ₹{target_amt:,.0f}"
    )

    # 7. Place order
    notify.send("🚀 Placing order...", silent=True)
    result = place_super_order(security_id, signal, lots, spot, premium, rr)

    # 8. Confirmation message
    if DRY_RUN:
        notify.send(
            f"✅ <b>DRY RUN complete</b>\n"
            f"Would have placed: BUY {lots} lot(s) {opt_sym}\n"
            f"SL ₹{sl_price:.0f}  |  TP ₹{tp_price:.0f}"
        )
        return

    if isinstance(result, dict):
        mode = result.get("mode", "SUPER_ORDER")
        oid  = (result.get("orderId") or
                result.get("order_id") or
                (result.get("buy_order") or {}).get("orderId"))

        if oid:
            notify.send(
                f"✅ <b>Order placed!</b>  [{mode}]\n"
                f"Order ID : {oid}\n"
                f"Option   : {opt_sym}\n"
                f"Qty      : {lots*LOT_SIZE}  |  SL ₹{sl_price:.0f}  |  TP ₹{tp_price:.0f}\n"
                f"Max risk : ₹{risk_amt:,.0f}  |  Max gain : ₹{target_amt:,.0f}\n"
                f"MIS auto-exits at 3:15 PM if SL/TP not triggered."
            )
        else:
            notify.send(
                f"⚠️ Order response received but no order ID found.\n"
                f"Full response: {str(result)[:400]}\n"
                f"Check Dhan app → Orders to confirm."
            )
    else:
        notify.send(f"⚠️ Unexpected order result: {result}\nCheck Dhan app.")


if __name__ == "__main__":
    main()
