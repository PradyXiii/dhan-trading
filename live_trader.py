#!/usr/bin/env python3
"""
live_trader.py — BankNifty Options Live Execution
===================================================
Reads today's signal → finds ATM option on Dhan → calculates sizing
→ previews trade, then places bracket order on confirmation.

Usage:
    python3 live_trader.py              # dry run: shows trade, NO order placed
    python3 live_trader.py --execute    # places actual order after confirmation

Run every trading morning between 9:15–9:20 AM IST, AFTER running:
    python3 data_fetcher.py && python3 signal_engine.py
"""

import os
import sys
import requests
import pandas as pd
from datetime import date, timedelta
from math import floor, sqrt
from dotenv import load_dotenv

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID")

if not TOKEN or not CLIENT_ID:
    print("ERROR: DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID not found in .env")
    sys.exit(1)

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR  = "data"
LOT_SIZE  = 30
SL_PCT    = 0.30      # stop-loss = 30% of premium
RISK_PCT  = 0.05      # risk 5% of capital per trade
MAX_LOTS  = 20        # lot cap for liquidity/margin safety
PREMIUM_K = 0.004     # ATM premium = spot × K × sqrt(DTE)

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}


# ── Step 1: Today's signal ────────────────────────────────────────────────────

def get_todays_signal():
    """Read today's signal row from signals.csv (regenerate first if stale)."""
    try:
        df  = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        df  = df.drop(columns=["threshold"], errors="ignore")
        today = pd.Timestamp(date.today())

        row = df[df["date"] == today]
        if row.empty:
            last = df.iloc[-1]
            print(f"  Note: No signal found for today ({today.date()}).")
            print(f"  Latest available: {last['date'].date()}  →  {last['signal']}")
            print(f"  Run: python3 data_fetcher.py && python3 signal_engine.py")
            return last
        return row.iloc[0]

    except FileNotFoundError:
        print("ERROR: data/signals.csv not found.")
        print("Run:  python3 data_fetcher.py && python3 signal_engine.py")
        sys.exit(1)


# ── Step 2: Available capital ─────────────────────────────────────────────────

def get_capital():
    """Fetch available balance from Dhan fund limit API."""
    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit",
                            headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            # Dhan has a typo in their API: "availabelBalance"
            bal = (d.get("availabelBalance") or
                   d.get("availableBalance") or
                   d.get("net") or 0)
            return float(bal)
    except Exception:
        pass
    return None


# ── Step 3: Nearest Wednesday expiry ─────────────────────────────────────────

