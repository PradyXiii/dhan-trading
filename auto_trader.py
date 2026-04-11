#!/usr/bin/env python3
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
SL_PCT    = 0.20
RISK_PCT  = 0.05
MAX_LOTS  = 20
PREMIUM_K = 0.004

RR = 2.0   # reward:risk ratio — flat across all days (SL=20%, TP=+40% of premium)


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


# ── Step 1: Fetch data + generate signal ─────────────────────────────────────

def refresh_data_and_signal():
    notify.log("Fetching latest market data...")
    r1 = subprocess.run(
        [sys.executable, "data_fetcher.py"],
        capture_output=True, text=True, timeout=120
    )
    if r1.returncode != 0:
        notify.log(f"data_fetcher.py had errors:\n{r1.stderr[-200:]}")

    notify.log("Generating signal...")
    r2 = subprocess.run(
        [sys.executable, "signal_engine.py"],
        capture_output=True, text=True, timeout=60
    )
    if r2.returncode != 0:
        die(f"signal_engine.py failed:\n{r2.stderr[-200:]}")


def get_todays_signal() -> tuple:
    """
    Returns (signal_dict, sig_note_str).
    sig_note is empty if today's date matches, or a label like "08 Apr close" if fallback.

    Why the fallback exists:
      At 9:15 AM IST, US markets haven't closed → today's row may be absent.
      Yesterday's signal is correct — it reflects the latest complete close data.
    """
    try:
        df    = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        df    = df.drop(columns=["threshold"], errors="ignore")
        today = pd.Timestamp(date.today())

        row = df[df["date"] == today]
        if not row.empty:
            return row.iloc[0].to_dict(), ""

        last     = df.iloc[-1]
        days_gap = (today - last["date"]).days

        if days_gap <= 4:
            note = f"signal from {last['date'].strftime('%d %b')} close"
            notify.log(f"Today's signal not in CSV — using {note}")
            return last.to_dict(), note

        notify.send(
            f"⚠️ <b>Stale signal</b> ({days_gap} days old)\n\n"
            f"Last signal: {last['date'].date()}\n"
            f"Run data_fetcher.py + signal_engine.py manually."
        )
        return None, ""

    except Exception as e:
        die(f"Cannot read signals.csv: {e}")


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
            return float(bal)
    except Exception:
        pass
    die("Could not fetch available capital from Dhan fund limit API.")


# ── Step 3: Expiry ────────────────────────────────────────────────────────────

def get_expiry() -> date:
    """
    Find the nearest valid BankNifty expiry using the /optionchain/expirylist endpoint.
    Returns the nearest upcoming expiry date. Falls back to Wednesday calculation
    if the API is unavailable.
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
            upcoming = [date.fromisoformat(e) for e in expiries if date.fromisoformat(e) >= today]
            if upcoming:
                expiry = min(upcoming)
                notify.log(f"Expiry from API: {expiry}  (all: {[e for e in expiries[:4]]})")
                return expiry
        notify.log(f"Expiry list API returned {resp.status_code} — falling back to Wednesday calc")
    except Exception as e:
        notify.log(f"Expiry list API failed ({e}) — falling back to Wednesday calc")

    # Fallback: calculate nearest Wednesday
    days_ahead = (2 - today.weekday()) % 7 or 7
    base_wed   = today + timedelta(days=days_ahead)
    notify.log(f"Using calculated Wednesday expiry: {base_wed}")
    return base_wed


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

    # All expiries exhausted — fallback to CSV spot for DRY RUN
    try:
        bn_df      = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
        spot       = spot_fallback or float(bn_df.iloc[-1]["close"])
        atm_strike = round(spot / 100) * 100
        notify.log(f"Option chain unavailable — using BN close ₹{spot:,.0f} (ATM {int(atm_strike)})")
        if DRY_RUN:
            return "DRY_RUN_PLACEHOLDER", atm_strike, spot
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
        "trailingJump":    5,
    }
    try:
        resp   = requests.post("https://api.dhan.co/v2/super/orders",
                               headers=HEADERS, json=payload, timeout=15)
        result = resp.json()
        if resp.status_code == 200 and result.get("status") not in ("failure", "error"):
            return result
        notify.log(f"Super Order failed ({resp.status_code}): {resp.text[:150]}")
    except Exception as e:
        notify.log(f"Super Order exception: {e}")

    # Fallback: market buy + SL-M sell
    # Build a clean OrderRequest payload — no super-order-specific fields
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
    buy_resp   = requests.post("https://api.dhan.co/v2/orders",
                               headers=HEADERS, json=buy_payload, timeout=15)
    time.sleep(2)
    sl_payload = {**buy_payload,
                  "transactionType": "SELL",
                  "orderType":       "STOP_LOSS_MARKET",
                  "triggerPrice":    sl_price,
                  "correlationId":   f"at_sl_{date.today().strftime('%Y%m%d')}"}
    sl_resp    = requests.post("https://api.dhan.co/v2/orders",
                               headers=HEADERS, json=sl_payload, timeout=15)
    return {"buy_order": buy_resp.json(), "sl_order": sl_resp.json(), "mode": "FALLBACK",
            "sl": sl_price, "tp": tp_price}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode_label = "DRY RUN" if DRY_RUN else "LIVE"
    notify.log(f"BankNifty Auto Trader starting [{mode_label}]")

    # 0. Credentials check
    check_credentials()

    # 1. Refresh data + signal
    refresh_data_and_signal()

    # 2. Read signal
    sig, sig_note = get_todays_signal()
    if sig is None:
        return

    signal       = sig["signal"]
    score        = int(sig["score"])
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

    # 4. Expiry + ATM option
    expiry = get_expiry()
    security_id, atm_strike, spot = get_atm_security_id(signal, expiry)

    if not security_id:
        die(
            f"Could not find ATM option for BANKNIFTY {expiry} "
            f"{'CE' if signal == 'CALL' else 'PE'}.\n"
            f"Option chain API may be down. Check Dhan app."
        )

    # 5. Sizing — calculate actual DTE from today to expiry (monthly expiry now)
    # BN switched to monthly expiry (last Wed of month). DTE varies 0–28 across the month.
    # MIN 0.25 so expiry-day premium is nonzero; +1 because options trade on expiry morning.
    dte     = max(0.25, (expiry - date.today()).days + 1)
    rr      = RR
    premium = spot * PREMIUM_K * sqrt(dte)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    lots          = min(MAX_LOTS, max(1, floor(capital * RISK_PCT / max_loss_1lot)))
    risk_amt      = lots * max_loss_1lot
    target_amt    = lots * LOT_SIZE * premium * SL_PCT * rr - 40  # rough charge estimate
    sl_price      = premium * (1 - SL_PCT)
    tp_price      = premium * (1 + SL_PCT * rr)

    opt_type  = "CE" if signal == "CALL" else "PE"
    opt_emoji = "📈" if signal == "CALL" else "📉"
    opt_sym   = f"BANKNIFTY {expiry.strftime('%d%b%Y').upper()} {int(atm_strike)} {opt_type}"
    cap_label = f"₹{capital:,.0f}" + ("  [DRY RUN]" if DRY_RUN else "")
    score_max = 4  # active indicators

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

    # 6. Send ONE trade-details message to Telegram
    notify.send(
        f"{opt_emoji}  <b>BUY {signal}</b>  ·  {today_wd}, {today_label}{sig_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Score      {score:+d} / {score_max}{score_desc}\n"
        f"Capital    {cap_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Option     <code>{opt_sym}</code>\n"
        f"Qty        {lots} lot{'s' if lots > 1 else ''}  ·  {lots*LOT_SIZE} shares\n"
        f"Spot       ₹{spot:,.0f}   Premium  ~₹{premium:.0f}\n"
        f"DTE        {dte:.1f} days  ·  Expiry {expiry.strftime('%d %b')}   RR  {rr}×\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Stop loss  ₹{sl_price:.0f}  (−{SL_PCT*100:.0f}%)\n"
        f"Target     ₹{tp_price:.0f}  (+{SL_PCT*rr*100:.0f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Risk  ₹{risk_amt:,.0f}   Reward  ₹{target_amt:,.0f}"
    )

    # 7. Place order
    notify.log("Placing order...")
    result = place_super_order(security_id, signal, lots, spot, premium, rr)

    # 8. Send ONE result message
    if DRY_RUN:
        notify.send(
            f"✅  <b>Dry Run Complete</b>\n\n"
            f"Would have bought:\n"
            f"<code>{opt_sym}</code>\n"
            f"{lots} lot{'s' if lots > 1 else ''}  ·  "
            f"SL ₹{sl_price:.0f}  ·  TP ₹{tp_price:.0f}\n\n"
            f"<i>Add funds to your Dhan account to go live.</i>"
        )
        return

    # Live result
    mode = result.get("mode", "SUPER_ORDER")
    oid  = (result.get("orderId") or result.get("order_id") or
            (result.get("buy_order") or {}).get("orderId"))

    corr_id = f"at_{date.today().strftime('%Y%m%d')}"
    if oid:
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


if __name__ == "__main__":
    main()