def get_expiry():
    """Return nearest Wednesday (BankNifty weekly expiry). Today if today is Wed."""
    today = date.today()
    if today.weekday() == 2:          # today is Wednesday
        return today
    days_ahead = (2 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


# ── Step 4: ATM option security_id via Dhan option chain ─────────────────────

def get_atm_security_id(signal, expiry):
    """
    Call Dhan option chain API to find the ATM CE or PE for BankNifty.
    Returns (security_id, strike, spot).
    """
    opt_type = "CE" if signal == "CALL" else "PE"
    payload  = {
        "UnderlyingScrip": 25,
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry.strftime("%Y-%m-%d"),
    }
    try:
        resp = requests.post("https://api.dhan.co/v2/optionchain",
                             headers=HEADERS, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"  Option chain API returned {resp.status_code}: {resp.text[:300]}")
            return None, None, None

        data = resp.json()

        # ── Extract spot price ────────────────────────────────────────────────
        spot = float(
            data.get("last_price") or
            data.get("lastTradedPrice") or
            (data.get("data") or {}).get("underlyingPrice") or
            (data.get("data") or {}).get("last_price") or
            0
        )

        # ── Try to find spot inside any nested structure ──────────────────────
        if spot == 0 and isinstance(data.get("data"), dict):
            for v in data["data"].values():
                if isinstance(v, (int, float)) and 40000 < v < 80000:
                    spot = float(v)
                    break

        atm_strike = round(spot / 100) * 100 if spot else None

        # ── Search option list for ATM strike ─────────────────────────────────
        options = (data.get("data") or
                   data.get("OptionChain") or
                   data.get("options") or [])

        # Format A: list of dicts with strikePrice, optionType, security_id
        if isinstance(options, list):
            for item in options:
                s = float(item.get("strikePrice") or item.get("strike_price") or 0)
                t = item.get("optionType") or item.get("option_type") or ""
                if atm_strike and abs(s - atm_strike) < 1 and t.upper() == opt_type:
                    sid = item.get("security_id") or item.get("securityId")
                    if sid:
                        return str(sid), atm_strike, spot

        # Format B: dict keyed by strike, with CE/PE sub-dicts
        elif isinstance(options, dict):
            for key, val in options.items():
                try:
                    s = float(key)
                except ValueError:
                    continue
                if atm_strike and abs(s - atm_strike) < 1:
                    sub = val.get(opt_type) or val.get(opt_type.lower()) or {}
                    sid = sub.get("security_id") or sub.get("securityId")
                    if sid:
                        return str(sid), atm_strike, spot

        # Couldn't parse — dump keys to help debug
        print(f"  Could not find {opt_type} at {atm_strike} in response.")
        print(f"  Top-level keys: {list(data.keys())}")
        return None, atm_strike, spot

    except Exception as e:
        print(f"  Option chain error: {e}")
        return None, None, None


# ── Step 5: Place bracket order ──────────────────────────────────────────────

def place_order(security_id, signal, lots, estimated_premium, rr):
    """Place live bracket order via dhanhq SDK."""
    from dhanhq import DhanContext, dhanhq as DhanHQ

    context = DhanContext(CLIENT_ID, TOKEN)
    dhan    = DhanHQ(context)

    qty        = lots * LOT_SIZE
    sl_pts     = round(estimated_premium * SL_PCT,       1)   # option price drop to SL
    target_pts = round(estimated_premium * SL_PCT * rr,  1)   # option price rise to TP

    result = dhan.place_order(
        security_id      = security_id,
        exchange_segment = "NSE_FNO",
        transaction_type = "BUY",
        quantity         = qty,
        order_type       = "MARKET",
        product_type     = "INTRADAY",
        price            = 0,
        bo_profit_value     = target_pts,
        bo_stop_loss_value  = sl_pts,
    )
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--execute" not in sys.argv

    banner = "DRY RUN — order will NOT be placed" if dry_run else "LIVE EXECUTION"
    print(f"\n{'═'*58}")
    print(f"  BankNifty Live Trader  [{banner}]")
    print(f"{'═'*58}\n")

    # ── 1. Signal ─────────────────────────────────────────────────────────────
    print("[ 1 ] Today's signal")
    row     = get_todays_signal()
    weekday = row["weekday"]
    signal  = row["signal"]
    score   = row["score"]
    print(f"      {row['date'].date()}  {weekday}  |  score {score:+d}  |  signal → {signal}")

    if signal not in ("CALL", "PUT"):
        print(f"\n  ✗  NO TRADE today (signal = {signal}). Exiting.\n")
        return

    # ── 2. Capital ────────────────────────────────────────────────────────────
    print("\n[ 2 ] Available capital")
    capital = get_capital()
    if capital and capital > 0:
        print(f"      ₹{capital:,.0f}  (from Dhan fund limit API)")
    else:
        print("      Could not fetch from Dhan API.")
        try:
            capital = float(input("      Enter available capital manually (₹): ")
                            .replace(",", "").strip())
        except (ValueError, EOFError):
            print("      Cannot proceed without capital. Exiting.")
            sys.exit(1)

    # ── 3. Expiry ─────────────────────────────────────────────────────────────
    expiry = get_expiry()
    print(f"\n[ 3 ] Expiry  →  {expiry}  (nearest Wednesday)")

    # ── 4. ATM option ─────────────────────────────────────────────────────────
    print(f"\n[ 4 ] ATM option lookup  ({signal} → {'CE' if signal=='CALL' else 'PE'})")
    security_id, atm_strike, spot = get_atm_security_id(signal, expiry)

    if spot:
        print(f"      BankNifty spot : ₹{spot:,.0f}")
    if atm_strike:
        opt_sym = f"BANKNIFTY {expiry.strftime('%d%b%Y').upper()} {int(atm_strike)} {'CE' if signal=='CALL' else 'PE'}"
        print(f"      ATM option     : {opt_sym}")
    if security_id:
        print(f"      Security ID    : {security_id}")
    else:
        print(f"\n      Auto-lookup failed. Find the security_id manually:")
        print(f"        Dhan app → F&O → BANKNIFTY → Expiry {expiry} → {atm_strike} {'CE' if signal=='CALL' else 'PE'}")
        try:
            security_id = input("      Paste security_id (or Enter to abort): ").strip()
        except (ValueError, EOFError):
            security_id = ""
        if not security_id:
            print("      Aborted — no security_id.")
            return

    # ── 5. Sizing ─────────────────────────────────────────────────────────────
    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    ref_px  = spot if spot else (atm_strike or 50000)
    premium = ref_px * PREMIUM_K * sqrt(dte)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    lots          = min(MAX_LOTS, max(1, floor(capital * RISK_PCT / max_loss_1lot)))
    risk_amt      = lots * max_loss_1lot
    target_amt    = lots * LOT_SIZE * premium * SL_PCT * rr - 40

    sl_price     = premium * (1 - SL_PCT)
    target_price = premium * (1 + SL_PCT * rr)

    print(f"\n[ 5 ] Trade sizing")
    print(f"      DTE {dte}  |  RR {rr}:1  |  est. premium ₹{premium:.0f}")
    print(f"      Lots     : {lots}  ({lots * LOT_SIZE} qty)")
    print(f"      Max risk : ₹{risk_amt:,.0f}  ({risk_amt/capital*100:.1f}% of capital)")
    print(f"      SL price : ₹{sl_price:.1f}  (−{SL_PCT*100:.0f}% from entry)")
    print(f"      TP price : ₹{target_price:.1f}  (+{SL_PCT*rr*100:.0f}% from entry)")
    print(f"      Max gain : ₹{target_amt:,.0f}")

    # ── Summary ───────────────────────────────────────────────────────────────
    action = "CE" if signal == "CALL" else "PE"
    print(f"\n{'═'*58}")
    print(f"  TRADE: BUY {lots} lot{'s' if lots > 1 else ''} × {opt_sym}")
    print(f"  Entry: MARKET at open  |  Product: MIS (Intraday)")
    print(f"  SL   : option falls to ~₹{sl_price:.0f}  →  loss ≈ ₹{risk_amt:,.0f}")
    print(f"  TP   : option rises to ~₹{target_price:.0f}  →  gain ≈ ₹{target_amt:,.0f}")
    print(f"{'═'*58}")

    if dry_run:
        print("\n  [DRY RUN] No order placed.")
        print("  Re-run with --execute flag to go live.\n")
        return

    # ── Live confirmation ─────────────────────────────────────────────────────
    print("\n  ⚠  This will place a REAL order on your Dhan account.")
    try:
        confirm = input("  Type  YES  to confirm: ").strip().upper()
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborted.")
        return

    if confirm != "YES":
        print("  Aborted.")
        return

    print("\n  Placing order...")
    try:
        result = place_order(security_id, signal, lots, premium, rr)
        print(f"  Order result: {result}")
        if isinstance(result, dict):
            oid = result.get("orderId") or result.get("order_id")
            if oid:
                print(f"\n  ✓ Order placed!  Order ID: {oid}")
                print(f"  Monitor in Dhan app → Orders tab.")
    except Exception as e:
        print(f"  Order placement failed: {e}")
        print("  Place manually in Dhan app using the details above.")


if __name__ == "__main__":
    main()
